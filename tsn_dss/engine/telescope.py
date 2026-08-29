from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import sqrt

from ..domain.models import ImagingProfile, PlannedPointing, TelescopeState


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compute_fov_deg(sensor_size_mm: float, focal_length_mm: float) -> float:
    if focal_length_mm <= 0:
        raise ValueError("focal_length_mm must be positive.")
    return 57.29577951308232 * (sensor_size_mm / focal_length_mm)


@dataclass(slots=True)
class TelescopeSnapshot:
    telescope_state: TelescopeState
    imaging_profile: ImagingProfile
    planned_pointing: PlannedPointing | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "telescope_state": asdict(self.telescope_state),
            "imaging_profile": asdict(self.imaging_profile),
            "planned_pointing": asdict(self.planned_pointing) if self.planned_pointing else None,
        }


class SimulatorTelescopeAdapter:
    def __init__(
        self,
        *,
        adapter_id: str = "simulator",
        site_lat_deg: float = 52.2297,
        site_lon_deg: float = 21.0122,
        site_elevation_m: float = 100.0,
        ra_hours: float = 5.588,
        dec_deg: float = -5.391,
        alt_deg: float | None = None,
        az_deg: float | None = None,
        target_name: str = "M42",
        position_quality: str = "simulated",
    ) -> None:
        self._adapter_id = adapter_id
        self._site_lat_deg = site_lat_deg
        self._site_lon_deg = site_lon_deg
        self._site_elevation_m = site_elevation_m
        self._ra_hours = ra_hours
        self._dec_deg = dec_deg
        self._alt_deg = alt_deg
        self._az_deg = az_deg
        self._target_name = target_name
        self._position_quality = position_quality
        self._connected = True
        self._status = "tracking"
        self._slew_start_ra_hours: float | None = None
        self._slew_start_dec_deg: float | None = None
        self._slew_end_ra_hours: float | None = None
        self._slew_end_dec_deg: float | None = None
        self._slew_start_alt_deg: float | None = None
        self._slew_start_az_deg: float | None = None
        self._slew_end_alt_deg: float | None = None
        self._slew_end_az_deg: float | None = None
        self._slew_started_at: datetime | None = None
        self._slew_finish_at: datetime | None = None
        self._post_slew_status: str = "tracking"

    def get_state(self) -> TelescopeState:
        current_ra_hours, current_dec_deg, current_alt_deg, current_az_deg = self._get_current_pointing()
        return TelescopeState(
            adapter_id=self._adapter_id,
            source_kind="simulator",
            timestamp_utc=_utc_now_iso(),
            connected=self._connected,
            status=self._status,
            is_simulated=True,
            site_lat_deg=self._site_lat_deg,
            site_lon_deg=self._site_lon_deg,
            site_elevation_m=self._site_elevation_m,
            ra_hours=current_ra_hours,
            dec_deg=current_dec_deg,
            alt_deg=current_alt_deg,
            az_deg=current_az_deg,
            target_name=self._target_name,
            position_quality=self._position_quality,
        )

    def update_pointing(
        self,
        *,
        ra_hours: float | None = None,
        dec_deg: float | None = None,
        alt_deg: float | None = None,
        az_deg: float | None = None,
        target_name: str | None = None,
        status: str | None = None,
    ) -> None:
        if target_name is not None:
            self._target_name = target_name
        current_ra_hours, current_dec_deg, current_alt_deg, current_az_deg = self._get_current_pointing()

        next_ra_hours = ra_hours if ra_hours is not None else current_ra_hours
        next_dec_deg = dec_deg if dec_deg is not None else current_dec_deg
        next_alt_deg = alt_deg if alt_deg is not None else current_alt_deg
        next_az_deg = az_deg if az_deg is not None else current_az_deg

        coordinates_changed = any(
            (
                ra_hours is not None and not _float_eq(next_ra_hours, current_ra_hours),
                dec_deg is not None and not _float_eq(next_dec_deg, current_dec_deg),
                alt_deg is not None and not _float_eq(next_alt_deg, current_alt_deg),
                az_deg is not None and not _float_eq(next_az_deg, current_az_deg),
            )
        )

        if coordinates_changed:
            now = datetime.now(timezone.utc)
            duration_s = _estimate_slew_duration_s(
                current_ra_hours,
                current_dec_deg,
                next_ra_hours,
                next_dec_deg,
            )
            self._slew_start_ra_hours = current_ra_hours
            self._slew_start_dec_deg = current_dec_deg
            self._slew_end_ra_hours = next_ra_hours
            self._slew_end_dec_deg = next_dec_deg
            self._slew_start_alt_deg = current_alt_deg
            self._slew_start_az_deg = current_az_deg
            self._slew_end_alt_deg = next_alt_deg
            self._slew_end_az_deg = next_az_deg
            self._slew_started_at = now
            self._slew_finish_at = now + timedelta(seconds=duration_s)
            self._post_slew_status = status or "tracking"
            self._status = "slewing"
            return

        self._ra_hours = next_ra_hours
        self._dec_deg = next_dec_deg
        self._alt_deg = next_alt_deg
        self._az_deg = next_az_deg
        if status is not None:
            self._status = status

    def _get_current_pointing(self) -> tuple[float, float, float | None, float | None]:
        if (
            self._slew_started_at is None
            or self._slew_finish_at is None
            or self._slew_start_ra_hours is None
            or self._slew_start_dec_deg is None
            or self._slew_end_ra_hours is None
            or self._slew_end_dec_deg is None
        ):
            return self._ra_hours, self._dec_deg, self._alt_deg, self._az_deg

        now = datetime.now(timezone.utc)
        total_s = max((self._slew_finish_at - self._slew_started_at).total_seconds(), 0.001)
        elapsed_s = (now - self._slew_started_at).total_seconds()
        fraction = max(0.0, min(1.0, elapsed_s / total_s))

        if fraction >= 1.0:
            self._ra_hours = self._slew_end_ra_hours
            self._dec_deg = self._slew_end_dec_deg
            self._alt_deg = self._slew_end_alt_deg
            self._az_deg = self._slew_end_az_deg
            self._status = self._post_slew_status
            self._slew_start_ra_hours = None
            self._slew_start_dec_deg = None
            self._slew_end_ra_hours = None
            self._slew_end_dec_deg = None
            self._slew_start_alt_deg = None
            self._slew_start_az_deg = None
            self._slew_end_alt_deg = None
            self._slew_end_az_deg = None
            self._slew_started_at = None
            self._slew_finish_at = None
            return self._ra_hours, self._dec_deg, self._alt_deg, self._az_deg

        return (
            _lerp(self._slew_start_ra_hours, self._slew_end_ra_hours, fraction),
            _lerp(self._slew_start_dec_deg, self._slew_end_dec_deg, fraction),
            _lerp_optional(self._slew_start_alt_deg, self._slew_end_alt_deg, fraction),
            _lerp_optional(self._slew_start_az_deg, self._slew_end_az_deg, fraction),
        )


