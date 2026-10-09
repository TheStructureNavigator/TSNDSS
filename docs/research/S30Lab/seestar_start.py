
"""Autonomous Seestar S30 Pro Scenery capture and parking.

Location:
    TSNWorkspace/S30Lab/scripts/seestar_start.py

Run from S30Lab:
    python scripts/seestar_start.py
    python scripts/seestar_start.py --no-capture
    python scripts/seestar_start.py --no-park

Tested individual RPCs on firmware 9.31.
Full combined procedure requires validation on the physical device.
"""

import argparse
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_KEY = (
    WORKSPACE / "seestar_alp" / "firmware-cache" / "interop.pem"
)
DEFAULT_OUTPUT = WORKSPACE / "S30Lab"

CAMERAS = (
    ("View", 4554, "main_autonomous.png"),
    ("SecondView", 4555, "wide_autonomous.png"),
)


def fail(message):
    raise RuntimeError(message)


def result_of(response, label):
    if not isinstance(response, dict) or response.get("code") != 0:
        fail(f"{label}: RPC error: {response!r}")
    return response.get("result")


def read_mount(raw):
    state = result_of(raw.get_device_state(), "get_device_state")
    mount = state.get("mount") if isinstance(state, dict) else None
    if not isinstance(mount, dict):
        fail("Missing mount state; refusing hardware movement")
    return mount


def get_app(raw):
    app = result_of(raw.iscope_get_app_state(), "iscope_get_app_state")
    if not isinstance(app, dict):
        fail("Invalid application state")
    return app


def wait_for_mount(raw, timeout, expected_close):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        mount = read_mount(raw)
        close = mount.get("close")
        movement = mount.get("move_type")

        print(f"[MOUNT] close={close} move_type={movement}")

        if close is expected_close and movement == "none":
            return

        time.sleep(2)

    fail("Mount did not reach requested state before timeout")


def ensure_arm_open(raw, timeout):
    mount = read_mount(raw)
    print(f"[MOUNT] {mount}")

    if mount.get("move_type") != "none":
        fail("Mount already moving; refusing another movement command")

    if mount.get("close") is False:
        print("[PASS] Arm already open")
        return

    if mount.get("close") is not True:
        fail("Unknown arm position; refusing movement")

    print("[ACTION] Opening arm. Ensure physical clearance!")
    result_of(raw.scope_move_to_horizon(), "scope_move_to_horizon")

    wait_for_mount(raw, timeout, expected_close=False)
    print("[PASS] Arm open and stationary")


def cameras_ready(app):
    for name, port, _ in CAMERAS:
        camera = app.get(name) or {}
        rtsp = camera.get("RTSP") or {}

        if not (
            camera.get("mode") == "scenery"
            and camera.get("stage") == "RTSP"
            and camera.get("state") == "working"
            and rtsp.get("state") == "working"
            and rtsp.get("port") == port
        ):
            return False

    return True


def cameras_stopped(app):
    for name, _, _ in CAMERAS:
        camera = app.get(name) or {}
        rtsp = camera.get("RTSP") or {}

        if camera.get("state") not in ("cancel", "complete", "idle"):
            return False

        if rtsp.get("state") not in ("cancel", "complete", "idle"):
            return False

    return True


def ensure_scenery(raw, timeout):
    app = get_app(raw)

    if cameras_ready(app):
        print("[PASS] Scenery already active on MAIN and WIDE")
        return

    print("[ACTION] Starting Scenery MAIN/WIDE")

    command = {
        "method": "iscope_start_view",
        "params": {
            "mode": "scenery",
            "target_ra_dec": [None, None],
            "target_name": "Unknown",
            "lp_filter": False,
            "cam_id": 1,
        },
    }

    response = raw.send_command(command)
    value = result_of(response, "iscope_start_view")

    if value != 0:
        fail(f"Unexpected iscope_start_view result: {value!r}")

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        time.sleep(2)
        app = get_app(raw)

        if cameras_ready(app):
            print("[PASS] MAIN and WIDE RTSP working")
            return

    fail("Both cameras did not reach Scenery/RTSP working state")


