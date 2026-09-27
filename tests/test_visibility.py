from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import LocalHorizonPoint, Site, Target
from tsn_dss.engine.astronomy import AstronomicalConditionsCurrent, AstronomicalConditionsSnapshot, AstronomicalTargetContext
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.visibility import TargetVisibilityService, analyze_local_horizon_visibility, visibility_windows


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


class SequenceVisibilityEvaluator:
    def __init__(self, start: datetime, values: list[tuple[float, float]]) -> None:
        self.start = start
        self.values = values

    def altitude_azimuth_at(self, moment_utc: datetime) -> tuple[float | None, float | None]:
        elapsed_minutes = int(round((moment_utc - self.start).total_seconds() / 60.0))
        index = max(0, min(len(self.values) - 1, elapsed_minutes))
        return self.values[index]


class LinearVisibilityEvaluator:
    def __init__(self, start: datetime, *, altitude_at_start: float, altitude_slope_deg_per_min: float, azimuth_deg: float = 90.0) -> None:
        self.start = start
        self.altitude_at_start = altitude_at_start
        self.altitude_slope_deg_per_min = altitude_slope_deg_per_min
        self.azimuth_deg = azimuth_deg

    def altitude_azimuth_at(self, moment_utc: datetime) -> tuple[float | None, float | None]:
        minutes = (moment_utc - self.start).total_seconds() / 60.0
        return self.altitude_at_start + minutes * self.altitude_slope_deg_per_min, self.azimuth_deg


class ParabolicVisibilityEvaluator:
    def __init__(self, start: datetime, *, peak_offset_minutes: float) -> None:
        self.start = start
        self.peak_offset_minutes = peak_offset_minutes

    def altitude_azimuth_at(self, moment_utc: datetime) -> tuple[float | None, float | None]:
        minutes = (moment_utc - self.start).total_seconds() / 60.0
        altitude = 60.0 - ((minutes - self.peak_offset_minutes) ** 2) / 100.0
        return altitude, 180.0


