"""DB-03 Wave 3: boundaries of the Seestar preview integration package."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tsn_dss.engine import seestar_preview

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(seestar_preview.__file__).resolve().parent
SOURCES = sorted(PACKAGE.glob("*.py"))
ALLOWED_STDLIB = {"__future__", "dataclasses", "datetime", "typing", "sys"}
FORBIDDEN_TOP = {
    "socket", "ssl", "select", "selectors", "subprocess", "multiprocessing", "threading", "asyncio", "http",
    "urllib", "ftplib", "requests", "os", "io", "pathlib", "tempfile", "shutil", "sqlite3", "pickle", "ctypes",
    "cv2", "numpy", "PIL", "av", "imageio", "cryptography", "seestarpy", "astropy", "mcp",
}
ALLOWED_SIBLINGS = {
    "device_runtime", "device_runtime.preview_models", "device_runtime.preview_stream",
    "device_runtime.preview_readiness", "device_runtime.preview_manager", "device_runtime.preview_freshness",
    "device_runtime.support", "seestar_provider.config", "seestar_provider.errors", "seestar_provider.normalize",
}
COMMAND_WORDS = {
    "start", "stop", "arm", "park", "slew", "move", "goto", "send", "command", "execute", "run", "set", "toggle",
    "enable", "disable", "heat", "capture", "frame", "session", "observation", "acquire", "acquisition", "dataset",
}


def _words(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return {w.lower() for w in spaced.split("_") if w}


class ImportBoundaryTests(unittest.TestCase):
    def test_package_location_and_files(self) -> None:
        self.assertEqual(PACKAGE, ROOT / "tsn_dss" / "engine" / "seestar_preview")
        self.assertEqual({p.name for p in SOURCES}, {"__init__.py", "config.py", "integration.py", "readiness.py", "source.py"})

    def test_imports_are_stdlib_subset_or_the_two_allowed_sibling_packages(self) -> None:
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], ALLOWED_STDLIB, (path.name, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0:
                        self.assertIn((node.module or "").split(".")[0], ALLOWED_STDLIB, (path.name, node.module))
                    elif node.level == 1:
                        self.assertIn(node.module, {None, "config", "integration", "readiness", "source"}, (path.name, node.module))
                    else:
                        self.assertEqual(node.level, 2, (path.name, node.module))
                        self.assertIn(node.module, ALLOWED_SIBLINGS, (path.name, node.module))

    def test_no_forbidden_module_is_imported_anywhere(self) -> None:
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                )
                for name in names:
                    self.assertNotIn(name.split(".")[0], FORBIDDEN_TOP, (path.name, name))

    def test_transport_and_auth_of_db02_are_never_imported(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for banned in ("seestar_provider.transport", "seestar_provider.auth", "seestar_provider.provider", "seestar_provider.protocol"):
                self.assertNotIn(banned, text, (path.name, banned))
            self.assertNotRegex(text, r"from \.\.seestar_provider import")
            self.assertNotIn("import seestarpy", text)

    def test_no_domain_storage_gui_mcp_or_legacy_adapter_imports(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for banned in ("engine.sqlite", "engine.telescope", "tsn_dss.gui", "tsn_dss.mcp", "repository"):
                self.assertNotIn(banned, text, (path.name, banned))

    def test_neutral_runtime_and_db02_do_not_know_this_package(self) -> None:
        for directory in ("device_runtime", "seestar_provider"):
            for path in sorted((ROOT / "tsn_dss" / "engine" / directory).glob("*.py")):
                self.assertNotIn("seestar_preview", path.read_text(encoding="utf-8"), path.name)

    def test_importing_the_package_loads_no_decoder_or_third_party_code(self) -> None:
        code = (
            "import sys\n"
            "import tsn_dss.engine.seestar_provider\n"
            "before=set(sys.modules)\n"
            "import tsn_dss.engine.seestar_preview\n"
            "bad=sorted(m for m in set(sys.modules)-before if m.split('.')[0] in %r)\n"
            "print(','.join(bad))\n"
        ) % ({"cv2", "numpy", "PIL", "av", "imageio", "cryptography", "seestarpy", "astropy", "mcp", "requests"},)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


class SafetyBoundaryTests(unittest.TestCase):
    def test_no_command_vocabulary_in_any_identifier(self) -> None:
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    self.assertFalse(_words(node.name) & COMMAND_WORDS, (path.name, node.name))
                    if isinstance(node, ast.FunctionDef):
                        for arg in node.args.args + node.args.kwonlyargs:
                            self.assertFalse(_words(arg.arg) & COMMAND_WORDS, (path.name, arg.arg))
        for name in seestar_preview.__all__:
            self.assertFalse(_words(name) & COMMAND_WORDS, name)

    def test_no_state_changing_rpc_method_names_appear(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for banned in (
                "iscope_start", "iscope_stop", "scope_", "set_setting", "set_user_location", "start_scan", "stop_scan",
                "scope_park", "scope_move", "scope_goto", "start_view", "stop_view", "set_control_value",
            ):
                self.assertNotIn(banned, text, (path.name, banned))

    def test_no_print_eval_file_access_or_dynamic_import(self) -> None:
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            self.assertEqual(called & {"print", "input", "exec", "eval", "compile", "__import__", "open"}, set(), path.name)

    def test_no_literal_addresses_or_fallback_hosts_in_the_package(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), [], path.name)
            self.assertNotIn("localhost", text.lower())
            self.assertNotIn("s30lab", text.lower())

    def test_stream_address_is_built_in_one_place_from_fixed_ports(self) -> None:
        hits = [p.name for p in SOURCES if "rtsp://" in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, ["config.py"])

    def test_no_pixel_or_record_persistence(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8").lower()
            for banned in ("write(", "write_bytes", "tofile", "dump(", "save(", "imwrite"):
                self.assertNotIn(banned, text, (path.name, banned))


if __name__ == "__main__":
    unittest.main()
