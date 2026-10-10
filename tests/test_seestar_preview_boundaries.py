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


PUBLIC_API = {
    "SeestarPreviewConfig", "StreamEndpoint", "build_preview_manager", "SeestarReadinessEvidenceProvider",
    "camera_availability", "DecoderError", "ImageDecoder",
}


class PublicApiBoundaryTests(unittest.TestCase):
    """The manager is the only official way to open a preview stream (a documented boundary, not a lock)."""

    def test_exact_public_names(self) -> None:
        self.assertEqual(set(seestar_preview.__all__), PUBLIC_API)

    def test_source_building_blocks_are_not_part_of_the_package_namespace(self) -> None:
        for name in ("RtspPreviewSource", "make_source_factory"):
            self.assertFalse(hasattr(seestar_preview, name), name)
            self.assertNotIn(name, seestar_preview.__all__)
            self.assertNotIn(name, dir(seestar_preview))
        from tsn_dss.engine.seestar_preview import source  # still importable for direct tests

        self.assertTrue(callable(source.RtspPreviewSource) and callable(source.make_source_factory))
        self.assertIn("INTERNAL", source.__doc__)

    def test_no_public_name_opens_a_source(self) -> None:
        import inspect

        for name in seestar_preview.__all__:
            obj = getattr(seestar_preview, name)
            if name == "ImageDecoder":  # the interface an adapter implements; not something that opens by itself
                continue
            if inspect.isclass(obj):
                self.assertFalse({"open", "read", "close"} & set(dir(obj)), name)
            if inspect.isfunction(obj):
                params = set(inspect.signature(obj).parameters)
                self.assertFalse(params & {"source", "stream", "source_factory"}, name)

    def test_official_entry_point_builds_a_manager_wired_through_the_gate(self) -> None:
        from tsn_dss.engine.device_runtime.preview_manager import PreviewStreamManager
        from tsn_dss.engine.device_runtime.preview_readiness import ReadinessGate

        try:
            from seestar_preview_support import Flow
        except ModuleNotFoundError:  # pragma: no cover
            from tests.seestar_preview_support import Flow

        manager = Flow().manager
        self.assertIsInstance(manager, PreviewStreamManager)
        self.assertIsInstance(manager._gate, ReadinessGate)
        self.assertIsInstance(manager._gate._provider, seestar_preview.SeestarReadinessEvidenceProvider)
        text = (PACKAGE / "integration.py").read_text(encoding="utf-8")
        self.assertIn("ReadinessGate(", text)
        self.assertIn("gate=gate", text)

    def test_streams_and_sources_are_constructed_and_opened_in_one_place(self) -> None:
        engine = ROOT / "tsn_dss"
        constructions = {
            "PreviewStream(": [], "RtspPreviewSource(": [], "make_source_factory(": [],
        }
        for path in engine.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle, hits in constructions.items():
                for line in text.splitlines():
                    if needle in line and not line.lstrip().startswith(("class ", "def ", "#", '"', "``", "return f")):
                        hits.append(path.name)
        self.assertEqual(constructions["PreviewStream("], ["preview_manager.py"])
        self.assertEqual(constructions["RtspPreviewSource("], ["source.py"])
        # Wave 3 build_preview_manager and, since 4B-3c, the isolated composition root: the only two places that build a source factory
        self.assertEqual(constructions["make_source_factory("], ["integration.py", "integration.py"])
        self.assertEqual(sorted(p.parent.name for p in engine.rglob("integration.py") if "make_source_factory(" in p.read_text(encoding="utf-8")),
                         ["opencv_isolated_decoder", "seestar_preview"])
        manager_text = (ROOT / "tsn_dss/engine/device_runtime/preview_manager.py").read_text(encoding="utf-8")
        self.assertEqual(manager_text.count("stream.open()"), 1)
        gate_pos = manager_text.index("self._gate.check(")
        self.assertLess(gate_pos, manager_text.index("self._factory(label)"))
        self.assertLess(gate_pos, manager_text.index("stream.open()"))
        for path in SOURCES:
            if path.name != "source.py":
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"\.open\(", path.name)

    def test_official_api_results_never_hand_out_a_stream_source_or_decoder(self) -> None:
        from dataclasses import fields, is_dataclass

        from tsn_dss.engine.device_runtime.preview_stream import PreviewSource, PreviewStream

        try:
            from seestar_preview_support import Flow
        except ModuleNotFoundError:  # pragma: no cover
            from tests.seestar_preview_support import Flow

        flow = Flow()
        results = [flow.manager.open_stream("main"), flow.manager.open_stream("wide"), flow.manager.open_stream("main")]
        results += list(flow.manager.poll_all())
        results += [flow.manager.view("main"), flow.manager.fresh_pixels("wide"), flow.manager.states(), flow.manager.stream_id("main")]
        results += [flow.manager.close_stream("main"), flow.manager.close_all()]

        def walk(value, depth=0):
            yield value
            if depth < 3 and is_dataclass(value):
                for f in fields(value):
                    yield from walk(getattr(value, f.name), depth + 1)
            elif depth < 3 and isinstance(value, (tuple, list)):
                for item in value:
                    yield from walk(item, depth + 1)
            elif depth < 3 and isinstance(value, dict):
                for item in value.values():
                    yield from walk(item, depth + 1)

        for result in results:
            for item in walk(result):
                self.assertNotIsInstance(item, (PreviewStream, PreviewSource, seestar_preview.ImageDecoder))
        public = [n for n in dir(flow.manager) if not n.startswith("_")]
        self.assertEqual(
            sorted(public),
            sorted(["connection_id", "cameras", "active_labels", "states", "stream_id", "open_stream", "poll", "poll_all",
                    "view", "fresh_pixels", "close_stream", "close_all"]),
        )


if __name__ == "__main__":
    unittest.main()
