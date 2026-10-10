"""Operator-run, supervised DB-05 validation of the four Seestar commands through the DB-04 Safe Command Runtime.

WARNING: with --allow-physical-motion this tool MOVES THE ARM and STARTS THE CAMERAS. Without it the tool is READ-ONLY: it attaches,
reads the arm and camera state once, reports it and sends nothing. Importing this module, running its tests and running it without the
opt-in contact no device that is not asked for and never send a command.

Everything goes through ``SeestarControl`` (the composition root) and its ``CommandDriver``. This tool never touches the control
transport, never names a wire method and cannot supply a parameter: the four commands are the allow-listed kind ids, and each is
admitted, gated, submitted, polled and verified by the DB-04 executor. It never retries a physical command, never recovers or clears an
uncertainty, and passes no clearance authorizer, so an uncertain outcome stays open and is reported (exit 3).

Physical mode needs ALL of: --allow-physical-motion, an interactive terminal, and typing DEPLOY. The permit (tsn_dss ... OperatorPermit) is
in memory only, bound to the one device that was identified, valid for --permit-validity seconds, and the arm moves (deploy, park) each
need a further ARM confirmation. Clear the arm's path and the cameras' field first.

Sequence (stop at the first failure; cleanup only when the run itself sent a command and no uncertainty is open):
    IDENTIFY -> BASELINE -> DEPLOY ARM -> START SCENERY -> PREVIEW (DB-03 isolated manager, read-only) -> STOP SCENERY -> PARK ARM
The starting state must be: arm folded, mount stationary, all camera items stopped. Anything else is refused (nothing is sent).

There are NO timing defaults: every window, deadline and interval comes from you. These values are not hardware-calibrated.

    set TSNDSS_SEESTAR_KEY_PATH=<path to your own PEM file>
    py -3.13 tools\\seestar_command_validate.py --host <addr> --operator <name> --telemetry-max-age S --capability-max-age S          # read-only
    py -3.13 tools\\seestar_command_validate.py --host <addr> --operator <name> --telemetry-max-age S --capability-max-age S \\
        --allow-physical-motion --command-deadline S --poll-interval S --permit-validity S --frames N --preview-max-seconds S --out r.json

The report holds stage names, statuses, fixed tokens, command kind ids, outcome classifications and state words only: no address, host,
key path, serial, device payload, command id or pixel data. Exit codes: 0 PASS (or read-only report), 1 FAIL with the device left as found
or confirmed safe, 2 prerequisite or opt-in missing (nothing sent), 3 unsafe or unknown final state / recovery required, 130 interrupted.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(Path(__file__).resolve().parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tsn_dss.engine.device_runtime.errors import DeviceRuntimeError  # noqa: E402
from tsn_dss.engine.device_runtime.support import utc_now  # noqa: E402
from tsn_dss.engine.seestar_control import (  # noqa: E402
    ARM_DEPLOY, ARM_PARK, ARM_PHRASE, GRANT_PHRASE, SCENERY_START, SCENERY_STOP, ControlFreshness, OperatorPermit, SeestarControl,
    SeestarControlTransport,
)
from tsn_dss.engine.seestar_control.states import (  # noqa: E402
    CAMERA_ITEMS, arm_closed, arm_stationary, cameras_ready, cameras_stopped_count,
)

STAGES = ("identify", "baseline", "deploy_arm", "start_scenery", "preview", "stop_scenery", "park_arm")


class Unsafe(Exception):
    """A stage failed with a fixed token as its only content."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


# --- state words (plain words from one telemetry sample; nothing else leaves this function) -------------------------------------


def state_words(sample: Any) -> dict:
    stationary, closed = arm_stationary(sample), arm_closed(sample)
    if stationary is not True or closed is None:
        arm = "moving_or_unknown"
    else:
        arm = "closed" if closed else "open"
    stopped = cameras_stopped_count(sample)
    if stopped is None:
        cameras = "unknown"
    elif stopped == len(CAMERA_ITEMS):
        cameras = "stopped"
    elif cameras_ready(sample):
        cameras = "ready"
    else:
        cameras = "other"
    return {"arm": arm, "cameras": cameras}


