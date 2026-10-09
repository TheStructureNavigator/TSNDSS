"""DB-03 Wave 3: preview configuration, stream endpoints and redaction."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from tsn_dss.engine.seestar_preview import SeestarPreviewConfig, StreamEndpoint
from tsn_dss.engine.seestar_provider.errors import SeestarConfigError

try:
    from seestar_support import HOST
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_support import HOST


class ConfigTests(unittest.TestCase):
    def test_defaults_and_endpoints(self) -> None:
        config = SeestarPreviewConfig(host=HOST)
        self.assertEqual(config.cameras, ("main", "wide"))
        self.assertEqual(config.endpoint("main").address, f"rtsp://{HOST}:4554/stream")
        self.assertEqual(config.endpoint("wide").address, f"rtsp://{HOST}:4555/stream")

    def test_single_camera_config(self) -> None:
        config = SeestarPreviewConfig(host=HOST, cameras=("wide",))
        self.assertEqual(config.endpoint("wide").camera, "wide")
        with self.assertRaises(SeestarConfigError) as ctx:
            config.endpoint("main")
        self.assertEqual(ctx.exception.category, "camera_not_configured")

    def test_hostname_and_ipv6_literal(self) -> None:
        self.assertEqual(SeestarPreviewConfig(host="scope.local").endpoint("main").address, "rtsp://scope.local:4554/stream")
        self.assertEqual(SeestarPreviewConfig(host="2001:db8::1").endpoint("wide").address, "rtsp://[2001:db8::1]:4555/stream")

    def test_hosts_that_could_alter_the_address_are_rejected(self) -> None:
        bad = (
            "", " ", None, 5, f"{HOST} ", f" {HOST}", f"{HOST}:4554", f"rtsp://{HOST}", f"{HOST}/x", f"{HOST}?a=1",
            f"user@{HOST}", f"{HOST}#f", "ho st", "host\n", "host/../x", "ho\\st", "-bad.example", "a..b",
        )
        for host in bad:
            with self.assertRaises(SeestarConfigError, msg=repr(host)) as ctx:
                SeestarPreviewConfig(host=host)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.category, "invalid_host")
            self.assertEqual(str(ctx.exception), "invalid_host")

    def test_camera_validation(self) -> None:
        for cameras in ((), ("main", "main"), ("main", "wide", "main"), ("MAIN",), ("tele",), ["main"], "main"):
            with self.assertRaises(SeestarConfigError, msg=repr(cameras)) as ctx:
                SeestarPreviewConfig(host=HOST, cameras=cameras)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.category, "invalid_cameras")

    def test_readiness_age_validation(self) -> None:
        for value in (0, -1, 60.1, True, "10", None):
            with self.assertRaises(SeestarConfigError, msg=repr(value)) as ctx:
                SeestarPreviewConfig(host=HOST, readiness_max_age_s=value)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.category, "invalid_readiness_age")
        SeestarPreviewConfig(host=HOST, readiness_max_age_s=60)

    def test_ports_and_path_cannot_be_configured(self) -> None:
        for field in ("port", "main_port", "wide_port", "path", "url"):
            with self.assertRaises(TypeError):
                SeestarPreviewConfig(host=HOST, **{field: 1})  # type: ignore[arg-type]

    def test_config_is_immutable(self) -> None:
        config = SeestarPreviewConfig(host=HOST)
        with self.assertRaises(FrozenInstanceError):
            config.host = "other.example"  # type: ignore[misc]


class RedactionTests(unittest.TestCase):
    def test_host_never_appears_in_representations(self) -> None:
        config = SeestarPreviewConfig(host=HOST)
        endpoint = config.endpoint("main")
        for obj in (config, endpoint):
            for text in (repr(obj), str(obj), f"{obj}", f"{obj!r}"):
                self.assertNotIn(HOST, text)
                self.assertNotIn("rtsp://", text)
        self.assertNotIn(HOST, repr(config.redacted()))
        self.assertEqual(config.redacted()["host"], "<set>")
        self.assertIn("main", repr(endpoint))

    def test_endpoint_is_the_only_holder_of_the_address(self) -> None:
        endpoint = SeestarPreviewConfig(host=HOST).endpoint("wide")
        self.assertIsInstance(endpoint, StreamEndpoint)
        self.assertIn(HOST, endpoint.address)
        self.assertNotIn(HOST, repr(endpoint))

    def test_errors_never_contain_the_offending_host(self) -> None:
        secret = f"{HOST}/secret-path"
        with self.assertRaises(SeestarConfigError) as ctx:
            SeestarPreviewConfig(host=secret)
        self.assertNotIn("secret", str(ctx.exception) + repr(ctx.exception))


if __name__ == "__main__":
    unittest.main()
