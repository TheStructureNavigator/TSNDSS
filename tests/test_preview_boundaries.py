"""DB-03 Wave 1: architectural boundaries of the neutral preview core."""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "tsn_dss" / "engine" / "device_runtime"
MODULES = ("preview_models", "preview_freshness", "preview_stream", "preview_simulator")
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


if __name__ == "__main__":
    unittest.main()
