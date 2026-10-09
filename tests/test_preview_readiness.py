"""DB-03 Wave 2: neutral readiness gate."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tsn_dss.engine.device_runtime.models import PreviewAvailability as A
from tsn_dss.engine.device_runtime.preview_readiness import (
    GateReason as G,
    ReadinessEvidence,
    ReadinessEvidenceProvider,
    ReadinessGate,
    ReadinessIdentity,
    evaluate_readiness,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
IDENT = ReadinessIdentity("prov", "conn-1", "dev-1")
MAX = timedelta(seconds=10)


def ev(**kw):
    values = dict(provider_id="prov", connection_id="conn-1", device_ref="dev-1", source_label="cam_a",
                  availability=A.AVAILABLE, host_observed_at=T0)
    values.update(kw)
    return ReadinessEvidence(**values)


def decide(evidence, now=T0, **kw):
    return evaluate_readiness(evidence, identity=IDENT, source_label="cam_a", now=now, max_age=MAX, **kw)


class PureGateTests(unittest.TestCase):
    def test_only_fresh_available_allows(self) -> None:
        d = decide(ev())
        self.assertEqual((d.allowed, d.reason), (True, G.ALLOWED))
        self.assertEqual(decide(ev(availability=A.UNKNOWN)).reason, G.UNKNOWN)
        self.assertEqual(decide(ev(availability=A.UNAVAILABLE)).reason, G.UNAVAILABLE)
        self.assertEqual(decide(None).reason, G.NO_EVIDENCE)
        for availability in (A.UNKNOWN, A.UNAVAILABLE):
            self.assertFalse(decide(ev(availability=availability)).allowed)
        self.assertFalse(decide(None).allowed)

    def test_age_boundary(self) -> None:
        self.assertTrue(decide(ev(), now=T0 + MAX).allowed)
        d = decide(ev(), now=T0 + MAX + timedelta(microseconds=1))
        self.assertEqual((d.allowed, d.reason), (False, G.STALE_EVIDENCE))

    def test_stale_unavailable_still_reports_stale_first(self) -> None:
        self.assertEqual(decide(ev(availability=A.UNAVAILABLE), now=T0 + timedelta(minutes=5)).reason, G.STALE_EVIDENCE)

    def test_identity_must_match_every_part(self) -> None:
        for change in ({"provider_id": "other"}, {"connection_id": "conn-2"}, {"device_ref": "dev-2"}, {"source_label": "cam_b"}):
            d = decide(ev(**change))
            self.assertEqual((d.allowed, d.reason), (False, G.IDENTITY_MISMATCH), change)

    def test_identity_checked_before_availability(self) -> None:
        self.assertEqual(decide(ev(connection_id="conn-2", availability=A.UNKNOWN)).reason, G.IDENTITY_MISMATCH)

    def test_future_evidence_is_clock_regression(self) -> None:
        self.assertEqual(decide(ev(host_observed_at=T0 + timedelta(seconds=1))).reason, G.CLOCK_REGRESSION)

    def test_not_before_boundary(self) -> None:
        self.assertEqual(decide(ev(), not_before=T0).reason, G.PREDATES_LOSS)
        self.assertEqual(decide(ev(), not_before=T0 + timedelta(seconds=1), now=T0 + timedelta(seconds=2)).reason, G.PREDATES_LOSS)
        self.assertTrue(decide(ev(host_observed_at=T0 + timedelta(seconds=1)), not_before=T0, now=T0 + timedelta(seconds=1)).allowed)

    def test_argument_validation(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_readiness(ev(), identity=IDENT, source_label="cam_a", now=datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            evaluate_readiness(ev(), identity=IDENT, source_label="cam_a", now=T0, max_age=timedelta(0))
        with self.assertRaises(ValueError):
            ReadinessGate(_Provider(None), max_age=timedelta(0))

    def test_evidence_validation(self) -> None:
        for change in ({"host_observed_at": datetime(2026, 1, 1)}, {"source_label": "bad label"}, {"connection_id": " "}, {"availability": "available"}):
            with self.assertRaises(ValueError, msg=str(change)):
                ev(**change)


class _Provider:
    def __init__(self, result) -> None:
        self.result = result
        self.reads = 0

    def read_evidence(self, label):
        self.reads += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class GateObjectTests(unittest.TestCase):
    def test_provider_protocol(self) -> None:
        self.assertIsInstance(_Provider(None), ReadinessEvidenceProvider)
        self.assertEqual({n for n in dir(ReadinessEvidenceProvider) if not n.startswith("_")}, {"read_evidence"})

    def test_every_check_reads_anew(self) -> None:
        provider = _Provider(ev())
        gate = ReadinessGate(provider, max_age=MAX)
        for _ in range(3):
            self.assertTrue(gate.check(IDENT, "cam_a", T0).allowed)
        self.assertEqual(provider.reads, 3)

    def test_provider_failure_is_a_denial(self) -> None:
        for result in (RuntimeError("x"), "not evidence", 5):
            d = ReadinessGate(_Provider(result)).check(IDENT, "cam_a", T0)
            self.assertEqual((d.allowed, d.reason), (False, G.EVIDENCE_ERROR), result)

    def test_gate_has_no_connection_state_input(self) -> None:
        import inspect

        params = set(inspect.signature(ReadinessGate.check).parameters)
        self.assertEqual(params, {"self", "identity", "source_label", "now", "not_before"})


if __name__ == "__main__":
    unittest.main()
