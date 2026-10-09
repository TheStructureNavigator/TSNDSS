"""DB-03 Wave 1: architectural boundaries of the neutral preview core."""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "tsn_dss" / "engine" / "device_runtime"
MODULES = (
    "preview_models", "preview_freshness", "preview_stream", "preview_simulator",
    "preview_readiness", "preview_manager",  # DB-03 Wave 2
)
FILES = [PACKAGE / f"{name}.py" for name in MODULES]
FORBIDDEN_IMPORTS = {
    "sqlite3", "socket", "ssl", "http", "urllib", "subprocess", "multiprocessing", "ctypes", "asyncio",
    "threading", "tempfile", "shutil", "pathlib", "os", "io", "pickle", "random",
    "cv2", "numpy", "PIL", "seestarpy", "astropy", "mcp",
}
COMMAND_WORDS = ("start", "stop", "arm", "park", "slew", "send", "command", "configure", "set_")


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


class PreviewBoundaryTests(unittest.TestCase):
    def test_modules_exist(self) -> None:
        for path in FILES:
            self.assertTrue(path.is_file(), path)

    def test_no_file_or_network_primitives(self) -> None:
        for path in FILES:
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], FORBIDDEN_IMPORTS, (path.name, alias.name))
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertNotIn(node.module.split(".")[0], FORBIDDEN_IMPORTS, (path.name, node.module))
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"open", "exec", "eval", "__import__"}, path.name)

    def test_no_domain_identity_or_persistence(self) -> None:
        for path in FILES:
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertFalse(node.module.startswith("tsn_dss"), (path.name, node.module))
                if isinstance(node, ast.ImportFrom) and node.level >= 2:
                    self.fail(f"{path.name} imports above the package")
            text = path.read_text(encoding="utf-8").lower()
            for token in ("sqlite", "capturerecord", "frameid", "target_id", "dataset"):
                self.assertNotIn(token, text, (path.name, token))

    def test_no_vendor_tokens(self) -> None:
        for path in FILES:
            text = path.read_text(encoding="utf-8").lower()
            for token in ("seestar", "rtsp", "ascom", "alpaca", "4700", "4554", "4555", "cv2", "opencv"):
                self.assertNotIn(token, text, (path.name, token))

    def test_no_command_surface(self) -> None:
        """No public callable of the stream, buffer or source protocol looks like a device command."""
        from tsn_dss.engine.device_runtime.preview_stream import PixelBuffer, PreviewSource, PreviewStream

        for cls in (PreviewStream, PixelBuffer, PreviewSource):
            for name in dir(cls):
                if name.startswith("_"):
                    continue
                self.assertFalse(any(name.startswith(w) or name == w for w in COMMAND_WORDS), (cls.__name__, name))

    def test_manager_and_gate_expose_no_command_surface(self) -> None:
        from tsn_dss.engine.device_runtime.preview_manager import PreviewStreamManager
        from tsn_dss.engine.device_runtime.preview_readiness import ReadinessEvidenceProvider, ReadinessGate

        for cls in (PreviewStreamManager, ReadinessGate, ReadinessEvidenceProvider):
            for name in dir(cls):
                if name.startswith("_"):
                    continue
                self.assertFalse(any(name.startswith(w) or name == w for w in COMMAND_WORDS), (cls.__name__, name))
        public = {n for n in dir(PreviewStreamManager) if not n.startswith("_")}
        self.assertEqual(
            public,
            {"connection_id", "cameras", "active_labels", "states", "stream_id", "open_stream", "poll", "poll_all",
             "view", "fresh_pixels", "close_stream", "close_all"},
        )

    def test_fresh_interpreter_loads_nothing_forbidden(self) -> None:
        """Importing the preview modules adds no forbidden module beyond what the package import already loads."""
        code = (
            "import sys\n"
            "import tsn_dss.engine.device_runtime\n"
            "before=set(sys.modules)\n"
            + "".join(f"import tsn_dss.engine.device_runtime.{m}\n" for m in MODULES)
            + "added=set(sys.modules)-before\n"
            "bad=[m for m in added if m.split('.')[0] in %r or m.startswith('tsn_dss.gui') "
            "or m.startswith('tsn_dss.mcp') or m.startswith('tsn_dss.engine.sqlite')]\n"
            "print(','.join(sorted(bad)))"
        ) % ({"socket", "subprocess", "cv2", "numpy", "PIL", "astropy", "seestarpy", "sqlite3", "mcp"},)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "")

    def test_protected_stage_files_are_not_extended(self) -> None:
        init = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("preview_", init)


