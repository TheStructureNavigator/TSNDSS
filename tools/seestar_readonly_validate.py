"""Operator-run, READ-ONLY validation of the DB-02 Seestar provider against real hardware.

Run it yourself, locally, with the telescope powered and the arm in whatever
position it is already in. It sends only the three allow-listed read requests
(plus the authentication prerequisite). It never moves the telescope, starts
a camera mode, opens a preview stream, changes a setting, or enables a heater.

    set TSNDSS_SEESTAR_KEY_PATH=<path to your own PEM file>      (Windows CMD)
    export TSNDSS_SEESTAR_KEY_PATH=<path to your own PEM file>   (POSIX)
    python tools/seestar_readonly_validate.py --host <telescope address> --out report.json

TSNDSS neither supplies nor extracts the key. The report contains only step
results, counts and states; it never contains the host, key path, serial or
raw device payloads. Review report.json before sharing it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tsn_dss.engine.device_runtime import ConnectionState, ProviderRuntime  # noqa: E402
from tsn_dss.engine.seestar_provider.errors import SeestarError  # noqa: E402
from tsn_dss.engine.seestar_provider import (  # noqa: E402
    RsaKeyFileAuthenticator,
    SeestarProvider,
    SeestarProviderConfig,
    TcpSeestarTransport,
)

STEADY_PREFIXES = ("mount.", "app.")  # evidence expected unchanged by a read-only session
VIEWS = ("View", "SecondView")
_WORD = re.compile(r"^[A-Za-z0-9_.-]{0,40}$")


def _field(source, *path) -> dict:
    """One field of an app-state view: ``absent`` (key not in the reply) is not the same as ``null`` (key present, value null)."""
    current = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return {"present": False}
        current = current[key]
    if current is None:
        return {"present": True, "null": True}
    if isinstance(current, bool) or (isinstance(current, str) and _WORD.match(current)):
        return {"present": True, "null": False, "value": current}
    return {"present": True, "null": False, "value_type": type(current).__name__}  # never an arbitrary payload


def _number(value) -> dict:
    """A reported number as the device sent it (no conversion), or why it is not one."""
    if value is None:
        return {"present": True, "null": True}
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {"present": True, "null": False, "value_type": type(value).__name__}
    if value != value or value in (float("inf"), float("-inf")):
        return {"present": True, "null": False, "value_type": "non_finite_number"}
    return {"present": True, "null": False, "value": value}


def equ_coord_report(reply) -> dict:
    """Sanitized description of a ``scope_get_equ_coord`` reply: structure, field names and the reported RA/Dec numbers.

    Nothing is converted or reinterpreted. Units are the documented ones of a third-party library, not verified on this
    firmware, and no source states the coordinate epoch."""
    result = reply.result
    out: dict = {"rpc_ok": reply.code == 0, "code": reply.code, "result_type": type(result).__name__,
                 "units_documented_unverified": {"ra": "hours", "dec": "degrees"}, "epoch": "not stated by any source"}
    if isinstance(result, dict):
        out["result_keys"] = sorted(str(k) for k in result)
        out["fields"] = {"ra": _number(result["ra"]) if "ra" in result else {"present": False},
                         "dec": _number(result["dec"]) if "dec" in result else {"present": False}}
        out["other_key_types"] = {str(k): type(v).__name__ for k, v in result.items() if k not in ("ra", "dec")}
    elif isinstance(result, list):
        out["list_length"] = len(result)
        out["items"] = [_number(v) for v in result[:6]]
    ra, dec = (out.get("fields") or {}).get("ra", {}), (out.get("fields") or {}).get("dec", {})
    if "value" in ra and "value" in dec:
        out["within_documented_ranges"] = 0 <= ra["value"] <= 24 and -90 <= dec["value"] <= 90
    return out


def camera_state_report(reply) -> dict:
    """Sanitized description of a ``get_camera_state`` reply: structure and the state word, if there is one.

    The reply is not attributed to any camera (MAIN, WIDE or both is unknown) and no camera state is inferred from it."""
    result = reply.result
    out: dict = {"rpc_ok": reply.code == 0, "code": reply.code, "result_type": type(result).__name__,
                 "camera_scope": "not determined: MAIN, WIDE or both is unknown"}
    if isinstance(result, dict):
        out["result_keys"] = sorted(str(k) for k in result)
        out["state"] = _field(result, "state")
        out["other_key_types"] = {str(k): type(v).__name__ for k, v in result.items() if k != "state"}
        out["nested_keys"] = {str(k): sorted(str(n) for n in v)[:20] for k, v in result.items() if isinstance(v, dict)}
    elif isinstance(result, list):
        out["list_length"] = len(result)
        out["item_types"] = [type(v).__name__ for v in result[:10]]
    elif isinstance(result, str):
        out["string_value"] = _field({"v": result}, "v")
    return out


def app_state_shape(result) -> dict:
    """The structure of an ``iscope_get_app_state`` result: key names and a few state words. No payloads, no addresses."""
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    views = {}
    for name in VIEWS:
        view = result.get(name)
        if name not in result:
            views[name] = {"present": False}
        elif not isinstance(view, dict):
            views[name] = {"present": True, "null": view is None, "value_type": type(view).__name__}
        else:
            views[name] = {"present": True, "keys": sorted(str(k) for k in view),
                           "fields": {label: _field(view, *path) for label, path in
                                      (("state", ("state",)), ("stage", ("stage",)), ("mode", ("mode",)), ("RTSP.state", ("RTSP", "state")))}}
    return {"top_level_keys": sorted(str(k) for k in result), "views": views}


class Report:
    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []

    def record(self, step: str, ok: bool, **details: object) -> bool:
        self.steps.append({"step": step, "ok": ok, **details})
        print(f"[{'PASS' if ok else 'FAIL'}] {step}")
        return ok


def _steady(sample) -> dict[str, object]:
    return {i.name: (i.state.value, i.value) for i in sample.items if i.name.startswith(STEADY_PREFIXES)}


def run(host: str, key_env: str, out: Path, udp: bool, overwrite: bool = False, equ_coord: bool = False, camera_state: bool = False) -> int:
    # Refuse before anything else, so an existing report is never silently replaced.
    shape_path = out.with_name("app_state_" + out.name)  # does not match a report glob such as <stem>*.json
    equ_path = out.with_name("equ_coord_" + out.name)
    camera_path = out.with_name("camera_state_" + out.name)
    if out.is_dir() or (out.exists() and not overwrite) or shape_path.is_dir() or (shape_path.exists() and not overwrite) \
            or (equ_coord and (equ_path.is_dir() or (equ_path.exists() and not overwrite))) \
            or (camera_state and (camera_path.is_dir() or (camera_path.exists() and not overwrite))):
        print("[FAIL] output file already exists; choose another name or pass --overwrite")
        return 2
    report = Report()
    key_path = os.environ.get(key_env)
    if not report.record("credential configuration present", bool(key_path and os.path.isfile(key_path))):
        out.write_text(json.dumps({"steps": report.steps}, indent=2) + "\n", encoding="utf-8")
        return 2

    config = SeestarProviderConfig(host=host, key_path=key_path, allow_udp_discovery=udp)
    transport = TcpSeestarTransport(config, authenticator=RsaKeyFileAuthenticator(key_path))
    runtime = ProviderRuntime(SeestarProvider(config, transport))

    discovery = runtime.discover()
    found = len(discovery.devices) == 1
    report.record(
        "discovery returns one runtime Device Reference",
        found,
        outcome=discovery.outcome.value,
        error_category=discovery.error_category,
        model=discovery.devices[0].model if found else None,
        firmware=discovery.devices[0].firmware_version if found else None,
        device_fingerprint=hashlib.sha256(discovery.devices[0].device_ref.encode()).hexdigest()[:4] if found else None,
    )
    if not found:
        out.write_text(json.dumps({"steps": report.steps}, indent=2) + "\n", encoding="utf-8")
        return 1
    device = discovery.devices[0]

    connection = runtime.connect(runtime.open_connection(device))
    report.record("connection established with identity verified", connection.state is ConnectionState.CONNECTED,
                  state=connection.state.value)
    if connection.state is not ConnectionState.CONNECTED:
        out.write_text(json.dumps({"steps": report.steps}, indent=2) + "\n", encoding="utf-8")
        return 1

    before = runtime.read_telemetry(connection)
    report.record("evidence refresh reaches ready", runtime.refresh_evidence(connection) is ConnectionState.READY)

    capabilities = runtime.capability_report(connection)
    report.record(
        "capability report is fresh and read-only",
        all(not e.available_now for e in capabilities.entries if not e.supported_by_provider)
        and capabilities.observed_at is not None,
        supported=sorted(e.name for e in capabilities.entries if e.supported_by_provider),
    )

    counts = Counter(i.state.value for i in before.items)
    report.record("telemetry sample carries host observation time", before.host_observed_at is not None,
                  states=dict(counts), items=len(before.items))
    preview = runtime.describe_preview(connection)
    report.record("preview availability is evidence only", preview.is_runtime_evidence_only and not preview.is_canonical_record,
                  availability=preview.availability.value)

    # The same authenticated read-only transport, one allow-listed read. Written to a separate file so the audited report keeps its shape.
    try:
        app_reply = transport.read_app_state(host)
        diagnostic = {"code_ok": app_reply.code == 0, "shape": app_state_shape(app_reply.result)}
    except SeestarError as exc:
        diagnostic = {"code_ok": False, "error_category": exc.category}
    shape_path.write_text(json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8")
    print(f"[INFO] app-state structure written to {shape_path.name} (field names and state words only)")

    if equ_coord:  # opt-in: one allow-listed read of the mount's reported coordinates; nothing moves
        try:
            equ_report = equ_coord_report(transport.read_equ_coord(host))
        except SeestarError as exc:
            equ_report = {"rpc_ok": False, "error_category": exc.category}
        equ_path.write_text(json.dumps(equ_report, indent=2) + "\n", encoding="utf-8")
        print(f"[INFO] coordinate diagnostic written to {equ_path.name} (field names and numbers only)")

    if camera_state:  # opt-in: one allow-listed read of the device's camera state word; no view is started or stopped
        try:
            camera_report = camera_state_report(transport.read_camera_state(host))
        except SeestarError as exc:
            camera_report = {"rpc_ok": False, "error_category": exc.category}
        camera_path.write_text(json.dumps(camera_report, indent=2) + "\n", encoding="utf-8")
        print(f"[INFO] camera-state diagnostic written to {camera_path.name} (field names and state words only)")

    after = runtime.read_telemetry(connection)
    unchanged = _steady(before) == _steady(after)
    report.record("mount and view state unchanged across the session", unchanged)

    runtime.disconnect(connection)
    second = runtime.connect(runtime.open_connection(device))
    report.record("reconnect uses a new connection identity",
                  second.connection_id != connection.connection_id and second.state is ConnectionState.CONNECTED)
    runtime.disconnect(second)

    ok = all(step["ok"] for step in report.steps)
    out.write_text(json.dumps({"all_passed": ok, "steps": report.steps}, indent=2) + "\n", encoding="utf-8")
    print("Physical check (manual): confirm the telescope did not move and no camera mode started.")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DB-02 hardware validation (operator-run).")
    parser.add_argument("--host", required=True, help="telescope address; never printed or written to the report")
    parser.add_argument("--key-env", default="TSNDSS_SEESTAR_KEY_PATH", help="environment variable holding your PEM path")
    parser.add_argument("--out", type=Path, default=Path("seestar_validation_report.json"))
    parser.add_argument("--udp", action="store_true", help="also allow opt-in UDP discovery (explicit host still wins)")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing report file")
    parser.add_argument("--equ-coord", action="store_true", help="also read the mount's reported RA/Dec once (read-only) into equ_coord_<report name>")
    parser.add_argument("--camera-state", action="store_true", help="also read the device's camera state once (read-only) into camera_state_<report name>")
    args = parser.parse_args()
    return run(args.host, args.key_env, args.out, args.udp, args.overwrite, args.equ_coord, args.camera_state)


if __name__ == "__main__":
    sys.exit(main())
