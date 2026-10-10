"""Operator-run, EXPLICITLY PHYSICAL end-to-end validation: arm, scenery, isolated RTSP preview, stop, park (DB-03 Wave 5).

WARNING: this tool MOVES THE ARM and STARTS THE CAMERAS. It is the only TSNDSS path that does, it is separate from the read-only
provider (which has no control surface and stays that way) and from tools/seestar_preview_validate.py (read-only). Importing this module,
running its tests and running it without the opt-in do nothing to a device and open no connection.

Sequence (reference behaviour: docs/research/S30Lab/seestar_start.py; no manufacturer app is needed):
    CONNECT -> DEPLOY ARM -> START SCENERY -> VERIFY MAIN/WIDE READY -> TEST ISOLATED PREVIEW FRAMES -> STOP SCENERY -> PARK ARM
Commands sent (exactly these four, nothing else; fixed parameters): scope_move_to_horizon, iscope_start_view (mode scenery),
iscope_stop_view, scope_park. Everything else is a read through the existing read-only transport. Every command is guarded by
fail-closed state checks and followed by a bounded wait for the state it should cause.

Cleanup always runs once the device state was readable: scenery is stopped (and confirmed), and the arm is parked ONLY if both cameras
are confirmed stopped and the mount is stationary and open. If that cannot be confirmed the tool refuses to park and reports an UNSAFE
or UNKNOWN state prominently (exit code 3). A run is never reported as PASS when cleanup did not complete.

Operator opt-in (all required): --allow-physical-motion, an interactive terminal, and typing DEPLOY at the prompt. Clear the arm's
path and the cameras' field first.

    set TSNDSS_SEESTAR_KEY_PATH=<path to your own PEM file>
    py -3.13 tools\\seestar_e2e_validate.py --host <telescope address> --allow-physical-motion --frames 3 --out e2e.json

Needs Python >= 3.13, OpenCV in this interpreter (used only inside the isolated worker), your own PEM key. The report holds stage names,
statuses, durations, fixed tokens, camera/mount state words and frame counts/formats/dimensions only: no address, host, key path, serial,
raw device payload or pixel data. Exit codes: 0 PASS, 1 FAIL (safe state confirmed), 2 prerequisite or opt-in missing (nothing contacted),
3 FAIL with an unsafe or unknown final state, 130 interrupted.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(Path(__file__).resolve().parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tsn_dss.engine.seestar_provider.auth import Authenticator, RsaKeyFileAuthenticator, perform_handshake, require_mapping  # noqa: E402
from tsn_dss.engine.seestar_provider.errors import SeestarError  # noqa: E402
from tsn_dss.engine.seestar_provider.protocol import MAX_SKIPPED_FRAMES, decode_frame, split_frames  # noqa: E402

CONFIRM_PHRASE = "DEPLOY"
TCP_PORT = 4700
CAMERAS = (("main", "View", 4554), ("wide", "SecondView", 4555))        # label, app-state key, expected RTSP port
SCENERY_PARAMS = {"mode": "scenery", "target_ra_dec": [None, None], "target_name": "Unknown", "lp_filter": False, "cam_id": 1}
# The ONLY commands this tool can encode. Parameters are fixed here; callers cannot supply any.
CONTROL = {"scope_move_to_horizon": None, "iscope_start_view": SCENERY_PARAMS, "iscope_stop_view": None, "scope_park": None}
STOPPED = ("cancel", "complete", "idle")
STAGES = ("connect", "deploy_arm", "start_scenery", "verify_ready", "preview", "stop_scenery", "park_arm")


class E2EError(Exception):
    """A failure with a fixed token as its only content."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


# --- explicit physical permission ---------------------------------------------------------------------------------

_TOKEN = object()


class PhysicalPermit:
    """Proof that the operator opted in. Only ``grant_permit`` can create one; the control channel refuses to work without it."""

    def __init__(self, token: object) -> None:
        if token is not _TOKEN:
            raise E2EError("permit_cannot_be_constructed")


