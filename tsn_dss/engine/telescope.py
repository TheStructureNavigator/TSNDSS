from __future__ import annotations
"""Hardware-agnostic telescope state, adapters, and sky-facing control service."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import sqrt
from typing import Callable, Protocol, runtime_checkable

from ..domain.models import ImagingProfile, PlannedPointing, Site, TelescopeState


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compute_fov_deg(sensor_size_mm: float, focal_length_mm: float) -> float:
    if focal_length_mm <= 0:
        raise ValueError("focal_length_mm must be positive.")
    return 57.29577951308232 * (sensor_size_mm / focal_length_mm)


@dataclass(slots=True)
class TelescopeSnapshot:
    """Combined telescope telemetry, imaging geometry, and optional planned pointing."""
    telescope_state: TelescopeState
    imaging_profile: ImagingProfile
    planned_pointing: PlannedPointing | None = None
    active_site: Site | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "telescope_state": asdict(self.telescope_state),
            "imaging_profile": asdict(self.imaging_profile),
            "planned_pointing": asdict(self.planned_pointing) if self.planned_pointing else None,
            "active_site": asdict(self.active_site) if self.active_site else None,
        }


@dataclass(slots=True)
class TelescopeAdapterCapabilities:
    """Capability flags used by the API and GUI to shape telescope controls."""
    can_connect: bool = True
    can_disconnect: bool = True
    can_manual_pointing: bool = False
    can_slew_to_coordinates: bool = True
    can_park: bool = False
    can_set_tracking: bool = False
    can_stream_preview: bool = False
    can_start_stack: bool = False
    can_run_observation_plans: bool = False


@dataclass(slots=True)
class TelescopeAdapterDescriptor:
    """Static metadata about one registered telescope adapter implementation."""
    adapter_id: str
    label: str
    source_kind: str
    is_simulated: bool
    capabilities: TelescopeAdapterCapabilities


@dataclass(slots=True)
class SeestarAdapterConfig:
    """Configuration placeholder for the future real Seestar-backed adapter."""
    adapter_id: str = "seestar"
    host: str | None = None
    auth_key_path: str | None = None
    device_label: str = "ZWO Seestar"
    model_label: str = "Seestar S30 Pro"
    use_event_listener: bool = True
    use_live_stream: bool = False


@runtime_checkable
class TelescopeAdapter(Protocol):
    """Neutral adapter contract implemented by simulator and hardware backends."""
    def get_state(self) -> TelescopeState:
        ...

    def get_imaging_profile(self) -> ImagingProfile:
        ...

    def get_capabilities(self) -> TelescopeAdapterCapabilities:
        ...

    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...

    def slew_to_coordinates(
        self,
        *,
        ra_hours: float,
        dec_deg: float,
        target_name: str | None = None,
    ) -> None:
        ...


class TelescopeAdapterRegistry:
    """Factory registry for named telescope adapters exposed to TSN DSS."""

    def __init__(self) -> None:
        self._descriptors: dict[str, TelescopeAdapterDescriptor] = {}
        self._factories: dict[str, Callable[[], TelescopeAdapter]] = {}

    def register(
        self,
        descriptor: TelescopeAdapterDescriptor,
        factory: Callable[[], TelescopeAdapter],
    ) -> None:
        self._descriptors[descriptor.adapter_id] = descriptor
        self._factories[descriptor.adapter_id] = factory

    def create(self, adapter_id: str) -> TelescopeAdapter:
        factory = self._factories.get(adapter_id)
        if factory is None:
            raise KeyError(f"Unknown telescope adapter: {adapter_id}")
        return factory()

    def list_descriptors(self) -> list[TelescopeAdapterDescriptor]:
        return [self._descriptors[adapter_id] for adapter_id in sorted(self._descriptors)]

    def get_descriptor(self, adapter_id: str) -> TelescopeAdapterDescriptor | None:
        return self._descriptors.get(adapter_id)


@runtime_checkable
class ManualPointingAdapter(TelescopeAdapter, Protocol):
    """Optional adapter extension for sources that allow direct manual pointing updates."""
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
        ...


class SimulatorTelescopeAdapter:
    """Built-in telescope adapter used for UI work and hardware-free testing."""

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
        self._imaging_profile = _build_seestar_s30_pro_tele_profile(profile_id="simulator-default")

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

    def get_imaging_profile(self) -> ImagingProfile:
        return self._imaging_profile

    def get_capabilities(self) -> TelescopeAdapterCapabilities:
        return TelescopeAdapterCapabilities(
            can_connect=True,
            can_disconnect=True,
            can_manual_pointing=True,
            can_slew_to_coordinates=True,
            can_park=False,
            can_set_tracking=False,
            can_stream_preview=False,
            can_start_stack=False,
            can_run_observation_plans=False,
        )

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

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
        """Update simulator coordinates immediately or animate a synthetic slew."""
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

    def slew_to_coordinates(
        self,
        *,
        ra_hours: float,
        dec_deg: float,
        target_name: str | None = None,
    ) -> None:
        self.update_pointing(
            ra_hours=ra_hours,
            dec_deg=dec_deg,
            target_name=target_name,
            status="tracking",
        )


class SeestarAdapter:
    """Registered hardware adapter skeleton reserved for future seestarpy integration."""

    def __init__(self, config: SeestarAdapterConfig | None = None) -> None:
        self._config = config or SeestarAdapterConfig()
        self._connected = False
        self._last_known_ra_hours: float | None = None
        self._last_known_dec_deg: float | None = None
        self._last_known_alt_deg: float | None = None
        self._last_known_az_deg: float | None = None
        self._last_target_name: str | None = None
        self._status = "disconnected"

    def get_state(self) -> TelescopeState:
        return TelescopeState(
            adapter_id=self._config.adapter_id,
            source_kind="seestar",
            timestamp_utc=_utc_now_iso(),
            connected=self._connected,
            status=self._status,
            is_simulated=False,
            ra_hours=self._last_known_ra_hours,
            dec_deg=self._last_known_dec_deg,
            alt_deg=self._last_known_alt_deg,
            az_deg=self._last_known_az_deg,
            target_name=self._last_target_name,
            position_quality="telemetry" if self._connected else "unavailable",
        )

    def get_imaging_profile(self) -> ImagingProfile:
        return _build_seestar_s30_pro_tele_profile(profile_id="seestar_s30_pro_tele")

    def get_capabilities(self) -> TelescopeAdapterCapabilities:
        return TelescopeAdapterCapabilities(
            can_connect=True,
            can_disconnect=True,
            can_manual_pointing=False,
            can_slew_to_coordinates=True,
            can_park=True,
            can_set_tracking=True,
            can_stream_preview=True,
            can_start_stack=True,
            can_run_observation_plans=True,
        )

    def connect(self) -> None:
        self._connected = True
        self._status = "idle"

    def disconnect(self) -> None:
        self._connected = False
        self._status = "disconnected"

    def slew_to_coordinates(
        self,
        *,
        ra_hours: float,
        dec_deg: float,
        target_name: str | None = None,
    ) -> None:
        raise NotImplementedError(
            "SeestarAdapter skeleton is registered, but real seestarpy integration is not implemented yet."
        )


class TelescopeStateService:
    """Own the active adapter and expose normalized telescope-facing operations."""

    def __init__(
        self,
        *,
        adapter: TelescopeAdapter | None = None,
        registry: TelescopeAdapterRegistry | None = None,
        active_adapter_id: str = "simulator",
    ) -> None:
        self._registry = registry or build_default_telescope_adapter_registry()
        self._active_adapter_id = active_adapter_id
        self._adapter = adapter or self._registry.create(active_adapter_id)
        self._planned_pointing: PlannedPointing | None = None
        self._active_site: Site | None = None

    def get_snapshot(self) -> TelescopeSnapshot:
        telescope_state = self._adapter.get_state()
        if self._active_site is not None:
            telescope_state.site_lat_deg = self._active_site.latitude_deg
            telescope_state.site_lon_deg = self._active_site.longitude_deg
            telescope_state.site_elevation_m = self._active_site.elevation_m
        return TelescopeSnapshot(
            telescope_state=telescope_state,
            imaging_profile=self._adapter.get_imaging_profile(),
            planned_pointing=self._planned_pointing,
            active_site=self._active_site,
        )

    def get_adapter_capabilities(self) -> TelescopeAdapterCapabilities:
        return self._adapter.get_capabilities()

    def get_active_adapter_id(self) -> str:
        return self._active_adapter_id

    def get_active_site(self) -> Site | None:
        return self._active_site

    def get_active_site_id(self) -> str | None:
        return self._active_site.id if self._active_site is not None else None

    def list_available_adapters(self) -> list[TelescopeAdapterDescriptor]:
        return self._registry.list_descriptors()

    def set_active_adapter(self, adapter_id: str) -> TelescopeSnapshot:
        """Swap the active adapter without changing the frontend contract."""
        self._adapter = self._registry.create(adapter_id)
        self._active_adapter_id = adapter_id
        return self.get_snapshot()

    def set_active_site(self, site: Site | None) -> TelescopeSnapshot:
        """Attach an observing site to the normalized telescope snapshot."""
        self._active_site = site
        return self.get_snapshot()

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
        """Forward manual pointing changes only to adapters that explicitly support them."""
        if not isinstance(self._adapter, ManualPointingAdapter):
            raise ValueError("The active telescope adapter does not support manual pointing updates.")

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
        """Store a planned sky position independently from live telescope telemetry."""
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
        """Ask the active adapter to slew to the current planned pointing."""
        if self._planned_pointing is None:
            raise ValueError("No planned pointing is set.")

        self._adapter.slew_to_coordinates(
            ra_hours=self._planned_pointing.ra_hours,
            dec_deg=self._planned_pointing.dec_deg,
            target_name=self._planned_pointing.target_name,
        )
        return self.get_snapshot()

    @staticmethod
    def _default_imaging_profile() -> ImagingProfile:
        return _build_seestar_s30_pro_tele_profile(profile_id="simulator-default")


def _build_seestar_s30_pro_tele_profile(*, profile_id: str) -> ImagingProfile:
    """Return the default imaging profile currently used for simulator and Seestar work."""
    focal_length_mm = 160.0
    sensor_width_mm = 11.2
    sensor_height_mm = 6.3
    return ImagingProfile(
        profile_id=profile_id,
        label="Seestar S30 Pro tele profile",
        focal_length_mm=focal_length_mm,
        sensor_width_mm=sensor_width_mm,
        sensor_height_mm=sensor_height_mm,
        rotation_deg=0.0,
        fov_width_deg=_compute_fov_deg(sensor_width_mm, focal_length_mm),
        fov_height_deg=_compute_fov_deg(sensor_height_mm, focal_length_mm),
    )


def build_default_telescope_adapter_registry() -> TelescopeAdapterRegistry:
    """Register the built-in simulator and current Seestar skeleton adapters."""
    registry = TelescopeAdapterRegistry()
    registry.register(
        TelescopeAdapterDescriptor(
            adapter_id="seestar",
            label="Seestar",
            source_kind="seestar",
            is_simulated=False,
            capabilities=TelescopeAdapterCapabilities(
                can_connect=True,
                can_disconnect=True,
                can_manual_pointing=False,
                can_slew_to_coordinates=True,
                can_park=True,
                can_set_tracking=True,
                can_stream_preview=True,
                can_start_stack=True,
                can_run_observation_plans=True,
            ),
        ),
        lambda: SeestarAdapter(),
    )
    registry.register(
        TelescopeAdapterDescriptor(
            adapter_id="simulator",
            label="Simulator",
            source_kind="simulator",
            is_simulated=True,
            capabilities=TelescopeAdapterCapabilities(
                can_connect=True,
                can_disconnect=True,
                can_manual_pointing=True,
                can_slew_to_coordinates=True,
            ),
        ),
        lambda: SimulatorTelescopeAdapter(),
    )
    return registry


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
