"""Operator-run, attended validation of the REAL Seestar preview (RTSP) through the isolated decoder (DB-03 Wave 5, minimal).

It reads the telescope's state (the same read-only requests as tools/seestar_readonly_validate.py), asks the readiness gate whether a
preview may be opened, and, only if the gate allows it, reads a few frames from the camera stream that is ALREADY running. It never
starts a camera, a view or any mode, never sends a control command (no iscope_start_view or anything else), never moves or arms
anything, and never writes an image: only counts, formats, dimensions, durations and states go into the report. The stream address,
host, key path, serial and pixel data are never printed or written.

Prerequisites (operator):
  * Windows or POSIX, Python >= 3.13 (the isolated decoder refuses older interpreters);
  * OpenCV (opencv-python >= 4.8) installed in THIS interpreter; it is imported only inside the worker process, never in this one;
  * your own PEM key file, its path in the environment variable TSNDSS_SEESTAR_KEY_PATH (TSNDSS neither supplies nor extracts it);
  * the telescope powered, reachable, and the camera view you want to test ALREADY running in the Seestar app, so that the app
    reports that camera's RTSP stream as working. If it is not, the gate says so and NOTHING is opened.

    py -3.13 tools\\seestar_preview_validate.py --host <telescope address> --camera main --frames 5 --out w5_main.json
    py -3.13 tools\\seestar_preview_validate.py --host <telescope address> --camera wide --frames 5 --out w5_wide.json
    py -3.13 tools\\seestar_preview_validate.py --host <telescope address> --camera both --frames 5 --out w5_both.json

Exit code: 0 PASS, 1 FAIL (see the fixed reason tokens in the report), 2 a prerequisite is missing (nothing was contacted).
Stop with Ctrl+C at any time: the streams and workers are closed (bounded) before the script exits.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tsn_dss.engine.device_runtime import ConnectionState, ProviderRuntime  # noqa: E402
from tsn_dss.engine.opencv_isolated_decoder import (  # noqa: E402
    IsolatedDecoderConfig, abandoned_worker_count, build_isolated_preview_manager, live_worker_count, stop_all_workers,
)
from tsn_dss.engine.seestar_preview import SeestarPreviewConfig  # noqa: E402
from tsn_dss.engine.seestar_provider import (  # noqa: E402
    RsaKeyFileAuthenticator, SeestarProvider, SeestarProviderConfig, TcpSeestarTransport,
)

CAMERAS = {"main": ("main",), "wide": ("wide",), "both": ("main", "wide")}


def _stats(values: list) -> dict:
    return {"min": round(min(values), 3), "median": round(statistics.median(values), 3), "max": round(max(values), 3)} if values else {}


def validate_preview(manager, cameras: tuple, frames: int, max_seconds: float) -> dict:
    """Open the selected cameras through the manager (the only way to open a stream), read ``frames`` frames each, close, report.

    Pure with respect to the device: everything it knows comes from the manager, so it is tested offline with a fake runtime."""
    began = time.monotonic()
    rows = {c: {"camera": c, "gate_allowed": None, "gate_reason": None, "opened": False, "open_refusal": None, "error_category": None,
                "frames": 0, "format": None, "width": None, "height": None, "bytes": None, "freshness": [], "distinct_content": 0,
                "open_s": None, "poll_s": {}, "final_state": None, "fail_reasons": []} for c in cameras}
    seen: dict = {c: set() for c in cameras}
    durations: dict = {c: [] for c in cameras}
    workers_during = 0
    cleanup: dict = {}
    try:
        for camera in cameras:
            row, step = rows[camera], time.monotonic()
            outcome = manager.open_stream(camera)
            row["open_s"] = round(time.monotonic() - step, 3)
            row["gate_allowed"] = bool(outcome.decision.allowed) if outcome.decision is not None else None
            row["gate_reason"] = getattr(getattr(outcome.decision, "reason", None), "value", None)
            row["opened"] = bool(outcome.opened)
            row["open_refusal"] = getattr(outcome.refusal, "value", None) if outcome.refusal is not None else None
            row["error_category"] = outcome.error_category
        workers_during = live_worker_count()
        for _ in range(frames):
            for camera in cameras:
                row = rows[camera]
                if not row["opened"] or row["final_state"] == "lost" or time.monotonic() - began > max_seconds:
                    continue
                step = time.monotonic()
                result = manager.poll(camera)
                durations[camera].append(time.monotonic() - step)
                row["final_state"] = getattr(result.state, "value", None)
                if result.error_category:
                    row["error_category"] = result.error_category
                evidence = result.evidence
                if evidence is not None:
                    row["frames"] += 1
                    row["format"], row["width"], row["height"], row["bytes"] = (
                        evidence.pixel_format.value, evidence.width, evidence.height, evidence.byte_length)
                    if evidence.freshness.value not in row["freshness"]:
                        row["freshness"].append(evidence.freshness.value)
                    seen[camera].add(evidence.content_digest)
    finally:
        report = manager.close_all()
        cleanup = {"closed": list(report.closed), "close_errors": [category for _, category in report.errors]}
        cleanup["live_workers"], cleanup["abandoned_workers"] = live_worker_count(), abandoned_worker_count()
    for camera, row in rows.items():
        row["distinct_content"] = len(seen[camera])
        row["poll_s"] = _stats(durations[camera])
        reasons = row["fail_reasons"]
        if not row["gate_allowed"]:
            reasons.append("gate_denied")
        elif not row["opened"]:
            reasons.append("open_failed")
        elif row["frames"] < frames:
            reasons.append("too_few_frames")
        if row["final_state"] == "lost":
            reasons.append("stream_lost")
        row["verdict"] = "FAIL" if reasons else "PASS"
    leaks = cleanup["live_workers"] or cleanup["abandoned_workers"] or cleanup["close_errors"]
    isolated = len(cameras) < 2 or not all(r["opened"] for r in rows.values()) or workers_during == len(cameras)
    overall = {"verdict": "PASS" if all(r["verdict"] == "PASS" for r in rows.values()) and not leaks and isolated else "FAIL",
               "workers_during_run": workers_during, "cleanup": cleanup, "elapsed_s": round(time.monotonic() - began, 1),
               "expected_workers": len(cameras) if all(r["opened"] for r in rows.values()) else None}
    if leaks or not isolated:
        overall["fail_reasons"] = (["cleanup_not_clean"] if leaks else []) + ([] if isolated else ["workers_not_one_per_camera"])
    return {"cameras": list(rows.values()), "overall": overall}


def connect_real(host: str, key_path: str):
    """The read-only connection of tools/seestar_readonly_validate.py. Returns (runtime, connection) or (None, fixed reason token)."""
    config = SeestarProviderConfig(host=host, key_path=key_path)
    runtime = ProviderRuntime(SeestarProvider(config, TcpSeestarTransport(config, authenticator=RsaKeyFileAuthenticator(key_path))))
    discovery = runtime.discover()
    if len(discovery.devices) != 1:
        return None, "discovery_failed"
    connection = runtime.connect(runtime.open_connection(discovery.devices[0]))
    if connection.state is not ConnectionState.CONNECTED:
        return None, "connection_failed"
    if runtime.refresh_evidence(connection) is not ConnectionState.READY:
        runtime.disconnect(connection)
        return None, "evidence_not_ready"
    return runtime, connection


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Attended real-RTSP validation through the isolated decoder (operator-run).")
    parser.add_argument("--host", required=True, help="telescope address; never printed or written to the report")
    parser.add_argument("--camera", required=True, choices=sorted(CAMERAS))
    parser.add_argument("--frames", type=int, default=5, help="frames to read per camera (1-30)")
    parser.add_argument("--max-seconds", type=float, default=60.0, help="budget checked between reads, not a hard timeout")
    parser.add_argument("--key-env", default="TSNDSS_SEESTAR_KEY_PATH")
    parser.add_argument("--out", type=Path, default=Path("seestar_preview_validation.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    key_path = os.environ.get(args.key_env)
    problems = []
    if sys.version_info < (3, 13):
        problems.append("Python 3.13 or newer is required")
    if not 1 <= args.frames <= 30:
        problems.append("--frames must be between 1 and 30")
    if not (key_path and os.path.isfile(key_path)):
        problems.append(f"the PEM key file named by {args.key_env} is missing")
    if importlib.util.find_spec("cv2") is None:
        problems.append("OpenCV (opencv-python >= 4.8) is not installed in this interpreter")
    if args.out.is_dir() or (args.out.exists() and not args.overwrite):
        problems.append("the output file already exists (choose another name or pass --overwrite)")
    if problems:
        for problem in problems:
            print("[PREREQUISITE] " + problem)
        return 2

    result: dict = {}
    runtime = connection = None
    try:
        runtime, connection = connect_real(args.host, key_path)
        if runtime is None:
            result = {"overall": {"verdict": "FAIL", "fail_reasons": [connection]}, "cameras": []}
        else:
            manager = build_isolated_preview_manager(
                runtime=runtime, connection=connection, config=SeestarPreviewConfig(host=args.host, cameras=CAMERAS[args.camera]),
                isolated=IsolatedDecoderConfig())
            result = validate_preview(manager, CAMERAS[args.camera], args.frames, args.max_seconds)
    except KeyboardInterrupt:
        result = {"overall": {"verdict": "FAIL", "fail_reasons": ["interrupted"]}, "cameras": []}
    except Exception as exc:                                  # only the class name: a message could contain the address
        result = {"overall": {"verdict": "FAIL", "fail_reasons": [f"error_{type(exc).__name__}"]}, "cameras": []}
    finally:
        stop_all_workers()
        if runtime is not None and connection is not None:
            try:
                runtime.disconnect(connection)
            except Exception:
                pass
    result["selected"] = args.camera
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for row in result.get("cameras", []):
        print(f"[{row['verdict']}] {row['camera']}: gate_allowed={row['gate_allowed']} opened={row['opened']} frames={row['frames']} "
              f"{row['format']} {row['width']}x{row['height']} reasons={row['fail_reasons']}")
    print(f"[{result['overall']['verdict']}] overall; report written (no image, no address). Physical check (manual): confirm nothing moved.")
    return 0 if result["overall"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