# --- the supervised run ------------------------------------------------------------------------------------------------------------


class Run:
    def __init__(self, control: SeestarControl, permit: OperatorPermit | None, *, operator: str, physical: bool, ask: Callable[[str], str],
                 preview: Callable[[int], dict] | None, frames: int, deadline: timedelta, poll_interval: timedelta,
                 clock: Callable[[], Any]) -> None:
        self.control, self.permit, self.operator, self.physical, self.ask = control, permit, operator, physical, ask
        self.preview, self.frames, self.deadline, self.poll, self.clock = preview, frames, deadline, poll_interval, clock
        self.began = clock()
        self.stages = {n: {"name": n, "status": "NOT_RUN", "detail": None} for n in STAGES}
        self.handle: Any = None
        self.commands: list[str] = []
        self.outcomes: list[dict] = []
        self.initial: dict | None = None
        self.interrupted = self.cleanup_interrupted = False
        self.recovery_required = False
        self.cleanup = "not_needed"
        self.preview_summary: list = []

    # ---- helpers
    def words(self) -> dict:
        try:
            return state_words(self.control.runtime.read_telemetry(self.handle.connection))
        except Exception:
            raise Unsafe("state_unreadable") from None

    def stage(self, name: str, body: Callable[[], str]) -> bool:
        record = self.stages[name]
        try:
            record["detail"], record["status"] = body(), "PASS"
        except Unsafe as exc:
            record["status"], record["detail"] = "FAIL", exc.token
        except DeviceRuntimeError as exc:
            record["status"], record["detail"] = "FAIL", str(exc)[:60] if _is_token(str(exc)) else f"error_{type(exc).__name__}"
        except Exception as exc:  # only the class name: a message could carry data
            record["status"], record["detail"] = "FAIL", f"error_{type(exc).__name__}"
        return record["status"] == "PASS"

    def command(self, kind_id: str) -> None:
        """One command through the driver. Raises ``Unsafe`` unless it succeeded; an open uncertainty closes the cleanup path."""
        if kind_id in (ARM_DEPLOY, ARM_PARK):
            self.permit.confirm_arm_motion(kind_id, lambda: self.ask(f"Type {ARM_PHRASE} to allow this arm movement ({kind_id}): "))
        self.commands.append(kind_id)
        outcome = self.handle.driver.execute(
            self.handle.connection, kind_id, self.operator, deadline=self.clock() + self.deadline, poll_interval=self.poll)
        self.outcomes.append({"kind": kind_id, "classification": outcome.classification, "final_state": outcome.final_state,
                              "submitted": outcome.submitted, "polls": outcome.polls, "uncertainty_open": outcome.uncertainty_open})
        if outcome.uncertainty_open:
            self.recovery_required = True
        if not outcome.succeeded:
            raise Unsafe(f"command_{outcome.classification}")

    # ---- steps
    def identify(self) -> str:
        self.handle = self.control.attach()
        self.initial = self.words()
        return f"arm_{self.initial['arm']}_cameras_{self.initial['cameras']}"

    def baseline(self) -> str:
        if self.initial != {"arm": "closed", "cameras": "stopped"}:
            raise Unsafe("start_state_not_folded_and_stopped")
        self.handle.executor.establish_baseline_by_recovery(self.handle.connection)
        return "established_by_recovery"

    def deploy(self) -> str:
        self.command(ARM_DEPLOY)
        return "opened"

    def start(self) -> str:
        self.command(SCENERY_START)
        return "ready"

    def run_preview(self) -> str:
        if self.preview is None:
            return "skipped_by_operator"
        report = self.preview(self.frames)
        self.preview_summary = [{k: row.get(k) for k in ("camera", "frames", "format", "width", "height", "verdict", "fail_reasons")}
                                for row in report.get("cameras", [])]
        verdict = report.get("overall", {}).get("verdict")
        if verdict != "PASS":
            raise Unsafe("preview_" + "_".join(report.get("overall", {}).get("fail_reasons", ["failed"])))
        return "frames_received"

    def stop(self) -> str:
        if self.words()["cameras"] == "stopped":
            return "already_stopped"
        self.command(SCENERY_STOP)
        return "stopped"

    def park(self) -> str:
        words = self.words()
        if words["arm"] == "closed":
            return "already_parked"
        if words["arm"] != "open":
            raise Unsafe("refused_arm_state_unknown")
        if words["cameras"] != "stopped":
            raise Unsafe("refused_cameras_not_confirmed_stopped")
        self.command(ARM_PARK)
        return "parked"

    # ---- orchestration
    def execute(self) -> dict:
        try:
            if self.stage("identify", self.identify) and self.physical:
                self.consent()
                for name, body in (("baseline", self.baseline), ("deploy_arm", self.deploy), ("start_scenery", self.start),
                                   ("preview", self.run_preview)):
                    if not self.stage(name, body):
                        break
        except KeyboardInterrupt:
            self.interrupted = True
            if len(self.outcomes) < len(self.commands):  # a command was in flight: its result is not known, so no cleanup
                self.recovery_required = True
        finally:
            if self.physical and self.handle is not None and self.commands:
                self.run_cleanup()
        return self.report()

    def consent(self) -> None:
        """The permit is granted here, after the device is identified and before any command can be admitted."""
        interactive = self.ask is not None
        try:
            self.permit.grant(self.handle.connection, allow_physical_motion=True, interactive=interactive,
                              confirm=lambda: self.ask(f"Type {GRANT_PHRASE} to allow the arm to move and the cameras to start: "))
        except Exception as exc:
            self.stages["baseline"]["status"], self.stages["baseline"]["detail"] = "FAIL", f"permit_{getattr(exc, 'args', ['denied'])[0]}"
            raise _Stop() from None

    def run_cleanup(self) -> None:
        if self.recovery_required:
            self.cleanup = "not_attempted_recovery_required"
            return
        self.cleanup = "attempted"
        try:
            self.stage("stop_scenery", self.stop)
            if self.stages["stop_scenery"]["status"] == "PASS":
                self.stage("park_arm", self.park)
        except KeyboardInterrupt:
            self.cleanup_interrupted = True

    def owns_session(self) -> bool:
        return bool(self.commands)

    def final_state(self) -> dict:
        if self.handle is None:
            return {"arm": "never_read", "cameras": "never_read"}
        try:
            return self.words()
        except Exception:
            return {"arm": "unknown", "cameras": "unknown"}

    def report(self) -> dict:
        final = self.final_state()
        if not self.physical:
            overall = "READ_ONLY" if self.stages["identify"]["status"] == "PASS" else "FAIL"
            return self._body(overall, False, final)
        unsafe = self.owns_session() and (final != {"arm": "closed", "cameras": "stopped"} or self.recovery_required or self.cleanup_interrupted)
        passed = (all(s["status"] == "PASS" for s in self.stages.values()) and not unsafe and not self.interrupted
                  and final == {"arm": "closed", "cameras": "stopped"})
        return self._body("PASS" if passed else "FAIL", unsafe, final)

    def _body(self, overall: str, unsafe: bool, final: dict) -> dict:
        return {
            "overall": overall,
            "mode": "physical" if self.physical else "read_only",
            "unsafe_or_unknown_final_state": unsafe,
            "recovery_required": self.recovery_required,
            "device_left_as_found": not self.owns_session(),
            "interrupted": self.interrupted or self.cleanup_interrupted,
            "stages": list(self.stages.values()),
            "commands_sent": list(self.commands),
            "outcomes": self.outcomes,
            "initial_state": self.initial,
            "final_state": final,
            "cleanup": self.cleanup,
            "preview": self.preview_summary,
            "elapsed_s": round((self.clock() - self.began).total_seconds(), 1),
        }


