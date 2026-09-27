from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from tsn_dss.domain.models import Site
from tsn_dss.engine.astronomy import (
    AstronomicalConditionsService,
    AstronomicalTargetContext,
    classify_sky_state,
)


class AstronomicalConditionsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AstronomicalConditionsService()
        self.site = Site(
            id="site:warsaw",
            name="Warsaw",
            latitude_deg=52.2297,
            longitude_deg=21.0122,
            elevation_m=100.0,
        )

    def test_classify_sky_state_uses_standard_solar_altitude_thresholds(self) -> None:
        self.assertEqual(classify_sky_state(5.0), "day")
        self.assertEqual(classify_sky_state(-3.0), "civil_twilight")
        self.assertEqual(classify_sky_state(-9.0), "nautical_twilight")
        self.assertEqual(classify_sky_state(-15.0), "astronomical_twilight")
        self.assertEqual(classify_sky_state(-25.0), "astronomical_night")

    def test_fetch_conditions_without_target_returns_site_level_astronomy(self) -> None:
        snapshot = self.service.fetch_conditions(
            self.site,
            reference_time_utc="2026-10-15T20:00:00Z",
            forecast_hours=4,
        )

        self.assertEqual(snapshot.site_id, self.site.id)
        self.assertEqual(snapshot.provider, "tsn-dss-astronomy")
        self.assertEqual(len(snapshot.hourly), 4)
        self.assertIsNotNone(snapshot.current.sun_altitude_deg)
        self.assertIsNotNone(snapshot.current.moon_azimuth_deg)
        self.assertIsNotNone(snapshot.current.moon_illumination_pct)
        self.assertIsNone(snapshot.current.target)
        self.assertIn("moon_azimuth_deg", snapshot.hourly[0].to_dict())
        self.assertIsNotNone(snapshot.hourly[0].moon_azimuth_deg)

    def test_fetch_conditions_for_target_returns_alt_az_airmass_and_windows(self) -> None:
        snapshot = self.service.fetch_conditions(
            self.site,
            reference_time_utc="2026-10-15T20:00:00Z",
            forecast_hours=3,
            target=AstronomicalTargetContext(
                target_name="M31",
                ra_deg=10.6847083,
                dec_deg=41.26875,
                source_kind="target",
                source_id="target:m31",
            ),
        )

        target = snapshot.current.target
        assert target is not None
        self.assertEqual(target["target_name"], "M31")
        self.assertGreater(target["altitude_deg"], 20.0)
        self.assertIsNotNone(target["azimuth_deg"])
        self.assertIsNotNone(target["airmass"])
        self.assertIsNotNone(target["moon_separation_deg"])
        self.assertIn(target["above_horizon_window_status"], {"windowed", "always_up"})
        self.assertIn(target["observation_window_status"], {"windowed", "always_observable", "not_observable"})

    def test_target_that_never_rises_reports_unobservable_window(self) -> None:
        snapshot = self.service.fetch_conditions(
            self.site,
            reference_time_utc="2026-10-15T20:00:00Z",
            target=AstronomicalTargetContext(
                target_name="SMC-like",
                ra_deg=13.1583 * 15.0,
                dec_deg=-72.8,
                source_kind="manual",
            ),
        )

        target = snapshot.current.target
        assert target is not None
        self.assertFalse(target["above_horizon"])
        self.assertEqual(target["above_horizon_window_status"], "never_up")
        self.assertEqual(target["observation_window_status"], "not_observable")

    def test_high_latitude_summer_handles_missing_astronomical_night(self) -> None:
        tromsø = Site(
            id="site:tromso",
            name="Tromsø",
            latitude_deg=69.6492,
            longitude_deg=18.9553,
            elevation_m=20.0,
        )
        snapshot = self.service.fetch_conditions(
            tromsø,
            reference_time_utc="2026-06-21T12:00:00Z",
            target=AstronomicalTargetContext(
                target_name="M31",
                ra_deg=10.6847083,
                dec_deg=41.26875,
                source_kind="target",
            ),
        )

        self.assertIsNone(snapshot.current.astronomical_night_start_utc)
        self.assertIsNone(snapshot.current.astronomical_night_end_utc)
        target = snapshot.current.target
        assert target is not None
        self.assertEqual(target["above_horizon_window_status"], "no_astronomical_night")
        self.assertEqual(target["observation_window_status"], "no_astronomical_night")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ObservingNightScopeTests(unittest.TestCase):
    """scope="night": the observing night as facts. Synthetic Sites, no real coordinates."""

    BOUNDARY_ORDER = [
        "sunset_utc",
        "civil_twilight_evening_end_utc",
        "nautical_twilight_evening_end_utc",
        "astronomical_night_start_utc",
        "astronomical_night_end_utc",
        "nautical_twilight_morning_start_utc",
        "civil_twilight_morning_start_utc",
        "sunrise_utc",
    ]

    def setUp(self) -> None:
        self.service = AstronomicalConditionsService()
        self.site = Site(id="site:synthetic-50n", name="Synthetic 50N", latitude_deg=50.0, longitude_deg=19.0, elevation_m=0.0)

    def night(self, reference: str, site: Site | None = None):
        return self.service.fetch_conditions(site or self.site, reference_time_utc=reference, scope="night")

    def assert_same_instant(self, left: str | None, right: str | None) -> None:
        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertLessEqual(abs((_utc(left) - _utc(right)).total_seconds()), 2)

    def test_autumn_night_has_ordered_exact_boundaries_and_full_hourly_coverage(self) -> None:
        snapshot = self.night("2026-09-27T12:00:00Z")
        night = snapshot.to_dict()["night"]

        self.assertEqual(night["status"], "ok")
        instants = [_utc(night[name]) for name in self.BOUNDARY_ORDER]
        self.assertEqual(instants, sorted(instants))
        self.assertEqual(night["civil_twilight_evening_start_utc"], night["sunset_utc"])
        self.assertEqual(night["astronomical_twilight_evening_end_utc"], night["astronomical_night_start_utc"])

        hourly = [_utc(hour.time_utc) for hour in snapshot.hourly]
        self.assertLessEqual(hourly[0], _utc(night["sunset_utc"]))
        self.assertGreater(hourly[0] + timedelta(hours=1), _utc(night["sunset_utc"]))
        self.assertGreaterEqual(hourly[-1], _utc(night["sunrise_utc"]))
        self.assertLess(hourly[-1] - timedelta(hours=1), _utc(night["sunrise_utc"]))
        self.assertTrue(all(b - a == timedelta(hours=1) for a, b in zip(hourly, hourly[1:])))
        self.assertEqual(snapshot.current.time_utc, "2026-09-27T12:00:00Z")

    def test_daytime_evening_after_midnight_and_dawn_anchor_the_same_night(self) -> None:
        references = ["2026-09-27T12:00:00Z", "2026-09-27T16:45:00Z", "2026-09-27T23:30:00Z", "2026-09-28T04:20:00Z"]
        nights = [self.night(reference).to_dict()["night"] for reference in references]

        for night in nights[1:]:
            for name in self.BOUNDARY_ORDER:
                self.assert_same_instant(night[name], nights[0][name])
        self.assertTrue(nights[0]["sunset_utc"].startswith("2026-09-27T16:"))
        self.assertTrue(nights[0]["sunrise_utc"].startswith("2026-09-28T04:"))

    def test_after_sunrise_moves_to_the_next_night(self) -> None:
        night = self.night("2026-09-28T08:00:00Z").to_dict()["night"]
        self.assertTrue(night["sunset_utc"].startswith("2026-09-28T"))

    def test_summer_night_without_astronomical_darkness_keeps_meaningful_nulls(self) -> None:
        night = self.night("2027-06-21T12:00:00Z").to_dict()["night"]

        self.assertEqual(night["status"], "ok")
        self.assertIsNone(night["astronomical_night_start_utc"])
        self.assertIsNone(night["astronomical_night_end_utc"])
        self.assertIsNotNone(night["nautical_twilight_evening_end_utc"])
        self.assertIsNotNone(night["nautical_twilight_morning_start_utc"])
        self.assertLess(_utc(night["nautical_twilight_evening_end_utc"]), _utc(night["nautical_twilight_morning_start_utc"]))

    def test_sun_that_does_not_set_returns_status_and_no_hourly(self) -> None:
        arctic = Site(id="site:synthetic-78n", name="Synthetic 78N", latitude_deg=78.0, longitude_deg=15.0, elevation_m=0.0)
        snapshot = self.night("2027-06-21T12:00:00Z", arctic)
        night = snapshot.to_dict()["night"]

        self.assertEqual(night["status"], "sun_does_not_set")
        self.assertIsNone(night["sunset_utc"])
        self.assertIsNone(night["sunrise_utc"])
        self.assertIsNone(night["moon_up_at_sunset"])
        self.assertEqual(night["moon_events"], [])
        self.assertEqual(snapshot.hourly, [])

    def test_moon_already_up_at_sunset_and_setting_during_the_night(self) -> None:
        snapshot = self.night("2026-09-22T12:00:00Z")
        night = snapshot.to_dict()["night"]

        self.assertTrue(night["moon_up_at_sunset"])
        self.assertEqual([event["kind"] for event in night["moon_events"]], ["moonset"])
        moonset = _utc(night["moon_events"][0]["time_utc"])
        self.assertTrue(_utc(night["sunset_utc"]) < moonset < _utc(night["sunrise_utc"]))
        for hour in snapshot.hourly:
            if _utc(hour.time_utc) >= _utc(night["sunset_utc"]) and _utc(hour.time_utc) + timedelta(minutes=5) < moonset:
                self.assertGreater(hour.moon_altitude_deg, 0.0)
            if _utc(hour.time_utc) > moonset + timedelta(minutes=5):
                self.assertLess(hour.moon_altitude_deg, 0.0)

    def test_moonrise_shortly_after_sunset_is_found(self) -> None:
        # astroplan's moon_rise_time misses this rise (about 5 minutes after sunset); the night block must not.
        night = self.night("2026-09-27T12:00:00Z").to_dict()["night"]

        self.assertFalse(night["moon_up_at_sunset"])
        self.assertEqual([event["kind"] for event in night["moon_events"]], ["moonrise"])
        moonrise = _utc(night["moon_events"][0]["time_utc"])
        self.assertTrue(timedelta(0) < moonrise - _utc(night["sunset_utc"]) < timedelta(minutes=15))

    def test_invalid_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope"):
            self.service.fetch_conditions(self.site, reference_time_utc="2026-09-27T12:00:00Z", scope="week")

    def test_default_scope_is_unchanged(self) -> None:
        default = self.service.fetch_conditions(self.site, reference_time_utc="2026-09-27T16:45:00Z", forecast_hours=3)
        rolling = self.service.fetch_conditions(self.site, reference_time_utc="2026-09-27T16:45:00Z", forecast_hours=3, scope="rolling")

        self.assertNotIn("night", default.to_dict())
        self.assertEqual(default.to_dict(), rolling.to_dict())
        self.assertEqual([hour.time_utc for hour in default.hourly], ["2026-09-27T16:00:00Z", "2026-09-27T17:00:00Z", "2026-09-27T18:00:00Z"])


if __name__ == "__main__":
    unittest.main()
