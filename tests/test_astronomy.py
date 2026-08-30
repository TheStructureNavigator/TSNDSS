from __future__ import annotations

import unittest

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
        self.assertIsNotNone(snapshot.current.moon_illumination_pct)
        self.assertIsNone(snapshot.current.target)

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


if __name__ == "__main__":
    unittest.main()
