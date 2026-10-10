"""DB-05 Slice 3: the operator permit and the separate operator clearance. In memory, offline, no device."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tsn_dss.engine.device_runtime.command_executor import ClearanceAuthorizer, CommandAuthorizer
from tsn_dss.engine.seestar_control import (
    ARM_DEPLOY, ARM_PARK, COMMANDS, SCENERY_START, SCENERY_STOP, OperatorClearance, OperatorPermit, PermitError,
)

try:
    from test_seestar_control_runtime import build
except ImportError:  # pragma: no cover
    from tests.test_seestar_control_runtime import build

HOUR = timedelta(hours=1)
OK = lambda: "DEPLOY"  # noqa: E731


class FrozenClock:
    """Time moves only when the test says so (the runtime ManualClock steps on every read)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def kind(kind_id):
    return SimpleNamespace(kind_id=kind_id)


def ref_of(connection):
    return SimpleNamespace(provider_id=connection.provider_id, connection_id=connection.connection_id)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        _, self.control, self.handle, _ = build(baseline=False)
        self.clock = FrozenClock()
        self.connection = self.handle.connection
        self.permit = OperatorPermit(operator_id="op", valid_for=HOUR, clock=self.clock)

    def grant(self, **over):
        args = dict(allow_physical_motion=True, interactive=True, confirm=OK)
        args.update(over)
        self.permit.grant(self.connection, **args)

    def ask(self, kind_id="seestar.scenery.start", who="op", connection=None):
        return self.permit.is_authorized(who, kind(kind_id), ref_of(connection or self.connection))


class PermitTests(Fixture):
    def test_it_is_a_command_authorizer_and_denies_until_granted(self) -> None:
        self.assertIsInstance(self.permit, CommandAuthorizer)
        for kind_id in COMMANDS:
            self.assertFalse(self.ask(kind_id))

    def test_grant_allows_the_non_arm_kinds_for_the_operator(self) -> None:
        self.grant()
        self.assertTrue(self.ask(SCENERY_START))
        self.assertTrue(self.ask(SCENERY_STOP))

    def test_grant_needs_every_part_of_the_consent(self) -> None:
        cases = [
            (dict(allow_physical_motion=False), "physical_motion_not_allowed"),
            (dict(interactive=False), "interactive_consent_required"),
            (dict(confirm=lambda: "deploy please"), "confirmation_not_given"),
            (dict(confirm=lambda: ""), "confirmation_not_given"),
            (dict(confirm=lambda: None), "confirmation_not_given"),
        ]
        for over, token in cases:
            with self.subTest(token=token, over=list(over)):
                with self.assertRaises(PermitError) as caught:
                    self.grant(**over)
                self.assertEqual(str(caught.exception), token)
                self.assertFalse(self.ask(SCENERY_START))

    def test_the_prompt_is_not_asked_when_the_flag_or_terminal_is_missing(self) -> None:
        asked = []
        for over in (dict(allow_physical_motion=False), dict(interactive=False)):
            with self.assertRaises(PermitError):
                self.grant(confirm=lambda: asked.append(1) or "DEPLOY", **over)
        self.assertEqual(asked, [])

    def test_a_missing_device_identity_fails_closed(self) -> None:
        with self.assertRaises(PermitError) as caught:
            self.permit.grant(object(), allow_physical_motion=True, interactive=True, confirm=OK)
        self.assertEqual(str(caught.exception), "device_identity_missing")
        self.assertFalse(self.ask(SCENERY_START))

    def test_it_expires(self) -> None:
        self.grant()
        self.clock.advance(HOUR - timedelta(seconds=1))
        self.assertTrue(self.ask(SCENERY_STOP))
        self.assertEqual(self.permit.remaining(), timedelta(seconds=1))
        self.clock.advance(timedelta(seconds=1))  # the expiry instant itself is already denied
        self.assertFalse(self.ask(SCENERY_STOP))
        self.assertIsNone(self.permit.remaining())

    def test_it_is_bound_to_one_connection_and_one_operator(self) -> None:
        self.grant()
        other = SimpleNamespace(provider_id=self.connection.provider_id, connection_id="another-connection")
        self.assertFalse(self.ask(connection=other))
        self.assertFalse(self.ask(who="someone-else"))
        self.assertFalse(self.permit.is_authorized("op", kind(SCENERY_START), SimpleNamespace(provider_id="x", connection_id="y")))

    def test_a_replaced_connection_is_denied(self) -> None:
        self.grant()
        self.control.runtime.disconnect(self.connection)
        again = self.control.attach()
        self.assertNotEqual(again.connection.connection_id, self.connection.connection_id)
        self.assertFalse(self.ask(connection=again.connection))

    def test_it_covers_only_the_four_kinds(self) -> None:
        self.grant()
        for other in ("seestar.arm.sweep", "", None, "scope_park", "iscope_start_view"):
            self.assertFalse(self.ask(other))

    def test_arm_motion_needs_a_fresh_single_use_confirmation(self) -> None:
        self.grant()
        for arm in (ARM_DEPLOY, ARM_PARK):
            with self.subTest(arm=arm):
                self.assertFalse(self.ask(arm))  # the grant alone never moves the arm
                self.permit.confirm_arm_motion(arm, lambda: "ARM")
                self.assertTrue(self.ask(arm))
                self.assertFalse(self.ask(arm))  # consumed by that admission
        self.permit.confirm_arm_motion(ARM_DEPLOY, lambda: "ARM")
        self.assertFalse(self.ask(ARM_PARK))  # a confirmation is for one kind only
        self.assertTrue(self.ask(ARM_DEPLOY))

    def test_a_wrong_arm_confirmation_or_kind_is_refused(self) -> None:
        self.grant()
        for answer in ("", "arm now", "DEPLOY", None):
            with self.assertRaises(PermitError) as caught:
                self.permit.confirm_arm_motion(ARM_DEPLOY, lambda a=answer: a)
            self.assertEqual(str(caught.exception), "arm_confirmation_not_given")
        with self.assertRaises(PermitError) as caught:
            self.permit.confirm_arm_motion(SCENERY_START, lambda: "ARM")
        self.assertEqual(str(caught.exception), "not_an_arm_motion")
        self.assertFalse(self.ask(ARM_DEPLOY))

    def test_an_arm_confirmation_needs_an_active_permit_and_does_not_outlive_it(self) -> None:
        with self.assertRaises(PermitError) as caught:
            self.permit.confirm_arm_motion(ARM_DEPLOY, lambda: "ARM")
        self.assertEqual(str(caught.exception), "permit_not_active")
        self.grant()
        self.permit.confirm_arm_motion(ARM_DEPLOY, lambda: "ARM")
        self.clock.advance(HOUR)
        self.assertFalse(self.ask(ARM_DEPLOY))

    def test_a_permit_is_granted_once_and_can_be_revoked(self) -> None:
        self.grant()
        with self.assertRaises(PermitError) as caught:
            self.grant()
        self.assertEqual(str(caught.exception), "already_granted")
        self.permit.confirm_arm_motion(ARM_DEPLOY, lambda: "ARM")
        self.permit.revoke()
        self.assertFalse(self.ask(SCENERY_START))
        self.assertFalse(self.ask(ARM_DEPLOY))

    def test_terms_are_required(self) -> None:
        for kwargs, token in (
            (dict(operator_id=" ", valid_for=HOUR, clock=self.clock), "operator_required"),
            (dict(operator_id="op", valid_for=timedelta(0), clock=self.clock), "validity_required"),
            (dict(operator_id="op", valid_for=None, clock=self.clock), "validity_required"),
            (dict(operator_id="op", valid_for=HOUR, clock=None), "clock_required"),
        ):
            with self.assertRaises(PermitError) as caught:
                OperatorPermit(**kwargs)
            self.assertEqual(str(caught.exception), token)

    def test_a_failing_clock_fails_closed(self) -> None:
        self.grant()
        self.permit._clock = lambda: 1 / 0
        self.assertFalse(self.ask(SCENERY_START))

    def test_repr_carries_no_identity(self) -> None:
        self.grant()
        text = repr(self.permit)
        for secret in (self.connection.connection_id, self.connection.device.device_ref, "op"):
            self.assertNotIn(secret, text)


class ClearanceTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.clearance = OperatorClearance(operator_id="op", valid_for=HOUR, clock=self.clock)

    def grant(self, **over):  # the clearance consent
        args = dict(allow_clearance=True, interactive=True, confirm=lambda: "CLEAR")
        args.update(over)
        self.clearance.grant(self.connection, **args)

    def may(self, who="op", device=None):
        return self.clearance.may_clear(who, self.connection.provider_id, device or self.connection.device.device_ref)

    def test_it_denies_until_granted_and_is_a_clearance_authorizer(self) -> None:
        self.assertIsInstance(self.clearance, ClearanceAuthorizer)
        self.assertFalse(self.may())
        self.grant()
        self.assertTrue(self.may())

    def test_it_has_its_own_phrase_and_flag(self) -> None:
        for over, token in ((dict(confirm=OK), "confirmation_not_given"), (dict(allow_clearance=False), "clearance_not_allowed"),
                            (dict(interactive=False), "interactive_consent_required")):
            with self.assertRaises(PermitError) as caught:
                self.grant(**over)
            self.assertEqual(str(caught.exception), token)
        self.assertFalse(self.may())

    def test_it_is_scoped_to_the_device_the_operator_and_the_clock(self) -> None:
        self.grant()
        self.assertFalse(self.may(who="other"))
        self.assertFalse(self.may(device="another-device"))
        self.assertFalse(self.clearance.may_clear("op", "another-provider", self.connection.device.device_ref))
        self.clock.advance(HOUR)
        self.assertFalse(self.may())

    def test_neither_permit_authorizes_the_other_thing(self) -> None:
        self.grant()
        self.assertFalse(hasattr(self.clearance, "is_authorized"))
        self.permit.grant(self.connection, allow_physical_motion=True, interactive=True, confirm=OK)
        self.assertFalse(hasattr(self.permit, "may_clear"))
        fresh = OperatorClearance(operator_id="op", valid_for=HOUR, clock=self.clock)
        self.assertFalse(fresh.may_clear("op", self.connection.provider_id, self.connection.device.device_ref))


if __name__ == "__main__":
    unittest.main()