def grant_permit(allow_flag: bool, confirm: Callable[[], str]) -> PhysicalPermit:
    if not allow_flag:
        raise E2EError("physical_motion_not_allowed")
    if confirm().strip() != CONFIRM_PHRASE:
        raise E2EError("confirmation_not_given")
    return PhysicalPermit(_TOKEN)


# --- the control channel: four commands, authenticated, one short-lived connection each -----------------------------------------


class ControlChannel:
    def __init__(self, host: str, authenticator: Authenticator, permit: PhysicalPermit | None, *, timeout_s: float = 8.0,
                 connect: Callable[[tuple, float], Any] | None = None, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._host, self._auth, self._permit, self._timeout = host, authenticator, permit, timeout_s
        self._connect = connect or (lambda addr, timeout: socket.create_connection(addr, timeout))
        self._monotonic = monotonic
        self._next_id = 100

    def __repr__(self) -> str:
        return "ControlChannel(<redacted>)"

    def send(self, method: str) -> Any:
        """Send one allow-listed command and return its ``result``. Raises ``E2EError`` with a fixed token on any problem."""
        if not isinstance(self._permit, PhysicalPermit):
            raise E2EError("no_physical_permit")                      # before any socket exists
        if method not in CONTROL:
            raise E2EError("command_not_allowed")
        params = CONTROL[method]
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "verify": True, "method": method}
        if params is not None:
            message["params"] = dict(params)
        deadline = self._monotonic() + self._timeout
        try:
            sock = self._connect((self._host, TCP_PORT), self._timeout)
        except OSError:
            raise E2EError("connect_failed") from None
        state = {"buffer": b"", "skipped": 0}
        try:
            sock.settimeout(self._timeout)

            def exchange(frame: dict) -> Any:
                sock.sendall(json.dumps(frame).encode("utf-8") + b"\r\n")
                return require_mapping(self._read(sock, frame["id"], state, deadline))

            try:
                perform_handshake(exchange, self._auth)
                sock.sendall(json.dumps(message).encode("utf-8") + b"\r\n")
                reply = self._read(sock, message["id"], state, deadline)
            except SeestarError:
                raise E2EError("auth_or_protocol_failed") from None
            except OSError:
                raise E2EError("connection_lost") from None
        finally:
            try:
                sock.close()
            except OSError:
                pass
        code = reply.get("code")
        if code != 0:
            raise E2EError(f"rpc_error_{method}")
        return reply.get("result")

    def _read(self, sock: Any, want_id: int, state: dict, deadline: float) -> dict:
        while True:
            frames, state["buffer"] = split_frames(state["buffer"])
            for raw in frames:
                frame = decode_frame(raw)
                if frame is None:
                    continue
                if frame.get("id") == want_id and not isinstance(frame.get("id"), bool):
                    return frame
                state["skipped"] += 1
                if state["skipped"] > MAX_SKIPPED_FRAMES:
                    raise E2EError("too_many_events")
            if self._monotonic() >= deadline:
                raise E2EError("read_timeout")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                raise E2EError("read_timeout") from None
            if not chunk:
                raise E2EError("connection_closed")
            state["buffer"] += chunk


# --- reads (the existing read-only transport) ----------------------------------------------------------------------------------


class DeviceReader:
    """Mount and camera state as plain words, from the existing read-only transport. Nothing else leaves this class."""

    def __init__(self, transport: Any, host: str) -> None:
        self._transport, self._host = transport, host

    def mount(self) -> dict:
        try:
            reply = self._transport.read_device_state(self._host, ["mount"])
        except SeestarError:
            raise E2EError("mount_unreadable") from None
        state = reply.result.get("mount") if reply.code == 0 and isinstance(reply.result, dict) else None
        if not isinstance(state, dict):
            raise E2EError("mount_unreadable")
        return {"close": state.get("close"), "move_type": state.get("move_type")}

    def app(self) -> dict:
        try:
            reply = self._transport.read_app_state(self._host)
        except SeestarError:
            raise E2EError("app_state_unreadable") from None
        if reply.code != 0 or not isinstance(reply.result, dict):
            raise E2EError("app_state_unreadable")
        out = {}
        for label, key, _ in CAMERAS:
            view = reply.result.get(key) or {}
            rtsp = view.get("RTSP") or {}
            out[label] = {"mode": view.get("mode"), "stage": view.get("stage"), "state": view.get("state"),
                          "rtsp_state": rtsp.get("state"), "port": rtsp.get("port")}
        return out


