"""DB-03 Wave 4B-1: static and import-time boundaries of the process-isolation package."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "tsn_dss" / "engine" / "opencv_isolated_decoder"
FILES = {p.name: p for p in sorted(PACKAGE.glob("*.py"))}

# Which standard-library modules each file may import (decision D11, narrowed to what 4B-1 needs).
ALLOWED_STDLIB = {
    "__init__.py": {"__future__"},
    "protocol.py": {"__future__", "struct", "dataclasses", "enum", "typing"},
    "segment.py": {"__future__", "struct", "dataclasses"},
    "states.py": {"__future__", "enum"},
    "process.py": {"__future__", "contextlib", "os", "secrets", "subprocess", "sys", "tempfile", "threading", "time", "dataclasses",
                   "datetime", "pathlib", "multiprocessing"},
    "worker_main.py": {"__future__", "os", "queue", "sys", "threading", "multiprocessing"},
}
FORBIDDEN_EVERYWHERE = {"cv2", "numpy", "PIL", "av", "socket", "ssl", "select", "selectors", "asyncio", "http", "urllib", "ftplib", "ctypes",
                        "pickle", "shelve", "sqlite3", "shutil", "io", "signal", "atexit", "requests", "seestarpy", "astropy", "mcp"}
COMMAND_WORDS = {"start_view", "stop_view", "start_scan", "scope", "park", "slew", "goto", "arm"}


def code_only(path: Path) -> str:
    """Source without comments and docstrings, normalised by ast.unparse, so checks cannot be fooled (or tripped) by prose."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield 0, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or ""


