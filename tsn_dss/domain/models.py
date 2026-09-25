from __future__ import annotations
"""Core TSN DSS domain models shared across storage, processing, and GUI layers."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Target:
    """A durable sky target identified by catalog coordinates and optional metadata."""
    id: str
    catalog: str
    catalog_id: str
    name: str
    ra_deg: float
    dec_deg: float
    object_type: str | None = None
    angular_major_arcmin: float | None = None
    angular_minor_arcmin: float | None = None
    distance_ly: float | None = None
    constellation: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class LocalHorizonPoint:
    """One manually defined local-horizon obstruction sample for a Site."""
    azimuth_deg: float
    min_altitude_deg: float


@dataclass(slots=True)
class Site:
    """An observing location with optional environmental metadata."""
    id: str
    name: str
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    elevation_m: float | None = None
    sqm_mag_arcsec2: float | None = None
    bortle_class: int | None = None
    lp_artificial_brightness_mcd_m2: float | None = None
    lp_natural_sky_ratio: float | None = None
    lp_estimated_total_brightness_mcd_m2: float | None = None
    lp_estimated_sqm_mag_arcsec2: float | None = None
    lp_estimated_bortle_class: int | None = None
    lp_dataset_name: str | None = None
    lp_provider_name: str | None = None
    lp_source: str | None = None
    lp_source_unit: str | None = None
    lp_data_kind: str | None = None
    lp_updated_at: str | None = None
    south_horizon_open: bool = False
    notes: str | None = None
    horizon_profile: list[LocalHorizonPoint] = field(default_factory=list)


@dataclass(slots=True)
class Project:
    """A user workspace / target-centric observing campaign with a stable identity.

    ``id`` is opaque and never derived from ``dir_key``. ``dir_key`` is only the
    current directory name under the projects root (a locator, not identity).
    ``target_label`` is free text (initially the legacy ``sky_target``); ``target_id``
    stays None until a Target is linked with certainty.
    """
    id: str
    display_name: str
    dir_key: str
    target_id: str | None = None
    target_label: str | None = None
    status: str = "active"
    notes: str | None = None


@dataclass(slots=True)
class Equipment:
    """A single equipment asset such as a camera, mount, filter, or scope."""
    id: str
    equipment_type: str
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    active: bool = True
    notes: str | None = None


@dataclass(slots=True)
class AcquisitionSequence:
    """A planned sequence of homogeneous frames inside an acquisition plan."""
    sequence_order: int
    frame_type: str
    frame_count: int
    id: int | None = None
    name: str | None = None
    exposure_s: float | None = None
    iso: int | None = None
    gain: float | None = None
    offset_value: float | None = None
    binning_x: int = 1
    binning_y: int = 1
    filter_id: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class AcquisitionPlan:
    """A target-specific capture plan composed of ordered acquisition sequences."""
    id: str
    target_id: str
    name: str
    description: str | None = None
    status: str = "draft"
    sequences: list[AcquisitionSequence] = field(default_factory=list)


@dataclass(slots=True)
class ObservationEquipmentAssignment:
    """An observation-to-equipment role binding."""
    observation_id: str
    equipment_id: str
    role: str


@dataclass(slots=True)
class Observation:
    """A concrete observing session for one target, optionally linked to a plan."""
    id: str
    target_id: str
    status: str = "planned"
    observation_number: int | None = None
    site_id: str | None = None
    acquisition_plan_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    operator_notes: str | None = None
    weather_notes: str | None = None
    moon_illumination_pct: float | None = None
    equipment_assignments: list[ObservationEquipmentAssignment] = field(default_factory=list)


@dataclass(slots=True)
class Capture:
    """An ingest batch: which files arrived together, and from where.

    A Capture is not a scientific session and not a processing input. It may hold lights,
    calibration frames or both, in any folder layout. ``rel_path`` is relative to the
    Project root (``captures/<name>``). ``imported_at`` is None when the arrival time is
    unknown, as for captures that already existed on disk before they were registered.
    """
    id: str
    project_id: str
    name: str
    rel_path: str
    source_kind: str
    source_label: str | None = None
    source_path: str | None = None
    import_mode: str | None = None
    imported_at: str | None = None
    registered_at: str | None = None
    registrar_version: str | None = None


@dataclass(slots=True)
class Frame:
    """One physical observational file plus its canonical metadata and provenance.

    SQLite owns identity and metadata; the filesystem owns the bytes. ``rel_path`` is relative to
    the Project root and uses ``/``. Unknown facts are None (including ``frame_type``); ``origin``
    uses the explicit value ``unknown``. A recorded ``content_sha256`` never changes. A Frame
    belongs to a Capture and only optionally to an Observation, so calibration and legacy
    frames need no Observation or Target.
    """
    project_id: str
    capture_id: str
    rel_path: str
    id: int | None = None
    observation_id: str | None = None
    sequence_id: int | None = None
    frame_type: str | None = None
    origin: str = "unknown"
    stack_count: int | None = None
    file_format: str | None = None
    size_bytes: int | None = None
    content_sha256: str | None = None
    hashed_at: str | None = None
    width_px: int | None = None
    height_px: int | None = None
    instrument_name: str | None = None
    captured_at: str | None = None
    captured_at_source: str | None = None
    exposure_s: float | None = None
    gain: float | None = None
    offset_value: float | None = None
    iso: int | None = None
    binning_x: int | None = None
    binning_y: int | None = None
    camera_temp_c: float | None = None
    filter_id: str | None = None
    filter_name: str | None = None
    mount_ra_deg: float | None = None
    mount_dec_deg: float | None = None
    guiding_rms_arcsec: float | None = None
    pointing_source_kind: str | None = None
    pointing_source_id: str | None = None
    pointing_label: str | None = None
    planned_ra_deg: float | None = None
    planned_dec_deg: float | None = None
    accepted: bool | None = None
    rejection_reason: str | None = None
    fwhm_px: float | None = None
    fwhm_arcsec: float | None = None
    eccentricity: float | None = None
    star_count: int | None = None
    background_median: float | None = None
    background_sigma: float | None = None
    snr_estimate: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Dataset:
    """A curated set of observation material selected for downstream processing."""
    id: str
    target_id: str
    name: str
    status: str = "open"
    total_light_integration_s: float = 0.0
    notes: str | None = None
    observation_ids: list[str] = field(default_factory=list)
    frame_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ProcessingRun:
    """A durable processing record tied to a dataset version and engine output."""
    id: str
    dataset_id: str
    version_label: str
    engine_name: str
    status: str = "planned"
    engine_version: str | None = None
    pipeline: list[Any] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    linear_stack_path: str | None = None
    preview_path: str | None = None
    final_image_path: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class TelescopeState:
    """A normalized hardware-agnostic telescope telemetry snapshot."""
    adapter_id: str
    source_kind: str
    timestamp_utc: str
    connected: bool
    status: str
    is_simulated: bool = False
    site_lat_deg: float | None = None
    site_lon_deg: float | None = None
    site_elevation_m: float | None = None
    ra_hours: float | None = None
    dec_deg: float | None = None
    alt_deg: float | None = None
    az_deg: float | None = None
    target_name: str | None = None
    position_quality: str | None = None


@dataclass(slots=True)
class PlannedPointing:
    """A target location chosen for future slew or acquisition actions."""
    target_name: str | None
    ra_hours: float
    dec_deg: float
    source_kind: str = "manual"
    source_id: str | None = None
    updated_at_utc: str | None = None


@dataclass(slots=True)
class ImagingProfile:
    """The optical and sensor geometry used to derive field-of-view overlays."""
    profile_id: str
    label: str
    focal_length_mm: float
    sensor_width_mm: float
    sensor_height_mm: float
    pixel_size_um: float | None = None
    rotation_deg: float | None = None
    binning: int | None = None
    fov_width_deg: float | None = None
    fov_height_deg: float | None = None


@dataclass(slots=True)
class MosaicPanel:
    """One planned footprint tile inside a mosaic plan."""
    id: str
    mosaic_plan_id: str
    panel_index: int
    panel_label: str
    center_ra_deg: float
    center_dec_deg: float
    fov_width_deg: float
    fov_height_deg: float
    rotation_deg: float = 0.0
    row_index: int | None = None
    column_index: int | None = None
    status: str = "not_started"
    target_integration_seconds: float | None = None
    acquired_integration_seconds: float | None = None


@dataclass(slots=True)
class MosaicPlan:
    """A saved multi-panel sky coverage plan based on an imaging profile."""
    id: str
    project_slug: str
    name: str
    imaging_profile_id: str
    imaging_profile_label: str
    fov_width_deg: float
    fov_height_deg: float
    center_ra_deg: float
    center_dec_deg: float
    region_width_deg: float
    region_height_deg: float
    rotation_deg: float = 0.0
    overlap_percent: float = 10.0
    status: str = "draft"
    target_name: str | None = None
    observation_type: str | None = None
    filter: str | None = None
    selected_panel_id: str | None = None
    panels: list[MosaicPanel] = field(default_factory=list)
    # Stable link to the canonical Project. project_slug (the project's dir_key) is kept
    # for compatibility; the repository resolves project_id from it when it can.
    project_id: str | None = None