def cameras_ready(app: dict) -> bool:
    return all(app.get(label, {}).get("mode") == "scenery" and app[label].get("stage") == "RTSP" and app[label].get("state") == "working"
               and app[label].get("rtsp_state") == "working" and app[label].get("port") == port for label, _, port in CAMERAS)


def cameras_stopped(app: dict) -> bool:
    return all(app.get(label, {}).get("state") in STOPPED and app[label].get("rtsp_state") in STOPPED for label, _, _ in CAMERAS)


def arm_word(mount: dict | None) -> str:
    if not mount or mount.get("move_type") != "none":
        return "moving_or_unknown"
    return {True: "parked", False: "open"}.get(mount.get("close"), "moving_or_unknown")


# --- the sequence -------------------------------------------------------------------------------------------------------------------


class Run:
    def __init__(self, reader: Any, ctl: Any, preview: Callable[[int], dict], *, timeout: float, max_total: float, poll: float,
                 frames: int, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self.reader, self.ctl, self.preview, self.timeout, self.max_total, self.poll, self.frames = reader, ctl, preview, timeout, max_total, poll, frames
        self.clock, self.sleep, self.began = clock, sleep, clock()
        self.stages = {name: {"name": name, "status": "NOT_RUN", "seconds": None, "detail": None} for name in STAGES}
        self.commands: list = []
        self.readable = False
        self.arm_open_at_start = None
        self.scenery_active_at_start = None
        self.interrupted = False
        self.preview_summary: list = []
        self.cleanup_interrupted = False

    # ---- helpers
    def over_budget(self) -> bool:
        return self.clock() - self.began > self.max_total

    def command(self, method: str) -> Any:
        self.commands.append(method)
        return self.ctl.send(method)

    def wait(self, condition: Callable[[], bool], limit: float | None = None) -> bool:
        deadline = self.clock() + (self.timeout if limit is None else limit)
        while True:
            if condition():
                return True
            if self.clock() >= deadline or self.over_budget():
                return False
            self.sleep(self.poll)

    def stage(self, name: str, body: Callable[[], str]) -> bool:
        record, began = self.stages[name], self.clock()
        try:
            record["detail"] = body()
            record["status"] = "PASS"
        except E2EError as exc:
            record["status"], record["detail"] = "FAIL", exc.token
        except Exception as exc:                                        # only the class name: a message could carry data
            record["status"], record["detail"] = "FAIL", f"error_{type(exc).__name__}"
        record["seconds"] = round(self.clock() - began, 2)
        return record["status"] == "PASS"

    # ---- steps
    def connect(self) -> str:
        mount, app = self.reader.mount(), self.reader.app()
        self.readable = True
        self.arm_open_at_start, self.scenery_active_at_start = arm_word(mount) == "open", cameras_ready(app)
        return f"arm_{arm_word(mount)}"

    def deploy(self) -> str:
        mount = self.reader.mount()
        if mount.get("move_type") != "none":
            raise E2EError("mount_moving")
        if mount.get("close") is False:
            return "already_open"
        if mount.get("close") is not True:
            raise E2EError("arm_state_unknown")
        self.command("scope_move_to_horizon")
        if not self.wait(lambda: arm_word(self.reader.mount()) == "open"):
            raise E2EError("arm_did_not_open")
        return "opened"

    def start(self) -> str:
        if arm_word(self.reader.mount()) != "open":
            raise E2EError("arm_not_open_and_stationary")
        if cameras_ready(self.reader.app()):
            return "already_active"
        if self.command("iscope_start_view") != 0:
            raise E2EError("start_view_not_accepted")
        return "started"

    def verify(self) -> str:
        if not self.wait(lambda: cameras_ready(self.reader.app())):
            raise E2EError("cameras_not_ready")
        if arm_word(self.reader.mount()) != "open":
            raise E2EError("mount_not_stationary_open")
        return "both_ready"

    def run_preview(self) -> str:
        report = self.preview(self.frames)
        self.preview_summary = [{k: row.get(k) for k in ("camera", "frames", "format", "width", "height", "verdict", "fail_reasons")}
                                for row in report.get("cameras", [])]
        verdict = report.get("overall", {}).get("verdict")
        if verdict != "PASS":
            raise E2EError("preview_" + "_".join(report.get("overall", {}).get("fail_reasons", ["failed"])))
        return "frames_received"

    def stop(self) -> str:
        if cameras_stopped(self.reader.app()):
            return "already_stopped"
        self.command("iscope_stop_view")
        if not self.wait(lambda: cameras_stopped(self.reader.app())):
            raise E2EError("cameras_did_not_confirm_stopped")
        return "stopped"

    def park(self) -> str:
        mount = self.reader.mount()
        if mount.get("move_type") != "none":
            raise E2EError("refused_mount_moving")
        if mount.get("close") is True:
            return "already_parked"
        if mount.get("close") is not False:
            raise E2EError("refused_arm_state_unknown")
        if not cameras_stopped(self.reader.app()):
            raise E2EError("refused_cameras_not_confirmed_stopped")
        self.command("scope_park")
        if not self.wait(lambda: arm_word(self.reader.mount()) == "parked"):
            raise E2EError("arm_did_not_park")
        return "parked"

    # ---- orchestration
    def execute(self) -> dict:
        try:
            main = self.stage("connect", self.connect)
            if main:
                for name, body in (("deploy_arm", self.deploy), ("start_scenery", self.start), ("verify_ready", self.verify), ("preview", self.run_preview)):
                    if self.over_budget():
                        self.stages[name]["status"], self.stages[name]["detail"] = "FAIL", "time_budget_used_up"
                        break
                    if not self.stage(name, body):
                        break
        except KeyboardInterrupt:
            self.interrupted = True
        finally:
            if self.readable and (self.commands or self.stages["deploy_arm"]["status"] == "PASS"):
                try:                                                       # cleanup runs once this tool owns the session (it acted, or its first checks passed)
                    self.stage("stop_scenery", self.stop)
                    self.stage("park_arm", self.park)
                except KeyboardInterrupt:
                    self.cleanup_interrupted = True
        return self.report()

    def owns_session(self) -> bool:
        return bool(self.commands) or self.stages["deploy_arm"]["status"] == "PASS"

    def final_state(self) -> dict:
        if not self.readable:
            return {"arm": "never_read", "cameras_stopped": None}
        try:
            return {"arm": arm_word(self.reader.mount()), "cameras_stopped": cameras_stopped(self.reader.app())}
        except (E2EError, KeyboardInterrupt, Exception):
            return {"arm": "UNKNOWN", "cameras_stopped": None}

    def report(self) -> dict:
        final = self.final_state()
        # A run that never changed anything (a precondition failed first) leaves the device as found; it is not an unsafe state.
        unsafe = self.owns_session() and (final["arm"] != "parked" or final["cameras_stopped"] is not True or self.cleanup_interrupted)
        passed = (all(s["status"] == "PASS" for s in self.stages.values()) and not unsafe and not self.interrupted
                  and not self.cleanup_interrupted and final["arm"] == "parked")
        return {
            "overall": "PASS" if passed else "FAIL",
            "unsafe_or_unknown_final_state": unsafe,
            "device_left_as_found": not self.owns_session(),
            "interrupted": self.interrupted or self.cleanup_interrupted,
            "stages": list(self.stages.values()),
            "commands_sent": list(self.commands),
            "arm_open_at_start": self.arm_open_at_start,
            "scenery_active_at_start": self.scenery_active_at_start,
            "final_state": final,
            "preview": self.preview_summary,
            "elapsed_s": round(self.clock() - self.began, 1),
        }


def run_sequence(reader: Any, ctl: Any, preview: Callable[[int], dict], *, timeout: float = 90.0, max_total: float = 600.0, poll: float = 2.0,
                 frames: int = 3, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict:
    return Run(reader, ctl, preview, timeout=timeout, max_total=max_total, poll=poll, frames=frames, clock=clock, sleep=sleep).execute()


# --- command line -------------------------------------------------------------------------------------------------------------------


def _real_preview(host: str, key_path: str) -> Callable[[int], dict]:
    import seestar_preview_validate as PV                                  # same folder; imports no network code at import time
    from tsn_dss.engine.opencv_isolated_decoder import IsolatedDecoderConfig, build_isolated_preview_manager
    from tsn_dss.engine.seestar_preview import SeestarPreviewConfig

    def preview(frames: int) -> dict:
        runtime, connection = PV.connect_real(host, key_path)
        if runtime is None:
            return {"overall": {"verdict": "FAIL", "fail_reasons": [connection]}, "cameras": []}
        try:
            manager = build_isolated_preview_manager(runtime=runtime, connection=connection, isolated=IsolatedDecoderConfig(),
                                                     config=SeestarPreviewConfig(host=host, cameras=("main", "wide")))
            return PV.validate_preview(manager, ("main", "wide"), frames, 60.0)
        finally:
            try:
                runtime.disconnect(connection)
            except Exception:
                pass

    return preview


def main(argv=None, *, confirm: Callable[[], str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PHYSICAL end-to-end validation: arm, scenery, isolated preview, stop, park (operator-run).")
    parser.add_argument("--host", required=True, help="telescope address; never printed or written to the report")
    parser.add_argument("--allow-physical-motion", action="store_true", help="REQUIRED: the arm will move and the cameras will start")
    parser.add_argument("--frames", type=int, default=3, help="frames per camera in the preview stage (1-30)")
    parser.add_argument("--timeout", type=float, default=90.0, help="bounded wait for each device state (seconds)")
    parser.add_argument("--max-total", type=float, default=600.0, help="overall budget checked between steps, not a hard timeout")
    parser.add_argument("--key-env", default="TSNDSS_SEESTAR_KEY_PATH")
    parser.add_argument("--out", type=Path, default=Path("seestar_e2e_validation.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    key_path = os.environ.get(args.key_env)
    problems = []
    if not args.allow_physical_motion:
        problems.append("--allow-physical-motion is required: this tool moves the arm and starts the cameras")
    if confirm is None and not (sys.stdin and sys.stdin.isatty()):
        problems.append("an interactive terminal is required for the confirmation prompt")
    if sys.version_info < (3, 13):
        problems.append("Python 3.13 or newer is required")
    if not 1 <= args.frames <= 30 or not 5 <= args.timeout <= 600 or not 30 <= args.max_total <= 3600:
        problems.append("--frames 1-30, --timeout 5-600, --max-total 30-3600")
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

    print("This will MOVE THE ARM and START THE CAMERAS. Make sure the arm's path and the field of view are clear.")
    try:
        permit = grant_permit(args.allow_physical_motion, confirm or (lambda: input(f"Type {CONFIRM_PHRASE} to continue: ")))
    except (E2EError, EOFError):
        print("[PREREQUISITE] confirmation not given; nothing was contacted")
        return 2

    from tsn_dss.engine.seestar_provider import SeestarProviderConfig, TcpSeestarTransport
    config = SeestarProviderConfig(host=args.host, key_path=key_path)
    auth = RsaKeyFileAuthenticator(key_path)
    reader = DeviceReader(TcpSeestarTransport(config, authenticator=auth), args.host)
    ctl = ControlChannel(args.host, auth, permit)
    result = run_sequence(reader, ctl, _real_preview(args.host, key_path), timeout=args.timeout, max_total=args.max_total, frames=args.frames)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for stage in result["stages"]:
        print(f"[{stage['status']}] {stage['name']}: {stage['detail']}")
    final = result["final_state"]
    print(f"[{result['overall']}] overall; arm={final['arm']} cameras_stopped={final['cameras_stopped']}; report written (no address, no image).")
    if result["unsafe_or_unknown_final_state"]:
        print("*** UNSAFE OR UNKNOWN STATE: the arm may be open and/or a camera may still be running. CHECK THE TELESCOPE NOW. ***")
        return 3
    if result["interrupted"]:
        return 130
    return 0 if result["overall"] == "PASS" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[STOP] interrupted before the sequence started", file=sys.stderr)
        sys.exit(130)
