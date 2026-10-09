"""DB-02 architecture, read-only safety, secret hygiene and domain-ownership boundaries."""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tsn_dss.engine import seestar_provider
from tsn_dss.engine.sqlite.db import connect_database, initialize_database

try:
    from seestar_support import FakeSeestarTransport, connected, make_config, make_runtime
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_support import FakeSeestarTransport, connected, make_config, make_runtime

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(seestar_provider.__file__).resolve().parent
SOURCES = sorted(PACKAGE.glob("*.py"))
SCRIPT = ROOT / "tools" / "seestar_readonly_validate.py"
FIXTURES = ROOT / "tests" / "fixtures" / "seestar"

STATE_CHANGING = (
    "scope_move_to_horizon", "scope_park", "scope_goto", "scope_speed_move", "scope_sync", "scope_set_track_state",
    "iscope_start_view", "iscope_stop_view", "iscope_start_stack", "pi_output_set2", "pi_reboot", "pi_shutdown",
    "move_focuser", "set_setting", "set_control_value", "set_image_transfer_mode", "start_auto_focuse",
    "start_create_dark", "start_solve", "start_polar_align", "start_scan_planet", "play_sound",
    "send_command", "random_command", "begin_streaming",
)
HANDSHAKE = ("get_verify_str", "verify_client", "pi_is_verified")
FORBIDDEN_STDLIB = {
    "sqlite3", "subprocess", "multiprocessing", "ctypes", "asyncio", "http", "urllib", "ftplib", "smtplib",
    "telnetlib", "xmlrpc", "shelve", "dbm", "logging", "os", "pathlib", "tempfile", "shutil",
}
PEM_MARKER = "BEGIN " + "RSA PRIVATE KEY"  # assembled so this file itself carries no marker


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield 0, alias.name, node
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or "", node