DOMAIN_FIELDS = {
    "project_id", "session_id", "observation_id", "target_id", "session_plan_id", "acquisition_plan_id",
    "mosaic_plan_id", "capture_id", "frame_id", "dataset_id", "processing_run_id", "session_context_fact_id",
    "session_event_id", "site_id",
}
DOMAIN_WORDS = ("session", "observation", "capture", "frame", "dataset", "processing", "target", "project")


def _database_state(path: Path) -> tuple[str, dict[str, int]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    con = sqlite3.connect(str(path))
    try:
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
        counts = {t: con.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()
    return digest, counts


class PreviewEvidenceOnlyTests(unittest.TestCase):
    """REQ-018 and REQ-064 by behavior: preview data and decisions stay runtime evidence."""

    def test_full_preview_exercise_leaves_database_and_directory_untouched(self) -> None:
        import os
        from datetime import timedelta

        from tsn_dss.engine.device_runtime.preview_simulator import scenario_loss
        from tsn_dss.engine.sqlite.db import initialize_database

        try:
            from preview_manager_support import CAM_A, CAM_B, Rig
        except ModuleNotFoundError:  # pragma: no cover
            from tests.preview_manager_support import CAM_A, CAM_B, Rig

        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tsn.db"
            initialize_database(db_path).close()
            before_state, before_files = _database_state(db_path), sorted(os.listdir(tmp))
            self.assertGreater(len(before_state[1]), 5)
            os.chdir(tmp)
            try:
                rig = Rig()
                rig.factory.queues[CAM_A] = [scenario_loss(1)]
                rig.manager.open_stream(CAM_A)
                rig.manager.open_stream(CAM_B)
                for _ in range(3):
                    rig.manager.poll_all()
                    rig.manager.view(CAM_A)
                    rig.manager.fresh_pixels(CAM_B)
                rig.advance(1)
                rig.manager.open_stream(CAM_A)
                rig.runtime.disconnect(rig.connection)
                rig.manager.poll_all()
                rig.manager.close_all()
                self.assertGreaterEqual(len(rig.factory.sources), 3)  # the exercise really ran
            finally:
                os.chdir(previous)
            self.assertEqual(_database_state(db_path), before_state)
            self.assertEqual(sorted(os.listdir(tmp)), before_files)  # no pixel or record files appeared

    def test_preview_outputs_carry_no_domain_identity_or_claim(self) -> None:
        from dataclasses import fields, is_dataclass

        from tsn_dss.engine.device_runtime import preview_manager, preview_models, preview_readiness

        outputs = [
            preview_models.PreviewImageEvidence, preview_models.PreviewPixels, preview_models.SourceImage,
            preview_manager.OpenOutcome, preview_manager.PollResult, preview_manager.PreviewView,
            preview_manager.CloseReport, preview_readiness.GateDecision, preview_readiness.ReadinessEvidence,
            preview_readiness.ReadinessIdentity,
        ]
        for cls in outputs:
            self.assertTrue(is_dataclass(cls), cls)
            names = {f.name for f in fields(cls)}
            self.assertTrue(names.isdisjoint(DOMAIN_FIELDS), cls.__name__)
            self.assertFalse([n for n in names if any(w in n for w in DOMAIN_WORDS)], cls.__name__)
            public = {n for n in dir(cls) if not n.startswith("_")}
            self.assertFalse([n for n in public if any(w in n for w in DOMAIN_WORDS)], cls.__name__)

    def test_evidence_declares_itself_runtime_only(self) -> None:
        from tsn_dss.engine.device_runtime.preview_simulator import scenario_fresh
        from tsn_dss.engine.device_runtime.preview_stream import PreviewStream

        stream = PreviewStream(provider_id="p", connection_id="c", source_label="cam_a", source=scenario_fresh(1))
        stream.open()
        evidence = stream.poll()
        self.assertTrue(evidence.is_runtime_evidence_only)
        self.assertFalse(evidence.is_canonical_record)
        self.assertIsNone(getattr(evidence, "data", None))  # pixels stay in the in-memory buffer


if __name__ == "__main__":
    unittest.main()
