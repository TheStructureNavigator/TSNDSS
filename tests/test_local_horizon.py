import unittest

from tsn_dss.domain.local_horizon import get_local_horizon_altitude, normalize_local_horizon_profile
from tsn_dss.domain.models import LocalHorizonPoint


class LocalHorizonProfileTests(unittest.TestCase):
    def test_normalize_sorts_points_by_azimuth(self) -> None:
        profile = normalize_local_horizon_profile(
            [
                LocalHorizonPoint(azimuth_deg=180, min_altitude_deg=40),
                LocalHorizonPoint(azimuth_deg=0, min_altitude_deg=12),
                LocalHorizonPoint(azimuth_deg=90, min_altitude_deg=25),
            ]
        )

        self.assertEqual([point.azimuth_deg for point in profile], [0, 90, 180])

    def test_interpolates_between_regular_points(self) -> None:
        altitude = get_local_horizon_altitude(
            [
                LocalHorizonPoint(azimuth_deg=0, min_altitude_deg=10),
                LocalHorizonPoint(azimuth_deg=90, min_altitude_deg=30),
            ],
            45,
        )

        self.assertEqual(altitude, 20)

    def test_interpolates_across_zero_degrees(self) -> None:
        altitude = get_local_horizon_altitude(
            [
                LocalHorizonPoint(azimuth_deg=0, min_altitude_deg=10),
                LocalHorizonPoint(azimuth_deg=315, min_altitude_deg=20),
            ],
            337.5,
        )

        self.assertEqual(altitude, 15)

    def test_empty_profile_returns_none(self) -> None:
        self.assertIsNone(get_local_horizon_altitude([], 180))


if __name__ == "__main__":
    unittest.main()
