from __future__ import annotations

from collections.abc import Iterable

from .models import LocalHorizonPoint


def normalize_local_horizon_profile(points: Iterable[LocalHorizonPoint]) -> list[LocalHorizonPoint]:
    """Validate and sort a cyclic local horizon profile."""
    normalized: list[LocalHorizonPoint] = []
    seen_azimuths: set[float] = set()

    for point in points:
        azimuth = float(point.azimuth_deg)
        altitude = float(point.min_altitude_deg)

        if not 0 <= azimuth < 360:
            raise ValueError("Local horizon azimuth_deg must be in [0, 360).")
        if not 0 <= altitude <= 90:
            raise ValueError("Local horizon min_altitude_deg must be in [0, 90].")
        if azimuth in seen_azimuths:
            raise ValueError(f"Duplicate local horizon azimuth_deg: {azimuth:g}")

        seen_azimuths.add(azimuth)
        normalized.append(LocalHorizonPoint(azimuth_deg=azimuth, min_altitude_deg=altitude))

    return sorted(normalized, key=lambda item: item.azimuth_deg)


def get_local_horizon_altitude(
    profile: Iterable[LocalHorizonPoint],
    azimuth_deg: float,
) -> float | None:
    """Return interpolated local horizon altitude for any azimuth.

    The profile is cyclic: interpolation between the last and first sample crosses
    through 360°/0°.
    """
    points = normalize_local_horizon_profile(profile)
    if not points:
        return None
    if len(points) == 1:
        return points[0].min_altitude_deg

    azimuth = _normalize_azimuth(azimuth_deg)

    for index, left in enumerate(points):
        right = points[(index + 1) % len(points)]
        left_azimuth = left.azimuth_deg
        right_azimuth = right.azimuth_deg
        candidate_azimuth = azimuth

        if index == len(points) - 1:
            right_azimuth += 360
            if candidate_azimuth < left_azimuth:
                candidate_azimuth += 360

        if left_azimuth <= candidate_azimuth <= right_azimuth:
            span = right_azimuth - left_azimuth
            if span == 0:
                return left.min_altitude_deg
            fraction = (candidate_azimuth - left_azimuth) / span
            return left.min_altitude_deg + fraction * (right.min_altitude_deg - left.min_altitude_deg)

    return points[0].min_altitude_deg


def _normalize_azimuth(value: float) -> float:
    normalized = float(value) % 360
    return normalized + 360 if normalized < 0 else normalized
