from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import LocalHorizonPoint, Site, Target
from tsn_dss.engine.astronomy import AstronomicalConditionsCurrent, AstronomicalConditionsSnapshot
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.visibility import TargetVisibilityService, analyze_local_horizon_visibility


class FakeAstronomyService:
    provider_name = "fake"

    def __init__(self, *, altitude_deg: float, azimuth_deg: float) -> None:
        self.altitude_deg = altitude_deg
        self.azimuth_deg = azimuth_deg

    def fetch_conditions(self, site: Site, **kwargs):
        target = kwargs["target"]
        min_altitude = kwargs["min_target_altitude_deg"]
        current_target = {
            **target.to_dict(),
            "altitude_deg": self.altitude_deg,
            "azimuth_deg": self.azimuth_deg,
            "airmass": 1.2,
            "above_horizon": self.altitude_deg > 0.0,
            "above_observation_threshold": self.altitude_deg >= min_altitude,
            "moon_separation_deg": 80.0,
        }
        return AstronomicalConditionsSnapshot(
            site_id=site.id,
            site_name=site.name,
            latitude_deg=site.latitude_deg or 0.0,
            longitude_deg=site.longitude_deg or 0.0,
            elevation_m=site.elevation_m,
            generated_at_utc="2026-09-25T20:00:00Z",
            provider=self.provider_name,
            min_target_altitude_deg=min_altitude,
            target=target.to_dict(),
            current=AstronomicalConditionsCurrent(
                time_utc="2026-09-25T20:00:00Z",
                sky_state="astronomical_night",
                sun_altitude_deg=-20.0,
                sun_azimuth_deg=270.0,
                sunrise_utc=None,
                sunset_utc=None,
                civil_twilight_evening_start_utc=None,
                civil_twilight_evening_end_utc=None,
                civil_twilight_morning_start_utc=None,
                civil_twilight_morning_end_utc=None,
                nautical_twilight_evening_start_utc=None,
                nautical_twilight_evening_end_utc=None,
                nautical_twilight_morning_start_utc=None,
                nautical_twilight_morning_end_utc=None,
                astronomical_twilight_evening_start_utc=None,
                astronomical_twilight_evening_end_utc=None,
                astronomical_twilight_morning_start_utc=None,
                astronomical_twilight_morning_end_utc=None,
                astronomical_night_start_utc=None,
                astronomical_night_end_utc=None,
                moon_altitude_deg=12.0,
                moon_azimuth_deg=110.0,
                moon_illumination_fraction=0.25,
                moon_illumination_pct=25.0,
                moon_phase_angle_deg=90.0,
                moon_phase_label="first quarter",
                moonrise_utc=None,
                moonset_utc=None,
                target=current_target,
            ),
            hourly=[],
        )


class VisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "visibility.db"
        self.connection = initialize_database(self.db_path)
        self.repository = PlanningRepository(self.connection)
        self.repository.create_target(
            Target(
                id="target:m31",
                catalog="Messier",
                catalog_id="M31",
                name="Andromeda Galaxy",
                ra_deg=10.6847,
                dec_deg=41.2692,
            )
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_local_horizon_absent_does_not_block_visibility(self) -> None:
        result = analyze_local_horizon_visibility([], target_altitude_deg=12.0, target_azimuth_deg=90.0)

        self.assertFalse(result.profile_available)
        self.assertIsNone(result.above_local_horizon)

    def test_local_horizon_requires_positive_clearance(self) -> None:
        profile = [
            LocalHorizonPoint(azimuth_deg=0.0, min_altitude_deg=10.0),
            LocalHorizonPoint(azimuth_deg=180.0, min_altitude_deg=20.0),
        ]

        above = analyze_local_horizon_visibility(profile, target_altitude_deg=16.0, target_azimuth_deg=90.0)
        equal = analyze_local_horizon_visibility(profile, target_altitude_deg=15.0, target_azimuth_deg=90.0)
        below = analyze_local_horizon_visibility(profile, target_altitude_deg=14.0, target_azimuth_deg=90.0)

        self.assertTrue(above.above_local_horizon)
        self.assertFalse(equal.above_local_horizon)
        self.assertFalse(below.above_local_horizon)

    def test_target_visibility_distinguishes_geometric_minimum_and_local_horizon(self) -> None:
        self.repository.create_site(
            Site(
                id="site:horizon",
                name="Horizon Site",
                latitude_deg=52.0,
                longitude_deg=21.0,
                horizon_profile=[
                    LocalHorizonPoint(azimuth_deg=0.0, min_altitude_deg=10.0),
                    LocalHorizonPoint(azimuth_deg=180.0, min_altitude_deg=20.0),
                ],
            )
        )
        service = TargetVisibilityService(
            self.connection,
            astronomy_service=FakeAstronomyService(altitude_deg=16.0, azimuth_deg=90.0),
        )

        result = service.target_visibility_at(
            site_id="site:horizon",
            target_id="target:m31",
            time_utc="2026-09-25T20:00:00Z",
            min_target_altitude_deg=15.0,
        )

        self.assertTrue(result["above_geometric_horizon"])
        self.assertTrue(result["above_minimum_altitude"])
        self.assertTrue(result["above_local_horizon"])
        self.assertTrue(result["visible"])

    def test_target_visibility_local_horizon_blocks_final_visible_flag(self) -> None:
        self.repository.create_site(
            Site(
                id="site:blocking",
                name="Blocking Site",
                latitude_deg=52.0,
                longitude_deg=21.0,
                horizon_profile=[
                    LocalHorizonPoint(azimuth_deg=0.0, min_altitude_deg=10.0),
                    LocalHorizonPoint(azimuth_deg=180.0, min_altitude_deg=20.0),
                ],
            )
        )
        service = TargetVisibilityService(
            self.connection,
            astronomy_service=FakeAstronomyService(altitude_deg=15.0, azimuth_deg=90.0),
        )

        result = service.target_visibility_at(
            site_id="site:blocking",
            target_id="target:m31",
            time_utc="2026-09-25T20:00:00Z",
            min_target_altitude_deg=15.0,
        )

        self.assertTrue(result["above_geometric_horizon"])
        self.assertTrue(result["above_minimum_altitude"])
        self.assertFalse(result["above_local_horizon"])
        self.assertFalse(result["visible"])


if __name__ == "__main__":
    unittest.main()