def capture(ip, port, destination):
    import cv2

    url = f"rtsp://{ip}:{port}/stream"
    print(f"[CAPTURE] {url}")

    camera = cv2.VideoCapture(url, cv2.CAP_FFMPEG)

    try:
        if not camera.isOpened():
            fail(f"Cannot open RTSP stream: {url}")

        ok, frame = camera.read()

        if not ok or frame is None:
            fail(f"Cannot read RTSP frame: {url}")

        destination.parent.mkdir(parents=True, exist_ok=True)

        if not cv2.imwrite(str(destination), frame):
            fail(f"Cannot save image: {destination}")

        print(
            f"[PASS] Port={port} shape={frame.shape} "
            f"saved={destination}"
        )

    finally:
        camera.release()


def stop_scenery(raw, timeout):
    print("[ACTION] Stopping Scenery")

    app = get_app(raw)

    if cameras_stopped(app):
        print("[PASS] Both cameras already stopped")
        return

    result_of(raw.iscope_stop_view(), "iscope_stop_view")

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        time.sleep(2)
        app = get_app(raw)

        if cameras_stopped(app):
            print("[PASS] Both cameras stopped")
            return

    fail("Cameras did not confirm stopped state")


def park(raw, timeout):
    print("[ACTION] Checking mount before parking")

    mount = read_mount(raw)

    if mount.get("move_type") != "none":
        fail("Mount moving; refusing parking command")

    if mount.get("close") is True:
        print("[PASS] Telescope already parked")
        return

    if mount.get("close") is not False:
        fail("Unknown arm state; refusing parking command")

    app = get_app(raw)

    if not cameras_stopped(app):
        fail("Cameras still active; refusing parking command")

    print("[ACTION] Parking telescope. Ensure clearance!")

    result_of(raw.scope_park(), "scope_park")

    wait_for_mount(raw, timeout, expected_close=True)

    print("[PASS] Telescope PARKED")


def main():
    parser = argparse.ArgumentParser(
        description="Seestar S30 Pro autonomous capture and parking"
    )

    parser.add_argument(
        "--ip",
        default="10.160.154.146",
        help="Seestar IP for RTSP; RPC uses seestarpy discovery",
    )
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--no-capture", action="store_true")
    parser.add_argument("--no-park", action="store_true")

    args = parser.parse_args()

    if args.timeout <= 0:
        fail("--timeout must be positive")

    if not args.key.is_file():
        fail(f"RSA key missing: {args.key}")

    from seestarpy import auth, raw

    auth.set_key_path(str(args.key.resolve()))

    print("[STEP 1] Connecting to Seestar")
    mount = read_mount(raw)
    print(
        f"[PASS] Mount: close={mount.get('close')} "
        f"move_type={mount.get('move_type')}"
    )

    session_error = None

    try:
        print("[STEP 2] Opening arm")
        ensure_arm_open(raw, args.timeout)

        print("[STEP 3] Starting Scenery")
        ensure_scenery(raw, args.timeout)

        print("[STEP 4] Verifying stationary mount")
        mount = read_mount(raw)

        if (
            mount.get("close") is not False
            or mount.get("move_type") != "none"
        ):
            fail(f"Unexpected mount state: {mount}")

        print("[PASS] Mount stationary")

        if args.no_capture:
            print("[SKIP] Image capture disabled")
        else:
            print("[STEP 5] Capturing MAIN and WIDE")

            for _, port, filename in CAMERAS:
                capture(
                    args.ip,
                    port,
                    args.output / filename,
                )

        print("[PASS] Capture phase complete")

    except BaseException as exc:
        session_error = exc
        print(f"[SESSION ERROR] {exc}", file=sys.stderr)

    finally:
        print("[STEP 6] Cleanup")

        try:
            stop_scenery(raw, args.timeout)

            if args.no_park:
                print("[SKIP] Parking disabled (--no-park)")
            else:
                print("[STEP 7] Parking")
                park(raw, args.timeout)

        except Exception as cleanup_error:
            print(
                f"[CLEANUP ERROR] {cleanup_error}",
                file=sys.stderr,
            )

            if session_error is None:
                session_error = cleanup_error

    if session_error is not None:
        raise session_error

    if args.no_park:
        print("[PASS] SESSION COMPLETE — ARM LEFT OPEN")
    else:
        print("[PASS] SESSION COMPLETE — TELESCOPE PARKED")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("[STOP] Interrupted", file=sys.stderr)
        sys.exit(130)

    except Exception as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
