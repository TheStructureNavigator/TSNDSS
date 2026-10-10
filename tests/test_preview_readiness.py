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


class GateClockTests(unittest.TestCase):
    """The gate can take a clock and reads it AFTER the evidence (a fresh device read is stamped later than the caller's earlier time)."""

    class Slow:
        """A provider whose read takes ``delay`` of host time and stamps the evidence at the end of the read."""

        def __init__(self, clock, delay, **changes):
            self.clock, self.delay, self.changes = clock, delay, changes

        def read_evidence(self, label):
            self.clock[0] += self.delay
            return ev(source_label=label, host_observed_at=self.clock[0], **self.changes)

    def test_evidence_stamped_during_the_read_is_not_a_clock_regression(self) -> None:
        clock = [T0]
        before = clock[0]                                                  # what a caller captured before asking
        gate = ReadinessGate(self.Slow(clock, timedelta(milliseconds=300)), max_age=MAX)
        old = gate.check(IDENT, "cam_a", before)                           # the old call shape: the time was taken first
        self.assertEqual((old.allowed, old.reason), (False, G.CLOCK_REGRESSION))
        clock[0] = T0
        new = gate.check(IDENT, "cam_a", lambda: clock[0])                 # a clock, read after the evidence
        self.assertEqual((new.allowed, new.reason), (True, G.ALLOWED))
        self.assertEqual(new.evidence_age, timedelta(0))

    def test_a_genuine_regression_is_still_denied_with_a_clock(self) -> None:
        gate = ReadinessGate(_Provider(ev(host_observed_at=T0 + timedelta(seconds=5))), max_age=MAX)
        d = gate.check(IDENT, "cam_a", lambda: T0)                         # the clock after the read is earlier than the evidence
        self.assertEqual((d.allowed, d.reason), (False, G.CLOCK_REGRESSION))

    def test_freshness_identity_and_not_before_still_apply_with_a_clock(self) -> None:
        clock = [T0]
        stale = ReadinessGate(self.Slow(clock, timedelta(0)), max_age=MAX).check(IDENT, "cam_a", lambda: T0 + MAX + timedelta(microseconds=1))
        self.assertEqual((stale.allowed, stale.reason), (False, G.STALE_EVIDENCE))
        mismatch = ReadinessGate(self.Slow(clock, timedelta(0), connection_id="other"), max_age=MAX).check(IDENT, "cam_a", lambda: T0)
        self.assertEqual(mismatch.reason, G.IDENTITY_MISMATCH)
        lost = ReadinessGate(_Provider(ev()), max_age=MAX).check(IDENT, "cam_a", lambda: T0, not_before=T0)
        self.assertEqual(lost.reason, G.PREDATES_LOSS)

    def test_a_failing_or_naive_clock_is_a_denial_or_an_error_never_an_allowance(self) -> None:
        gate = ReadinessGate(_Provider(ev()), max_age=MAX)
        def broken():
            raise RuntimeError("clock")
        d = gate.check(IDENT, "cam_a", broken)
        self.assertEqual((d.allowed, d.reason), (False, G.EVIDENCE_ERROR))
        with self.assertRaises(ValueError):
            gate.check(IDENT, "cam_a", lambda: datetime(2026, 1, 1))       # naive time: rejected as before

    def test_a_plain_datetime_still_works(self) -> None:
        self.assertTrue(ReadinessGate(_Provider(ev()), max_age=MAX).check(IDENT, "cam_a", T0).allowed)


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