class _Stop(Exception):
    """Stops the main sequence after a refused permit. Nothing was sent."""


def _is_token(text: str) -> bool:
    return bool(text) and all(c.islower() or c.isdigit() or c == "_" for c in text)


def run_validation(control: SeestarControl, permit: OperatorPermit | None, *, operator: str, physical: bool, ask: Callable[[str], str] | None,
                   preview: Callable[[int], dict] | None = None, frames: int = 0, deadline: timedelta = timedelta(0),
                   poll_interval: timedelta = timedelta(0), clock: Callable[[], Any] = utc_now) -> dict:
    """Run the supervised sequence (or the read-only report) and return the JSON-ready report. Every timing value is the caller's."""
    run = Run(control, permit, operator=operator, physical=physical, ask=ask, preview=preview, frames=frames, deadline=deadline,
              poll_interval=poll_interval, clock=clock)
    try:
        return run.execute()
    except _Stop:
        return run.report()


# --- command line -------------------------------------------------------------------------------------------------------------------


def _real_preview(host: str, key_path: str) -> Callable[[int], dict]:
    import seestar_preview_validate as PV  # same folder; the DB-03 tool, no decoder of our own
    from tsn_dss.engine.opencv_isolated_decoder import IsolatedDecoderConfig, build_isolated_preview_manager
    from tsn_dss.engine.seestar_preview import SeestarPreviewConfig

    def preview(frames: int, max_seconds: float) -> dict:
        runtime, connection = PV.connect_real(host, key_path)
        if runtime is None:
            return {"overall": {"verdict": "FAIL", "fail_reasons": [connection]}, "cameras": []}
        try:
            manager = build_isolated_preview_manager(runtime=runtime, connection=connection, isolated=IsolatedDecoderConfig(),
                                                     config=SeestarPreviewConfig(host=host, cameras=("main", "wide")))
            return PV.validate_preview(manager, ("main", "wide"), frames, max_seconds)
        finally:
            try:
                runtime.disconnect(connection)
            except Exception:
                pass

    return preview