class TelescopeStateService:
    def __init__(
        self,
        *,
        adapter: SimulatorTelescopeAdapter | None = None,
        imaging_profile: ImagingProfile | None = None,
    ) -> None:
        self._adapter = adapter or SimulatorTelescopeAdapter()
        self._imaging_profile = imaging_profile or self._default_imaging_profile()
        self._planned_pointing: PlannedPointing | None = None

    def get_snapshot(self) -> TelescopeSnapshot:
        return TelescopeSnapshot(
            telescope_state=self._adapter.get_state(),
            imaging_profile=self._imaging_profile,
            planned_pointing=self._planned_pointing,
        )

    def update_simulator_pointing(
        self,
        *,
        ra_hours: float | None = None,
        dec_deg: float | None = None,
        alt_deg: float | None = None,
        az_deg: float | None = None,
        target_name: str | None = None,
        status: str | None = None,
    ) -> TelescopeSnapshot:
        self._adapter.update_pointing(
            ra_hours=ra_hours,
            dec_deg=dec_deg,
            alt_deg=alt_deg,
            az_deg=az_deg,
            target_name=target_name,
            status=status,
        )
        return self.get_snapshot()

    def set_planned_pointing(
        self,
        *,
        ra_hours: float,
        dec_deg: float,
        target_name: str | None = None,
        source_kind: str = "manual",
        source_id: str | None = None,
    ) -> TelescopeSnapshot:
        self._planned_pointing = PlannedPointing(
            target_name=target_name,
            ra_hours=ra_hours,
            dec_deg=dec_deg,
            source_kind=source_kind,
            source_id=source_id,
            updated_at_utc=_utc_now_iso(),
        )
        return self.get_snapshot()

    def clear_planned_pointing(self) -> TelescopeSnapshot:
        self._planned_pointing = None
        return self.get_snapshot()

    def slew_to_planned_pointing(self) -> TelescopeSnapshot:
        if self._planned_pointing is None:
            raise ValueError("No planned pointing is set.")

        self._adapter.update_pointing(
            ra_hours=self._planned_pointing.ra_hours,
            dec_deg=self._planned_pointing.dec_deg,
            target_name=self._planned_pointing.target_name,
            status="tracking",
        )
        return self.get_snapshot()

    @staticmethod
    def _default_imaging_profile() -> ImagingProfile:
        focal_length_mm = 160.0
        sensor_width_mm = 11.2
        sensor_height_mm = 6.3
        return ImagingProfile(
            profile_id="simulator-default",
            label="Seestar S30 Pro tele profile",
            focal_length_mm=focal_length_mm,
            sensor_width_mm=sensor_width_mm,
            sensor_height_mm=sensor_height_mm,
            rotation_deg=0.0,
            fov_width_deg=_compute_fov_deg(sensor_width_mm, focal_length_mm),
            fov_height_deg=_compute_fov_deg(sensor_height_mm, focal_length_mm),
        )


def _lerp(start: float, end: float, fraction: float) -> float:
    return start + (end - start) * fraction


def _lerp_optional(start: float | None, end: float | None, fraction: float) -> float | None:
    if start is None or end is None:
        return end if fraction >= 1.0 else start
    return _lerp(start, end, fraction)


def _float_eq(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) < 1e-9


def _estimate_slew_duration_s(
    start_ra_hours: float,
    start_dec_deg: float,
    end_ra_hours: float,
    end_dec_deg: float,
) -> float:
    start_ra_deg = start_ra_hours * 15.0
    end_ra_deg = end_ra_hours * 15.0
    delta_ra = min(abs(end_ra_deg - start_ra_deg), 360.0 - abs(end_ra_deg - start_ra_deg))
    delta_dec = abs(end_dec_deg - start_dec_deg)
    distance = sqrt(delta_ra * delta_ra + delta_dec * delta_dec)
    return max(4.0, min(12.0, 4.0 + distance / 10.0))