class WindowPatternEvaluator:
    def __init__(
        self,
        start: datetime,
        *,
        visible_ranges: list[tuple[timedelta, timedelta]],
        visible_altitude_deg: float = 30.0,
        hidden_altitude_deg: float = -5.0,
    ) -> None:
        self.start = start
        self.visible_ranges = visible_ranges
        self.visible_altitude_deg = visible_altitude_deg
        self.hidden_altitude_deg = hidden_altitude_deg
        self.calls: list[datetime] = []

    def altitude_azimuth_at(self, moment_utc: datetime) -> tuple[float | None, float | None]:
        self.calls.append(moment_utc)
        offset = moment_utc - self.start
        altitude = self.hidden_altitude_deg
        for range_start, range_end in self.visible_ranges:
            if range_start <= offset <= range_end:
                altitude = self.visible_altitude_deg
                break
        return altitude, 90.0


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
        self.site = Site(id="site:test", name="Test Site", latitude_deg=52.0, longitude_deg=21.0)
        self.target = AstronomicalTargetContext(
            target_name="M31",
            ra_deg=10.6847,
            dec_deg=41.2692,
            source_kind="manual",
        )
        self.start = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)

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

    def test_visibility_windows_returns_ordinary_window(self) -> None:
        evaluator = SequenceVisibilityEvaluator(
            self.start,
            [(5.0, 90.0)] * 10 + [(25.0, 90.0)] * 30 + [(5.0, 90.0)] * 30,
        )

        result = visibility_windows(
            self.site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=60),
            min_target_altitude_deg=20.0,
            evaluator=evaluator,
        )

        self.assertEqual(len(result.windows), 1)
        self.assertFalse(result.windows[0].starts_at_interval_start)
        self.assertFalse(result.windows[0].ends_at_interval_end)
        self.assertTrue(result.diagnostics.any_visible)

    def test_visibility_windows_detects_short_window_inside_old_coarse_span(self) -> None:
        evaluator = WindowPatternEvaluator(
            self.start,
            visible_ranges=[(timedelta(minutes=1), timedelta(minutes=3))],
        )

        result = visibility_windows(
            self.site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=5),
            min_target_altitude_deg=20.0,
            evaluator=evaluator,
        )

        self.assertEqual(len(result.windows), 1)
        self.assertEqual(result.windows[0].start_utc, "2026-09-25T20:01:00Z")
        self.assertEqual(result.windows[0].end_utc, "2026-09-25T20:03:00Z")
        self.assertFalse(result.windows[0].starts_at_interval_start)
        self.assertFalse(result.windows[0].ends_at_interval_end)
        self.assertTrue(result.diagnostics.any_visible)
        self.assert_no_state_detection_gap_exceeds_thirty_seconds(evaluator.calls)

    def test_visibility_windows_preserves_short_internal_blocked_gap(self) -> None:
        evaluator = WindowPatternEvaluator(
            self.start,
            visible_ranges=[
                (timedelta(minutes=0), timedelta(minutes=1)),
                (timedelta(minutes=3), timedelta(minutes=5)),
            ],
        )

        result = visibility_windows(
            self.site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=5),
            min_target_altitude_deg=20.0,
            evaluator=evaluator,
        )

        self.assertEqual(len(result.windows), 2)
        self.assertEqual(result.windows[0].start_utc, "2026-09-25T20:00:00Z")
        self.assertEqual(result.windows[0].end_utc, "2026-09-25T20:01:00Z")
        self.assertEqual(result.windows[1].start_utc, "2026-09-25T20:03:00Z")
        self.assertEqual(result.windows[1].end_utc, "2026-09-25T20:05:00Z")
        self.assertTrue(result.windows[0].starts_at_interval_start)
        self.assertTrue(result.windows[1].ends_at_interval_end)
        self.assert_no_state_detection_gap_exceeds_thirty_seconds(evaluator.calls)

    def test_visibility_windows_local_horizon_blocks_otherwise_valid_interval(self) -> None:
        site = Site(
            id="site:block",
            name="Blocking Site",
            latitude_deg=52.0,
            longitude_deg=21.0,
            horizon_profile=[LocalHorizonPoint(azimuth_deg=0.0, min_altitude_deg=40.0)],
        )
        evaluator = SequenceVisibilityEvaluator(self.start, [(30.0, 90.0)] * 61)

        result = visibility_windows(
            site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=60),
            min_target_altitude_deg=20.0,
            evaluator=evaluator,
        )

        self.assertEqual(result.windows, ())
        self.assertTrue(result.diagnostics.any_altitude_constraints_satisfied)
        self.assertFalse(result.diagnostics.any_clear_of_local_horizon)
        self.assertFalse(result.diagnostics.any_visible)

    def test_visibility_windows_minimum_altitude_blocks_geometrically_visible_target(self) -> None:
        evaluator = SequenceVisibilityEvaluator(self.start, [(5.0, 90.0)] * 61)

        result = visibility_windows(
            self.site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=60),
            min_target_altitude_deg=10.0,
            evaluator=evaluator,
        )

        self.assertEqual(result.windows, ())
        self.assertTrue(result.diagnostics.any_above_geometric_horizon)
        self.assertFalse(result.diagnostics.any_above_minimum_altitude)

    def test_visibility_windows_marks_interval_start_and_end_clipping(self) -> None:
        evaluator = SequenceVisibilityEvaluator(self.start, [(30.0, 90.0)] * 61)

        result = visibility_windows(
            self.site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=60),
            min_target_altitude_deg=20.0,
            evaluator=evaluator,
        )

        self.assertEqual(len(result.windows), 1)
        self.assertEqual(result.windows[0].start_utc, "2026-09-25T20:00:00Z")
        self.assertEqual(result.windows[0].end_utc, "2026-09-25T21:00:00Z")
        self.assertTrue(result.windows[0].starts_at_interval_start)
        self.assertTrue(result.windows[0].ends_at_interval_end)

    def test_visibility_windows_zero_window_diagnostics_for_never_above_horizon(self) -> None:
        evaluator = SequenceVisibilityEvaluator(self.start, [(-5.0, 90.0)] * 61)

        result = visibility_windows(
            self.site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=60),
            min_target_altitude_deg=0.0,
            evaluator=evaluator,
        )

        self.assertEqual(result.windows, ())
        self.assertFalse(result.diagnostics.any_above_geometric_horizon)
        self.assertFalse(result.diagnostics.any_visible)

    def test_visibility_windows_preserves_multiple_local_horizon_windows(self) -> None:
        site = Site(
            id="site:multiple",
            name="Multiple Windows Site",
            latitude_deg=52.0,
            longitude_deg=21.0,
            horizon_profile=[
                LocalHorizonPoint(azimuth_deg=0.0, min_altitude_deg=0.0),
                LocalHorizonPoint(azimuth_deg=80.0, min_altitude_deg=0.0),
                LocalHorizonPoint(azimuth_deg=90.0, min_altitude_deg=40.0),
                LocalHorizonPoint(azimuth_deg=100.0, min_altitude_deg=0.0),
                LocalHorizonPoint(azimuth_deg=260.0, min_altitude_deg=0.0),
                LocalHorizonPoint(azimuth_deg=270.0, min_altitude_deg=40.0),
                LocalHorizonPoint(azimuth_deg=280.0, min_altitude_deg=0.0),
            ],
        )

        class AzimuthSweepEvaluator:
            def altitude_azimuth_at(_, moment_utc: datetime) -> tuple[float | None, float | None]:
                minutes = (moment_utc - self.start).total_seconds() / 60.0
                return 30.0, minutes * 3.0

        result = visibility_windows(
            site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=120),
            min_target_altitude_deg=20.0,
            evaluator=AzimuthSweepEvaluator(),
        )

        self.assertGreaterEqual(len(result.windows), 3)

    def test_visibility_windows_refines_transition_to_thirty_seconds(self) -> None:
        evaluator = LinearVisibilityEvaluator(
            self.start,
            altitude_at_start=-8.5,
            altitude_slope_deg_per_min=1.0,
        )

        result = visibility_windows(
            self.site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=20),
            min_target_altitude_deg=0.0,
            evaluator=evaluator,
        )

        expected = self.start + timedelta(minutes=8.5)
        actual = datetime.fromisoformat(result.windows[0].start_utc.replace("Z", "+00:00"))
        self.assertLessEqual(abs((actual - expected).total_seconds()), 30)

    def test_visibility_windows_reports_max_altitude_time_to_sixty_seconds(self) -> None:
        evaluator = ParabolicVisibilityEvaluator(self.start, peak_offset_minutes=37.0)

        result = visibility_windows(
            self.site,
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=80),
            min_target_altitude_deg=20.0,
            evaluator=evaluator,
        )

        expected = self.start + timedelta(minutes=37)
        actual = datetime.fromisoformat(result.windows[0].max_altitude_time_utc.replace("Z", "+00:00"))
        self.assertLessEqual(abs((actual - expected).total_seconds()), 60)

    def test_visibility_windows_normalizes_utc_offset_and_naive_times(self) -> None:
        evaluator = SequenceVisibilityEvaluator(self.start, [(30.0, 90.0)] * 61)

        offset_result = visibility_windows(
            self.site,
            self.target,
            start_time_utc="2026-09-25T22:00:00+02:00",
            end_time_utc="2026-09-25T23:00:00+02:00",
            min_target_altitude_deg=20.0,
            evaluator=evaluator,
        )
        naive_result = visibility_windows(
            self.site,
            self.target,
            start_time_utc="2026-09-25T20:00:00",
            end_time_utc="2026-09-25T21:00:00",
            min_target_altitude_deg=20.0,
            evaluator=evaluator,
        )

        self.assertEqual(offset_result.interval_start_utc, "2026-09-25T20:00:00Z")
        self.assertEqual(naive_result.interval_start_utc, "2026-09-25T20:00:00Z")

    def test_visibility_windows_rejects_invalid_inputs(self) -> None:
        evaluator = SequenceVisibilityEvaluator(self.start, [(30.0, 90.0)] * 2)

        with self.assertRaisesRegex(ValueError, "end_time_utc"):
            visibility_windows(
                self.site,
                self.target,
                start_time_utc=self.start,
                end_time_utc=self.start,
                evaluator=evaluator,
            )
        with self.assertRaisesRegex(ValueError, "ra_deg"):
            visibility_windows(
                self.site,
                AstronomicalTargetContext(target_name="bad", ra_deg=360.0, dec_deg=0.0, source_kind="manual"),
                start_time_utc=self.start,
                end_time_utc=self.start + timedelta(minutes=1),
                evaluator=evaluator,
            )
        with self.assertRaisesRegex(ValueError, "latitude"):
            visibility_windows(
                Site(id="site:missing", name="Missing", latitude_deg=None, longitude_deg=21.0),
                self.target,
                start_time_utc=self.start,
                end_time_utc=self.start + timedelta(minutes=1),
                evaluator=evaluator,
            )
        with self.assertRaisesRegex(ValueError, "min_target_altitude"):
            visibility_windows(
                self.site,
                self.target,
                start_time_utc=self.start,
                end_time_utc=self.start + timedelta(minutes=1),
                min_target_altitude_deg=-1.0,
                evaluator=evaluator,
            )

    def test_visibility_windows_matches_target_visibility_at_for_representative_instant(self) -> None:
        self.repository.create_site(
            Site(
                id="site:compat",
                name="Compatibility Site",
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
        point = service.target_visibility_at(
            site_id="site:compat",
            target_id="target:m31",
            time_utc="2026-09-25T20:00:00Z",
            min_target_altitude_deg=15.0,
        )
        interval = visibility_windows(
            Site(
                id="site:compat",
                name="Compatibility Site",
                latitude_deg=52.0,
                longitude_deg=21.0,
                horizon_profile=[
                    LocalHorizonPoint(azimuth_deg=0.0, min_altitude_deg=10.0),
                    LocalHorizonPoint(azimuth_deg=180.0, min_altitude_deg=20.0),
                ],
            ),
            self.target,
            start_time_utc=self.start,
            end_time_utc=self.start + timedelta(minutes=1),
            min_target_altitude_deg=15.0,
            evaluator=SequenceVisibilityEvaluator(self.start, [(16.0, 90.0)] * 2),
        )

        self.assertTrue(point["visible"])
        self.assertEqual(interval.diagnostics.any_visible, point["visible"])

    def test_visibility_windows_real_astropy_regression(self) -> None:
        warsaw = Site(
            id="site:warsaw",
            name="Warsaw",
            latitude_deg=52.2297,
            longitude_deg=21.0122,
            elevation_m=100.0,
        )
        m31 = AstronomicalTargetContext(
            target_name="M31",
            ra_deg=10.6847083,
            dec_deg=41.26875,
            source_kind="manual",
        )

        result = visibility_windows(
            warsaw,
            m31,
            start_time_utc="2026-10-15T18:00:00Z",
            end_time_utc="2026-10-15T23:00:00Z",
            min_target_altitude_deg=20.0,
        )

        self.assertEqual(len(result.windows), 1)
        self.assertTrue(result.windows[0].starts_at_interval_start)
        self.assertTrue(result.windows[0].ends_at_interval_end)
        self.assertGreater(result.windows[0].max_altitude_deg, 70.0)

    def assert_no_state_detection_gap_exceeds_thirty_seconds(self, calls: list[datetime]) -> None:
        unique_calls = sorted(set(calls))
        grid_calls = [
            call
            for call in unique_calls
            if call >= self.start and call <= self.start + timedelta(minutes=5)
        ]
        gaps = [
            (right - left).total_seconds()
            for left, right in zip(grid_calls, grid_calls[1:])
        ]
        self.assertTrue(gaps)
        self.assertLessEqual(max(gaps), 30.0)


if __name__ == "__main__":
    unittest.main()
