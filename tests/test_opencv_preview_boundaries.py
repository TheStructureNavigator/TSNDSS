"""DB-03 Wave 4: boundaries of the optional OpenCV decoder package."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tsn_dss.engine import opencv_preview_decoder

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(opencv_preview_decoder.__file__).resolve().parent
SOURCES = sorted(PACKAGE.glob("*.py"))
ALLOWED_STDLIB = {"__future__", "dataclasses", "importlib", "re", "threading", "typing"}
FORBIDDEN_TOP = {
    "cv2", "numpy", "PIL", "av", "imageio", "socket", "ssl", "select", "selectors", "subprocess", "multiprocessing",
    "asyncio", "http", "urllib", "ftplib", "requests", "os", "io", "pathlib", "tempfile", "shutil", "sqlite3",
    "pickle", "ctypes", "sys", "seestarpy", "astropy", "mcp", "cryptography",
}
ALLOWED_SIBLINGS = {"device_runtime.preview_models", "seestar_preview"}
COMMAND_WORDS = {
    "start", "stop", "arm", "park", "slew", "move", "goto", "send", "command", "execute", "run", "set", "toggle",
    "enable", "disable", "heat", "capture", "frame", "session", "observation", "acquire", "acquisition", "dataset",
    "reconnect", "retry", "grab",
}


def _words(name: str) -> set[str]:
    return {w.lower() for w in re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).split("_") if w}


class ImportBoundaryTests(unittest.TestCase):
    def test_package_files(self) -> None:
        self.assertEqual(PACKAGE, ROOT / "tsn_dss" / "engine" / "opencv_preview_decoder")
        self.assertEqual({p.name for p in SOURCES}, {"__init__.py", "decoder.py"})

    def test_only_allowed_imports_and_opencv_is_never_imported_statically(self) -> None:
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        self.assertIn(top, ALLOWED_STDLIB, (path.name, alias.name))
                        self.assertNotIn(top, FORBIDDEN_TOP, (path.name, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0:
                        self.assertIn((node.module or "").split(".")[0], ALLOWED_STDLIB, (path.name, node.module))
                    elif node.level == 1:
                        self.assertIn(node.module, {"decoder", None}, (path.name, node.module))
                    else:
                        self.assertEqual(node.level, 2)
                        self.assertIn(node.module, ALLOWED_SIBLINGS, (path.name, node.module))

    def test_opencv_is_imported_in_exactly_one_lazy_place(self) -> None:
        hits = []
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "import_module":
                    hits.append((path.name, ast.literal_eval(node.args[0])))
        self.assertEqual(hits, [("decoder.py", "cv2")])
        text = (PACKAGE / "decoder.py").read_text(encoding="utf-8")
        self.assertNotIn("import cv2", text)
        self.assertNotIn("import numpy", text)

    def test_no_dependency_was_added(self) -> None:
        self.assertNotIn("opencv", (ROOT / "requirements.txt").read_text(encoding="utf-8").lower())
        for name in ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile"):
            self.assertFalse((ROOT / name).exists(), name)

    def test_no_test_imports_a_real_opencv(self) -> None:
        for path in sorted((ROOT / "tests").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if path.name == "test_opencv_preview_boundaries.py":
                continue
            self.assertNotRegex(text, r"^\s*(import cv2|from cv2)", path.name)

    def test_lower_layers_do_not_know_this_package(self) -> None:
        for directory in ("device_runtime", "seestar_provider", "seestar_preview"):
            for path in sorted((ROOT / "tsn_dss" / "engine" / directory).glob("*.py")):
                self.assertNotIn("opencv_preview_decoder", path.read_text(encoding="utf-8"), path.name)

    def test_importing_the_package_loads_neither_opencv_nor_numpy(self) -> None:
        code = (
            "import sys\nimport tsn_dss.engine.seestar_preview\nbefore=set(sys.modules)\n"
            "import tsn_dss.engine.opencv_preview_decoder\n"
            "print(','.join(sorted(m for m in set(sys.modules)-before if m.split('.')[0] in {'cv2','numpy','PIL','av','seestarpy'})))\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


class SafetyBoundaryTests(unittest.TestCase):
    def test_no_command_or_reconnect_vocabulary(self) -> None:
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    self.assertFalse(_words(node.name) & COMMAND_WORDS, (path.name, node.name))
        for name in opencv_preview_decoder.__all__:
            self.assertFalse(_words(name) & COMMAND_WORDS, name)

    def test_backend_is_explicit_and_there_is_no_fallback(self) -> None:
        text = (PACKAGE / "decoder.py").read_text(encoding="utf-8")
        self.assertIn("module.CAP_FFMPEG", text)
        self.assertIn('backend != "FFMPEG"', text)
        for banned in ("CAP_ANY", "CAP_GSTREAMER", "CAP_V4L", "CAP_DSHOW", "CAP_MSMF", "OPENCV_FFMPEG_CAPTURE_OPTIONS", "environ"):
            self.assertNotIn(banned, text, banned)
        self.assertEqual(text.count("module.VideoCapture("), 1)  # one construction: no second attempt

    def test_both_soft_timeouts_are_passed_and_no_hard_timeout_is_claimed(self) -> None:
        text = (PACKAGE / "decoder.py").read_text(encoding="utf-8")
        self.assertIn("CAP_PROP_OPEN_TIMEOUT_MSEC", text)
        self.assertIn("CAP_PROP_READ_TIMEOUT_MSEC", text)
        lowered = text.lower()
        self.assertIn("no hard timeout", lowered)
        self.assertNotIn("guarantee", lowered.replace("gives no hard containment", ""))
        doc = (ROOT / "docs" / "DB-03_OPENCV_DECODER.md").read_text(encoding="utf-8").lower()
        self.assertIn("no hard timeout", doc)
        self.assertIn("soft", doc)

    def test_no_threads_are_started_and_no_process_is_spawned(self) -> None:
        text = (PACKAGE / "decoder.py").read_text(encoding="utf-8")
        self.assertNotIn("Thread(", text)
        self.assertNotIn("Popen", text)
        self.assertEqual(text.count("threading.Lock()"), 1)

    def test_no_print_file_access_eval_or_literal_addresses(self) -> None:
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            self.assertEqual(called & {"print", "input", "exec", "eval", "compile", "__import__", "open"}, set(), path.name)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), [], path.name)
            for banned in ("write(", "write_bytes", "imwrite", "VideoWriter", "tofile"):
                self.assertNotIn(banned, text, (path.name, banned))

    def test_no_state_changing_capture_properties_are_used(self) -> None:
        text = (PACKAGE / "decoder.py").read_text(encoding="utf-8")
        self.assertNotIn(".set(", text)  # VideoCapture.set would change capture state
        self.assertNotIn("setExceptionMode", text)


if __name__ == "__main__":
    unittest.main()
