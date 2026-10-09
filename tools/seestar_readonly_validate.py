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
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tsn_dss.engine.device_runtime import ConnectionState, ProviderRuntime  # noqa: E402
from tsn_dss.engine.seestar_provider import (  # noqa: E402
    RsaKeyFileAuthenticator,
    SeestarProvider,
    SeestarProviderConfig,
    TcpSeestarTransport,
)

STEADY_PREFIXES = ("mount.", "app.")  # evidence expected unchanged by a read-only session


class Report:
    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []

    def record(self, step: str, ok: bool, **details: object) -> bool:
        self.steps.append({"step": step, "ok": ok, **details})
        print(f"[{'PASS' if ok else 'FAIL'}] {step}")
        return ok


def _steady(sample) -> dict[str, object]:
    return {i.name: (i.state.value, i.value) for i in sample.items if i.name.startswith(STEADY_PREFIXES)}


def run(host: str, key_env: str, out: Path, udp: bool, overwrite: bool = False) -> int:
    # Refuse before anything else, so an existing report is never silently replaced.
    if out.is_dir() or (out.exists() and not overwrite):
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
    args = parser.parse_args()
    return run(args.host, args.key_env, args.out, args.udp, args.overwrite)


if __name__ == "__main__":
    sys.exit(main())
