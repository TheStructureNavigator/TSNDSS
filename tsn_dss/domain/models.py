from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Target:
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
class Site:
    id: str
    name: str
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    elevation_m: float | None = None
    sqm_mag_arcsec2: float | None = None
    bortle_class: int | None = None
    south_horizon_open: bool = False
    notes: str | None = None


@dataclass(slots=True)
class Equipment:
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
    id: str
    target_id: str
    name: str
    description: str | None = None
    status: str = "draft"
    sequences: list[AcquisitionSequence] = field(default_factory=list)


@dataclass(slots=True)
class ObservationEquipmentAssignment:
    observation_id: str
    equipment_id: str
    role: str


@dataclass(slots=True)
class Observation:
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
class Frame:
    observation_id: str
    frame_type: str
    file_path: str
    id: int | None = None
    sequence_id: int | None = None
    captured_at: str | None = None
    exposure_s: float | None = None
    iso: int | None = None
    gain: float | None = None
    offset_value: float | None = None
    filter_id: str | None = None
    accepted: bool | None = None
    rejection_reason: str | None = None
    fwhm_px: float | None = None
    fwhm_arcsec: float | None = None
    eccentricity: float | None = None
    star_count: int | None = None
    background_median: float | None = None
    background_sigma: float | None = None
    snr_estimate: float | None = None
    mount_ra_deg: float | None = None
    mount_dec_deg: float | None = None
    guiding_rms_arcsec: float | None = None
    camera_temp_c: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Dataset:
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
