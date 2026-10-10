"""Read-only Seestar location/time diagnostic.

This operator-run tool performs exactly two authenticated read RPCs:
``get_user_location`` and ``pi_get_time``. It does not write device settings,
move the mount, start a view or update TSN DSS canonical Site data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tsn_dss.engine.seestar_provider import RsaKeyFileAuthenticator, SeestarProviderConfig, TcpSeestarTransport  # noqa: E402
from tsn_dss.engine.seestar_provider.errors import SeestarError  # noqa: E402

_SAFE_WORD = re.compile(r"^[A-Za-z0-9_./:+-]{0,80}$")


def _number(value: Any) -> dict[str, Any]:
    if value is None:
        return {"present": True, "null": True}
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {"present": True, "null": False, "value_type": type(value).__name__}
    if value != value or value in (float("inf"), float("-inf")):
        return {"present": True, "null": False, "value_type": "non_finite_number"}
    return {"present": True, "null": False, "value": float(value)}


def _word(value: Any) -> dict[str, Any]:
    if value is None:
        return {"present": True, "null": True}
    if isinstance(value, str) and _SAFE_WORD.match(value):
        return {"present": True, "null": False, "value": value}
    return {"present": True, "null": False, "value_type": type(value).__name__}


def user_location_report(reply: Any) -> dict[str, Any]:
    """Describe a ``get_user_location`` reply without converting coordinate order."""
    result = reply.result
    out: dict[str, Any] = {
        "rpc_ok": reply.code == 0,
        "code": reply.code,
        "method": "get_user_location",
        "result_type": type(result).__name__,
        "order_documented_unverified": ["longitude_deg", "latitude_deg"],
    }
    if isinstance(result, list):
        out["list_length"] = len(result)
        out["items"] = [_number(value) for value in result[:4]]
        if len(result) >= 2 and "value" in out["items"][0] and "value" in out["items"][1]:
            lon, lat = out["items"][0]["value"], out["items"][1]["value"]
            out["within_earth_ranges"] = -180 <= lon <= 180 and -90 <= lat <= 90
    elif isinstance(result, dict):
        out["result_keys"] = sorted(str(key) for key in result)
        out["fields"] = {
            "lat": _number(result["lat"]) if "lat" in result else {"present": False},
            "lon": _number(result["lon"]) if "lon" in result else {"present": False},
        }
        out["other_key_types"] = {str(k): type(v).__name__ for k, v in result.items() if k not in ("lat", "lon")}
        lat, lon = out["fields"]["lat"], out["fields"]["lon"]
        if "value" in lat and "value" in lon:
            out["within_earth_ranges"] = -90 <= lat["value"] <= 90 and -180 <= lon["value"] <= 180
    return out


def pi_time_report(reply: Any) -> dict[str, Any]:
    """Describe a ``pi_get_time`` reply. The device's timezone string is reported as data, not trusted."""
    result = reply.result
    out: dict[str, Any] = {
        "rpc_ok": reply.code == 0,
        "code": reply.code,
        "method": "pi_get_time",
        "result_type": type(result).__name__,
    }
    if isinstance(result, dict):
        out["result_keys"] = sorted(str(key) for key in result)
        out["fields"] = {
            "year": _number(result["year"]) if "year" in result else {"present": False},
            "mon": _number(result["mon"]) if "mon" in result else {"present": False},
            "day": _number(result["day"]) if "day" in result else {"present": False},
            "hour": _number(result["hour"]) if "hour" in result else {"present": False},
            "min": _number(result["min"]) if "min" in result else {"present": False},
            "sec": _number(result["sec"]) if "sec" in result else {"present": False},
            "time_zone": _word(result["time_zone"]) if "time_zone" in result else {"present": False},
        }
        out["other_key_types"] = {
            str(k): type(v).__name__
            for k, v in result.items()
            if k not in ("year", "mon", "day", "hour", "min", "sec", "time_zone")
        }
    elif isinstance(result, str):
        out["string_value"] = _word(result)
    return out


def run(host: str, key_env: str, out: Path, overwrite: bool = False) -> int:
    if out.is_dir() or (out.exists() and not overwrite):
        print("[FAIL] output file already exists; choose another name or pass --overwrite")
        return 2
    key_path = os.environ.get(key_env)
    if not key_path or not os.path.isfile(key_path):
        out.write_text(json.dumps({"ok": False, "error_category": "credential_configuration_missing"}, indent=2) + "\n", encoding="utf-8")
        return 2

    transport = TcpSeestarTransport(
        SeestarProviderConfig(host=host, key_path=key_path),
        authenticator=RsaKeyFileAuthenticator(key_path),
    )
    report: dict[str, Any] = {"ok": True, "diagnostics": {}}
    try:
        report["diagnostics"]["user_location"] = user_location_report(transport.read_user_location(host))
    except SeestarError as exc:
        report["ok"] = False
        report["diagnostics"]["user_location"] = {"rpc_ok": False, "error_category": exc.category}
    try:
        report["diagnostics"]["pi_time"] = pi_time_report(transport.read_pi_time(host))
    except SeestarError as exc:
        report["ok"] = False
        report["diagnostics"]["pi_time"] = {"rpc_ok": False, "error_category": exc.category}

    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[INFO] location/time diagnostic written to {out.name}")
    return 0 if report["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Seestar location/time diagnostic.")
    parser.add_argument("--host", required=True, help="telescope address; never written to the report")
    parser.add_argument("--key-env", default="TSNDSS_SEESTAR_KEY_PATH", help="environment variable holding your PEM path")
    parser.add_argument("--out", type=Path, default=Path("seestar_location_time_diagnostic.json"))
    parser.add_argument("--overwrite", action="store_true", help="replace an existing diagnostic file")
    args = parser.parse_args()
    return run(args.host, args.key_env, args.out, args.overwrite)


if __name__ == "__main__":
    sys.exit(main())
