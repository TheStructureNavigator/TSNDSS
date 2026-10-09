"""DB-01 architectural and domain-ownership boundaries (DSS-CTR-013 sections 13-15)."""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tsn_dss.engine import device_runtime
from tsn_dss.engine.sqlite.db import connect_database, initialize_database

try:
    from device_runtime_support import connected_runtime, make_registry, make_provider
except ModuleNotFoundError:  # pragma: no cover
    from tests.device_runtime_support import connected_runtime, make_registry, make_provider

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path(device_runtime.__file__).resolve().parent
SOURCES = sorted(PACKAGE_DIR.glob("*.py"))
PACKAGE_MODULES = {path.stem for path in SOURCES}

# Standard-library modules the runtime must never use: storage, network, processes.
FORBIDDEN_STDLIB = {
    "sqlite3", "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "telnetlib",
    "subprocess", "multiprocessing", "ctypes", "asyncio", "selectors", "xmlrpc", "shelve", "dbm",
}
THIRD_PARTY_MARKERS = ("astropy", "astroplan", "rasterio", "PIL", "numpy", "mcp")


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield 0, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or ""


class PackageIsolationTests(unittest.TestCase):
    def test_package_location_and_files(self) -> None:
        """Roadmap DB-01: tsn_dss/engine/device_runtime/."""
        self.assertEqual(PACKAGE_DIR, ROOT / "tsn_dss" / "engine" / "device_runtime")
        self.assertTrue({"models", "lifecycle", "connection", "runtime", "registry", "simulator"} <= PACKAGE_MODULES)

    def test_only_standard_library_and_intra_package_imports(self) -> None:
        """Roadmap DB-01: standard-library-only package."""
        stdlib = set(sys.stdlib_module_names)
        for path in SOURCES:
            for level, module in _imports(path):
                if level == 1:
                    self.assertIn(module.split(".")[0] or path.stem, PACKAGE_MODULES | {""}, (path.name, module))
                    continue
                self.assertEqual(level, 0, (path.name, module))
                top = module.split(".")[0]
                self.assertIn(top, stdlib, f"{path.name} imports non-stdlib '{module}'")
                self.assertNotIn(top, FORBIDDEN_STDLIB, f"{path.name} imports '{module}'")

    def test_no_import_of_domain_storage_gui_mcp_or_telescope_code(self) -> None:
        """REQ-032, REQ-052: no path from the runtime to canonical domain code."""
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for banned in ("tsn_dss", "telescope", "sqlite", "repository", "http_api", "mcp"):
                self.assertNotIn(f"import {banned}", text, (path.name, banned))
                self.assertNotIn(f"from {banned}", text, (path.name, banned))
            for level, module in _imports(path):
                self.assertFalse(level >= 2, (path.name, module))

    def test_no_vendor_specific_behavior_in_the_core(self) -> None:
        """REQ-054: no Seestar RPC names, RTSP ports, auth files, camera names or modes."""
        for path in SOURCES:
            text = path.read_text(encoding="utf-8").lower()
            for token in ("seestar", "seestarpy", "rtsp", "ascom", "alpaca", "4700", "4554"):
                self.assertNotIn(token, text, (path.name, token))

    def test_no_acquisition_execution_surface(self) -> None:
        """REQ-034, REQ-067: Acquisition Execution stays outside the provider runtime."""
        banned = ("acquisition", "acquire", "capture", "frame", "observation", "session", "dataset")
        for name in device_runtime.__all__:
            self.assertFalse(any(b in name.lower() for b in banned), name)
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    self.assertFalse(any(b in node.name.lower() for b in banned), (path.name, node.name))

    def test_importing_the_package_loads_no_third_party_or_gui_modules(self) -> None:
        """Isolation: a fresh interpreter importing the package stays stdlib-only."""
        code = (
            "import sys, tsn_dss.engine.device_runtime;"
            "bad=[m for m in sys.modules if m.split('.')[0] in %r or m.startswith('tsn_dss.gui') "
            "or m.startswith('tsn_dss.mcp')];"
            "print(','.join(bad))" % (THIRD_PARTY_MARKERS,)
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "")

    def test_existing_telescope_runtime_does_not_know_the_new_package(self) -> None:
        """Compatibility: telescope.py and engine/__init__.py are untouched by DB-01."""
        for relative in ("tsn_dss/engine/telescope.py", "tsn_dss/engine/__init__.py"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("device_runtime", text, relative)

    def test_existing_telescope_adapter_behavior_is_preserved(self) -> None:
        """Compatibility: legacy TelescopeStateService still works beside the new runtime."""
        from tsn_dss.engine.telescope import TelescopeStateService

        service = TelescopeStateService()
        snapshot = service.get_snapshot()
        self.assertEqual(service.get_active_adapter_id(), "simulator")
        self.assertIsNotNone(snapshot.telescope_state)


class NoCanonicalWriteTests(unittest.TestCase):
    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _row_counts(path: Path) -> dict[str, int]:
        connection = connect_database(path)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {t: connection.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
            counts["__user_version__"] = connection.execute("PRAGMA user_version").fetchone()[0]
            return counts
        finally:
            connection.close()

    def test_full_runtime_exercise_leaves_the_canonical_database_untouched(self) -> None:
        """REQ-003, REQ-032, REQ-052, REQ-055, REQ-064: no canonical domain writes."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tsn.db"
            initialize_database(db_path).close()
            before_digest, before_counts = self._digest(db_path), self._row_counts(db_path)

            registry = make_registry()
            runtime = registry.register(make_provider())
            runtime.discover()
            connection = runtime.connect(runtime.open_connection(runtime.last_discovery.devices[0]))
            runtime.refresh_evidence(connection)
            runtime.capability_report(connection)
            runtime.read_telemetry(connection)
            runtime.describe_preview(connection)
            runtime.disconnect(connection)
            runtime.connect(runtime.open_connection(runtime.last_discovery.devices[0]))

            self.assertEqual(self._digest(db_path), before_digest)
            self.assertEqual(self._row_counts(db_path), before_counts)
            self.assertGreater(len(before_counts), 5)

    def test_runtime_objects_hold_no_database_or_domain_handles(self) -> None:
        """REQ-052: the runtime's attributes are runtime-only types."""
        import sqlite3

        runtime, provider, connection = connected_runtime()
        for owner in (runtime, connection, provider):
            slots = getattr(owner, "__slots__", None)
            names = list(slots) if slots else list(vars(owner))
            for name in names:
                self.assertNotIsInstance(getattr(owner, name), sqlite3.Connection, name)

    def test_runtime_models_expose_no_domain_identity_fields(self) -> None:
        """REQ-008, REQ-032, REQ-064, REQ-065: models do not carry domain identities."""
        from dataclasses import fields

        domain_fields = {
            "project_id", "session_id", "observation_id", "target_id", "session_plan_id",
            "acquisition_plan_id", "mosaic_plan_id", "capture_id", "frame_id", "dataset_id",
            "processing_run_id", "session_context_fact_id", "session_event_id", "site_id",
        }
        for name in device_runtime.__all__:
            value = getattr(device_runtime, name)
            if hasattr(value, "__dataclass_fields__"):
                self.assertTrue(
                    {f.name for f in fields(value)}.isdisjoint(domain_fields), name
                )

    def test_provider_runtime_state_proves_no_domain_fact(self) -> None:
        """REQ-064: runtime objects never expose a claim that a domain record exists."""
        runtime, _, connection = connected_runtime()
        for obj in (runtime.descriptor, connection.device, runtime.capability_report(connection),
                    runtime.read_telemetry(connection), runtime.describe_preview(connection)):
            public = {n for n in dir(obj) if not n.startswith("_")}
            self.assertTrue(public.isdisjoint({"session", "observation", "capture", "frame", "dataset"}), obj)

    def test_provider_native_labels_do_not_replace_target_identity(self) -> None:
        """REQ-065: a device label is a plain string with no Target semantics."""
        device = connected_runtime()[2].device
        self.assertIsInstance(device.label, str)
        self.assertFalse(hasattr(device, "target"))
        self.assertFalse(hasattr(device, "target_id"))


if __name__ == "__main__":
    unittest.main()