class ImportBoundaryTests(unittest.TestCase):
    def test_the_package_has_exactly_the_planned_files(self) -> None:
        self.assertEqual(set(FILES), {"__init__.py", "protocol.py", "segment.py", "states.py", "process.py", "worker_main.py"})

    def test_each_file_imports_only_what_it_is_allowed_to(self) -> None:
        for name, path in FILES.items():
            for level, module in imports(path):
                if level == 0:
                    top = module.split(".")[0]
                    self.assertIn(top, ALLOWED_STDLIB[name], (name, module))
                    self.assertNotIn(top, FORBIDDEN_EVERYWHERE, (name, module))
                elif level == 1:
                    self.assertIn(module, {"", "process", "protocol", "segment", "states"}, (name, module))
                else:
                    self.assertEqual(level, 2)
                    self.assertIn(module, {"device_runtime.errors", "device_runtime.lifecycle"}, (name, module))

    def test_multiprocessing_is_limited_to_the_connection_module(self) -> None:
        for name, path in FILES.items():
            for level, module in imports(path):
                if level == 0 and module.split(".")[0] == "multiprocessing":
                    self.assertEqual(module, "multiprocessing.connection", (name, module))
                    self.assertIn(name, {"process.py", "worker_main.py"})

    def test_shared_memory_is_not_used_yet(self) -> None:
        for name, path in FILES.items():
            self.assertNotIn("shared_memory", code_only(path), name)                        # attached in 4B-2

    def test_no_opencv_decoder_and_no_preview_runtime_coupling(self) -> None:
        for name, path in FILES.items():
            text = code_only(path)
            self.assertNotIn("opencv_preview_decoder", text, name)
            self.assertNotIn("seestar_preview", text, name)
            self.assertNotIn("PreviewStreamManager", text, name)
            self.assertNotIn("cv2", text, name)

    def test_lower_layers_do_not_know_this_package(self) -> None:
        for directory in ("device_runtime", "seestar_provider", "seestar_preview", "opencv_preview_decoder"):
            for path in sorted((ROOT / "tsn_dss" / "engine" / directory).glob("*.py")):
                self.assertNotIn("opencv_isolated_decoder", path.read_text(encoding="utf-8"), path.name)

    def test_importing_the_package_starts_nothing_and_loads_no_decoder_library(self) -> None:
        code = (
            "import sys\nimport tsn_dss.engine.device_runtime\nbefore=set(sys.modules)\n"
            "import tsn_dss.engine.opencv_isolated_decoder as p\n"
            "bad=sorted(m for m in set(sys.modules)-before if m.split('.')[0] in {'cv2','numpy','PIL','av'} or m=='multiprocessing.shared_memory')\n"
            "print(','.join(bad)); print(sorted(p.__all__)[:2])\n"
        )
        result = subprocess.run([sys.executable, "-P", "-c", code], cwd=ROOT, capture_output=True, text=True,
                                env={"PATH": "", "PYTHONPATH": str(ROOT), "SYSTEMROOT": ""} if sys.platform == "win32" else {"PYTHONPATH": str(ROOT)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "")

    def test_no_dependency_was_added(self) -> None:
        self.assertNotIn("opencv", (ROOT / "requirements.txt").read_text(encoding="utf-8").lower())
        for name in ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile"):
            self.assertFalse((ROOT / name).exists(), name)


class SafetyBoundaryTests(unittest.TestCase):
    def test_no_pickle_surface(self) -> None:
        for name, path in FILES.items():
            text = code_only(path)
            self.assertIsNone(re.search(r"\.(send|recv)\(", text), name)           # only send_bytes / recv_bytes
            self.assertNotIn("pickle", text, name)

    def test_no_private_cpython_api(self) -> None:
        for name, path in FILES.items():
            code = code_only(path)
            for banned in ("._handle", "_winapi", "Popen._", "subprocess.Handle", "resource_tracker"):
                self.assertNotIn(banned, code, (name, banned))
            self.assertIsNone(re.search(r"\.Close\(\)", code), name)               # subprocess.Handle.Close() takes no argument

    def test_the_parents_global_state_is_never_modified(self) -> None:
        for name, path in FILES.items():
            code = code_only(path)
            for pattern in (r"os\.environ\[[^\]]*\]\s*=", r"os\.environ\.(update|setdefault|pop|clear)", r"os\.putenv", r"os\.unsetenv",
                            r"sys\.path\.(insert|append|extend)", r"sys\.path\s*=", r"sys\.modules\[[^\]]*\]\s*=",
                            r"sys\.modules\[.__main__.\]", r"signal\.signal", r"setattr\(sys"):
                self.assertIsNone(re.search(pattern, code), (name, pattern))

    def test_no_print_eval_or_literal_addresses(self) -> None:
        for name, path in FILES.items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            self.assertEqual(called & {"print", "input", "exec", "eval", "compile", "__import__", "open"}, set(), name)
            self.assertEqual(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", path.read_text(encoding="utf-8")), [], name)

    def test_no_command_vocabulary(self) -> None:
        for name, path in FILES.items():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    words = {w.lower() for w in re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", node.name).split("_") if w}
                    self.assertFalse(words & {"park", "slew", "goto", "arm", "heat", "capture", "frame", "session", "dataset", "acquire"}, (name, node.name))

    def test_the_worker_receives_only_numbers_on_its_command_line(self) -> None:
        tree = ast.parse((PACKAGE / "process.py").read_text(encoding="utf-8"))
        assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "argv" for t in n.targets)]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(ast.unparse(assignments[0].value),
                         "[sys.executable, '-P', '-m', self._entry, '--ctl', str(handle), '--proto', str(P.PROTOCOL_VERSION), *self._extra_args]")

    def test_the_child_environment_is_an_allowlist(self) -> None:
        code = code_only(PACKAGE / "process.py")
        self.assertIn("env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}", code)
        self.assertNotIn("dict(os.environ)", code)
        self.assertNotIn("os.environ.copy", code)

    def test_production_code_has_no_fixture_selection(self) -> None:
        for name, path in FILES.items():
            code = code_only(path)
            self.assertNotIn("--fixture", code, name)
            self.assertNotIn("process_fixtures", code, name)
        self.assertIn("_entry_module: str=PRODUCTION_ENTRY", code_only(PACKAGE / "process.py").replace(" = ", "="))

    def test_the_entry_point_is_a_module_not_a_script_path(self) -> None:
        from tsn_dss.engine.opencv_isolated_decoder import PRODUCTION_ENTRY
        self.assertEqual(PRODUCTION_ENTRY, "tsn_dss.engine.opencv_isolated_decoder.worker_main")
        self.assertTrue((PACKAGE / "worker_main.py").is_file())

    def test_python_below_the_minimum_is_refused_without_breaking_import(self) -> None:
        self.assertIn("MIN_PYTHON = (3, 13)", code_only(PACKAGE / "process.py"))
        self.assertNotIn("version_info", code_only(PACKAGE / "__init__.py"))                                      # nothing at import time depends on the version


if __name__ == "__main__":
    unittest.main()