def _seconds(value: float | None) -> timedelta | None:
    return timedelta(seconds=value) if value is not None and value > 0 else None


def main(argv=None, *, ask: Callable[[str], str] | None = None, control_factory: Callable[..., SeestarControl] | None = None,
         preview_factory: Callable[[int, float], dict] | None = None, clock: Callable[[], Any] = utc_now) -> int:
    parser = argparse.ArgumentParser(description="Supervised DB-05 command validation (read-only unless --allow-physical-motion).")
    parser.add_argument("--host", required=True, help="telescope address; never printed or written to the report")
    parser.add_argument("--operator", required=True, help="operator name bound to the permit")
    parser.add_argument("--telemetry-max-age", type=float, required=True, help="seconds a gate may rely on a telemetry reading")
    parser.add_argument("--capability-max-age", type=float, required=True, help="seconds a gate may rely on a capability report")
    parser.add_argument("--allow-physical-motion", action="store_true", help="the arm will move and the cameras will start")
    parser.add_argument("--command-deadline", type=float, default=None, help="seconds each command may take (physical mode)")
    parser.add_argument("--poll-interval", type=float, default=None, help="seconds between polls (physical mode)")
    parser.add_argument("--permit-validity", type=float, default=None, help="seconds the operator permit stays valid (physical mode)")
    parser.add_argument("--frames", type=int, default=None, help="frames per camera in the preview stage (physical mode)")
    parser.add_argument("--preview-max-seconds", type=float, default=None, help="time limit of the preview stage (physical mode)")
    parser.add_argument("--no-preview", action="store_true", help="skip the preview stage")
    parser.add_argument("--key-env", default="TSNDSS_SEESTAR_KEY_PATH")
    parser.add_argument("--out", type=Path, default=Path("seestar_command_validation.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    physical = args.allow_physical_motion
    key_path = os.environ.get(args.key_env)
    problems = []
    if _seconds(args.telemetry_max_age) is None or _seconds(args.capability_max_age) is None:
        problems.append("--telemetry-max-age and --capability-max-age must be positive")
    if physical:
        needed = {"--command-deadline": args.command_deadline, "--poll-interval": args.poll_interval, "--permit-validity": args.permit_validity}
        problems += [f"{name} is required (there is no default) and must be positive" for name, v in needed.items() if not v or v <= 0]
        if not args.no_preview:
            if not args.frames or not 1 <= args.frames <= 30:
                problems.append("--frames 1-30 is required unless --no-preview")
            if not args.preview_max_seconds or args.preview_max_seconds <= 0:
                problems.append("--preview-max-seconds is required unless --no-preview")
            if preview_factory is None and importlib.util.find_spec("cv2") is None:
                problems.append("OpenCV (opencv-python >= 4.8) is not installed in this interpreter")
        if ask is None and not (sys.stdin and sys.stdin.isatty()):
            problems.append("an interactive terminal is required for the confirmation prompts")
    if control_factory is None:
        if sys.version_info < (3, 13) and physical and not args.no_preview:
            problems.append("Python 3.13 or newer is required for the preview")
        if not (key_path and os.path.isfile(key_path)):
            problems.append(f"the PEM key file named by {args.key_env} is missing")
    if args.out.is_dir() or (args.out.exists() and not args.overwrite):
        problems.append("the output file already exists (choose another name or pass --overwrite)")
    if problems:
        for problem in problems:
            print("[PREREQUISITE] " + problem)
        return 2

    freshness = ControlFreshness(_seconds(args.telemetry_max_age), _seconds(args.capability_max_age))
    permit = OperatorPermit(operator_id=args.operator, valid_for=_seconds(args.permit_validity) or timedelta(seconds=1), clock=clock)
    if control_factory is not None:
        control = control_factory(freshness, permit)
    else:
        from tsn_dss.engine.seestar_provider import RsaKeyFileAuthenticator, SeestarProviderConfig, TcpSeestarTransport

        config = SeestarProviderConfig(host=args.host, key_path=key_path)
        auth = RsaKeyFileAuthenticator(key_path)
        control = SeestarControl(config=config, read_transport=TcpSeestarTransport(config, authenticator=auth),
                                 control_transport=SeestarControlTransport(config, auth), freshness=freshness, authorizer=permit)
    preview = None
    if physical and not args.no_preview:
        make = preview_factory or _real_preview(args.host, key_path)
        preview = (lambda frames: make(frames, args.preview_max_seconds))
    if physical:
        print("This will MOVE THE ARM and START THE CAMERAS. Make sure the arm's path and the field of view are clear.")
    result = run_validation(
        control, permit if physical else None, operator=args.operator, physical=physical, ask=(ask or input) if physical else None,
        preview=preview, frames=args.frames or 0, deadline=_seconds(args.command_deadline) or timedelta(0),
        poll_interval=_seconds(args.poll_interval) or timedelta(0), clock=clock)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for stage in result["stages"]:
        print(f"[{stage['status']}] {stage['name']}: {stage['detail']}")
    print(f"[{result['overall']}] mode={result['mode']}; arm={result['final_state']['arm']} cameras={result['final_state']['cameras']}; "
          "report written (no address, no image).")
    if result["unsafe_or_unknown_final_state"] or result["recovery_required"]:
        print("*** UNSAFE OR UNKNOWN STATE: the arm may be open and/or a camera may still be running. CHECK THE TELESCOPE NOW. ***")
        if result["recovery_required"]:
            print("*** An uncertain command outcome is still open. Nothing was recovered or cleared automatically. ***")
        return 3
    if result["interrupted"]:
        return 130
    return 0 if result["overall"] in ("PASS", "READ_ONLY") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[STOP] interrupted before the sequence started", file=sys.stderr)
        sys.exit(130)