class ImportBoundaryTests(unittest.TestCase):
    def test_package_location(self) -> None:
        self.assertEqual(PACKAGE, ROOT / "tsn_dss" / "engine" / "seestar_provider")

    def test_only_stdlib_neutral_runtime_and_package_imports(self) -> None:
        stdlib = set(sys.stdlib_module_names)
        modules = {p.stem for p in SOURCES}
        for path in SOURCES:
            for level, module, node in _imports(path):
                if level == 1:
                    self.assertIn(module.split(".")[0] or "", modules | {""}, (path.name, module))
                    continue
                if level == 2:
                    self.assertTrue(module == "device_runtime" or module.startswith("device_runtime."), (path.name, module))
                    continue
                top = module.split(".")[0]
                if top == "cryptography":
                    continue  # checked separately: lazy and only in auth.py
                self.assertIn(top, stdlib, f"{path.name} imports non-stdlib '{module}'")
                self.assertNotIn(top, FORBIDDEN_STDLIB, f"{path.name} imports '{module}'")

    def test_socket_is_imported_only_by_the_transport(self) -> None:
        for path in SOURCES:
            names = {m.split(".")[0] for _l, m, _n in _imports(path) if _l == 0}
            if path.name != "transport.py":
                self.assertNotIn("socket", names, path.name)

    def test_cryptography_is_lazy_and_only_in_auth(self) -> None:
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    self.assertNotIn("cryptography", ast.dump(node), f"module-level cryptography import in {path.name}")
            lazy = [
                n for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom)) and "cryptography" in ast.dump(n)
            ]
            self.assertEqual(bool(lazy), path.name == "auth.py", path.name)

    def test_the_gpl_client_library_is_never_imported_or_copied(self) -> None:
        for path in SOURCES:
            for _level, module, _node in _imports(path):
                self.assertNotEqual(module.split(".")[0], "seestarpy", path.name)
            self.assertNotIn("GNU GENERAL PUBLIC LICENSE", path.read_text(encoding="utf-8").upper(), path.name)

    def test_vendor_neutral_runtime_does_not_know_this_package(self) -> None:
        for path in sorted((ROOT / "tsn_dss" / "engine" / "device_runtime").glob("*.py")):
            self.assertNotIn("seestar", path.read_text(encoding="utf-8").lower(), path.name)
        for relative in ("tsn_dss/engine/telescope.py", "tsn_dss/engine/__init__.py"):
            self.assertNotIn("seestar_provider", (ROOT / relative).read_text(encoding="utf-8"), relative)

    def test_no_domain_storage_gui_mcp_or_legacy_adapter_imports(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for banned in ("engine.sqlite", "engine.telescope", "gui", "mcp", "repository"):
                self.assertNotRegex(text, rf"^\s*(from|import)\s+[\w.]*{banned}", path.name)

    def test_importing_the_package_performs_no_network_access_and_loads_no_third_party_code(self) -> None:
        code = (
            "import socket, sys\n"
            "def boom(*a, **k):\n"
            "    raise AssertionError('network access at import')\n"
            "socket.socket = boom; socket.create_connection = boom; socket.getaddrinfo = boom\n"
            "socket.gethostbyname = boom; socket.gethostname = boom\n"
            "import tsn_dss.engine.seestar_provider\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('cryptography','seestarpy','astropy','numpy','requests')]\n"
            "print(','.join(bad))\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


class ReadOnlySafetyTests(unittest.TestCase):
    def test_no_state_changing_or_generic_method_name_appears_anywhere_in_the_package(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for name in STATE_CHANGING:
                self.assertNotIn(name, text, f"{path.name}: {name}")

    def test_handshake_method_names_exist_only_in_auth(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for name in HANDSHAKE:
                if path.name == "auth.py":
                    self.assertIn(name, text)
                else:
                    self.assertNotIn(name, text, f"{path.name}: {name}")

    def test_no_print_eval_or_file_access_outside_the_signer(self) -> None:
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            self.assertEqual(called & {"print", "input", "exec", "eval", "compile", "__import__"}, set(), path.name)
            if path.name != "auth.py":
                self.assertNotIn("open", called, path.name)

    def test_no_acquisition_or_capture_surface(self) -> None:
        """REQ-034, REQ-067: Acquisition Execution stays outside the Provider."""
        banned = ("acquisition", "acquire", "capture", "observation", "session", "dataset")
        for name in seestar_provider.__all__:
            self.assertFalse(any(b in name.lower() for b in banned) or name.lower().startswith("frame"), name)
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    self.assertFalse(any(b in node.name.lower() for b in banned), (path.name, node.name))
                if isinstance(node, ast.ClassDef):  # wire "frames" are protocol framing, a domain Frame class is not
                    self.assertNotIn("frame", node.name.lower(), (path.name, node.name))

    def test_no_ipv4_literals_other_than_the_limited_broadcast_and_no_fallback_host(self) -> None:
        for path in SOURCES:
            literals = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", path.read_text(encoding="utf-8")))
            self.assertLessEqual(literals, {"255.255.255.255"}, path.name)
        self.assertNotIn("10.0." + "0.1", "".join(p.read_text(encoding="utf-8") for p in SOURCES))

    def test_every_transport_call_during_a_full_session_is_an_allow_listed_read(self) -> None:
        runtime, _, transport, connection = connected()
        runtime.refresh_evidence(connection)
        runtime.capability_report(connection)
        runtime.read_telemetry(connection)
        runtime.describe_preview(connection)
        runtime.disconnect(connection)
        self.assertTrue({c[0] for c in transport.calls} <= {"read_device_state", "read_app_state", "test_connection"})
        self.assertEqual(
            {n for n in dir(FakeSeestarTransport) if not n.startswith("_") and callable(getattr(FakeSeestarTransport, n))} - {"fail_next"},
            {"read_device_state", "read_app_state", "test_connection", "discover_via_udp"},
        )


class SecretHygieneTests(unittest.TestCase):
    def test_no_key_material_or_real_addresses_in_package_tests_fixtures_or_script(self) -> None:
        files = [*SOURCES, *FIXTURES.glob("*"), SCRIPT, *sorted((ROOT / "tests").glob("*seestar*.py"))]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(PEM_MARKER, text, path.name)
            self.assertNotIn("PRIVATE KEY", text.replace("PRIVATE " + "KEY", ""), path.name)
            for address in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
                allowed = address.startswith("192.0.2.") or address in {"255.255.255.255", "255.255.255.0"}
                self.assertTrue(allowed, f"{path.name}: {address}")

    def test_no_pem_files_exist_in_the_repository_tree_under_dsstools(self) -> None:
        for suffix in ("*.pem", "*.key"):
            self.assertEqual(list((ROOT / "tsn_dss").rglob(suffix)) + list((ROOT / "tests").rglob(suffix)) + list((ROOT / "tools").rglob(suffix)), [])

    def test_fixtures_are_marked_synthetic(self) -> None:
        for path in FIXTURES.glob("*.json"):
            self.assertIn('"_synthetic": true', path.read_text(encoding="utf-8"), path.name)


class OwnershipBoundaryTests(unittest.TestCase):
    def test_full_exercise_leaves_the_canonical_database_untouched(self) -> None:
        """REQ-003, REQ-032, REQ-052, REQ-055, REQ-064: no canonical domain writes."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tsn.db"
            initialize_database(db_path).close()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()
            runtime, _, _, connection = connected()
            runtime.refresh_evidence(connection)
            runtime.capability_report(connection)
            runtime.read_telemetry(connection)
            runtime.describe_preview(connection)
            runtime.disconnect(connection)
            self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), before)
            conn = connect_database(db_path)
            try:
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 5)
            finally:
                conn.close()

    def test_runtime_evidence_has_no_domain_identity_fields(self) -> None:
        """REQ-008, REQ-032, REQ-064, REQ-065."""
        from dataclasses import fields

        domain = {"project_id", "session_id", "observation_id", "target_id", "capture_id", "frame_id",
                  "dataset_id", "processing_run_id", "site_id", "target_name", "ra_hours", "dec_deg"}
        runtime, _, _, connection = connected()
        for obj in (connection.device, runtime.capability_report(connection), runtime.read_telemetry(connection),
                    runtime.describe_preview(connection)):
            self.assertTrue({f.name for f in fields(type(obj))}.isdisjoint(domain), type(obj).__name__)


class OperatorScriptStaticTests(unittest.TestCase):
    def test_operator_script_exists_and_is_read_only_by_construction(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        ast.parse(text)
        for name in STATE_CHANGING:
            self.assertNotIn(name, text, name)
        for name in HANDSHAKE:
            self.assertNotIn(name, text, name)
        self.assertNotIn("seestarpy", text)
        self.assertIn("TcpSeestarTransport", text)

    def test_operator_script_never_echoes_host_key_path_or_serial(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        for call in re.findall(r"print\((.*?)\)\n", text, flags=re.S):
            self.assertNotRegex(call, r"host|key|sn\b|serial|device_ref", call)


if __name__ == "__main__":
    unittest.main()
