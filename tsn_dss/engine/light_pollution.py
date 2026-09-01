from __future__ import annotations

"""Light pollution point data providers and TSN DSS domain conversions."""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

NATURAL_SKY_BRIGHTNESS_MCD_M2 = 0.171168465
SQM_ZERO_POINT_LUMINANCE_MCD_M2 = 108000000

NEW_WORLD_ATLAS_DATASET_NAME = "New World Atlas"
NEW_WORLD_ATLAS_SOURCE = "Falchi et al. 2016"
NEW_WORLD_ATLAS_SOURCE_UNIT = "mcd/m² artificial sky brightness"

try:  # pragma: no cover - dependency availability is environment-specific
    import rasterio
    from rasterio.errors import RasterioIOError
    from rasterio.warp import transform as rasterio_transform
    from rasterio.windows import Window
except ImportError:  # pragma: no cover - covered by provider unavailable tests
    rasterio = None  # type: ignore[assignment]
    RasterioIOError = OSError  # type: ignore[assignment,misc]
    rasterio_transform = None  # type: ignore[assignment]
    Window = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class LightPollutionMeasurement:
    """Derived astronomy-facing values from artificial sky brightness."""

    artificial_brightness_mcd_m2: float
    natural_sky_ratio: float
    estimated_total_brightness_mcd_m2: float
    estimated_sqm_mag_arcsec2: float
    estimated_bortle_class: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "artificial_brightness_mcd_m2": self.artificial_brightness_mcd_m2,
            "natural_sky_ratio": self.natural_sky_ratio,
            "estimated_total_brightness_mcd_m2": self.estimated_total_brightness_mcd_m2,
            "estimated_sqm_mag_arcsec2": self.estimated_sqm_mag_arcsec2,
            "estimated_bortle_class": self.estimated_bortle_class,
        }


@dataclass(frozen=True, slots=True)
class LightPollutionPointResult:
    """Normalized result returned by a light-pollution point data provider."""

    status: str
    provider_name: str
    dataset_name: str
    source: str
    latitude_deg: float
    longitude_deg: float
    source_value: float | None
    source_unit: str | None
    measurement: LightPollutionMeasurement | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider_name": self.provider_name,
            "dataset_name": self.dataset_name,
            "source": self.source,
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "source_value": self.source_value,
            "source_unit": self.source_unit,
            "measurement": self.measurement.to_dict() if self.measurement is not None else None,
            "message": self.message,
        }


class LocalRasterLightPollutionProvider:
    """Read point values from a local New World Atlas/Falchi 2016 GeoTIFF."""

    provider_name = "local-raster"
    dataset_name = NEW_WORLD_ATLAS_DATASET_NAME
    source = NEW_WORLD_ATLAS_SOURCE
    source_unit = NEW_WORLD_ATLAS_SOURCE_UNIT

    def __init__(
        self,
        raster_path: str | Path | None,
        *,
        dataset_opener: Callable[[Path], Any] | None = None,
        coordinate_transformer: Callable[[str, Any, list[float], list[float]], tuple[list[float], list[float]]] | None = None,
    ) -> None:
        self.raster_path = Path(raster_path) if raster_path else None
        self._dataset_opener = dataset_opener
        self._coordinate_transformer = coordinate_transformer
        self._dataset: Any | None = None

    def lookup(self, *, latitude_deg: float, longitude_deg: float) -> LightPollutionPointResult:
        """Return the raster cell value for a WGS84 point without loading the full raster."""
        _validate_coordinates(latitude_deg=latitude_deg, longitude_deg=longitude_deg)

        dataset = self._open_dataset()
        if dataset is None:
            return self._unavailable(
                latitude_deg=latitude_deg,
                longitude_deg=longitude_deg,
                status="dataset unavailable",
                message="Light pollution dataset unavailable.",
            )

        try:
            x, y = self._to_dataset_coordinates(dataset, latitude_deg=latitude_deg, longitude_deg=longitude_deg)
            row, col = dataset.index(x, y)
        except Exception as error:
            return self._unavailable(
                latitude_deg=latitude_deg,
                longitude_deg=longitude_deg,
                status="out of bounds",
                message=f"Point is outside the light pollution raster extent. {error}",
            )

        width = int(getattr(dataset, "width"))
        height = int(getattr(dataset, "height"))
        if row < 0 or col < 0 or row >= height or col >= width:
            return self._unavailable(
                latitude_deg=latitude_deg,
                longitude_deg=longitude_deg,
                status="out of bounds",
                message="Point is outside the light pollution raster extent.",
            )

        value = self._read_cell(dataset, row=row, col=col)
        if value is None:
            return self._unavailable(
                latitude_deg=latitude_deg,
                longitude_deg=longitude_deg,
                status="no data",
                message="No light pollution value available for this point.",
            )

        measurement = estimate_light_pollution_from_artificial_brightness(value)
        return LightPollutionPointResult(
            status="available",
            provider_name=self.provider_name,
            dataset_name=self.dataset_name,
            source=self.source,
            latitude_deg=latitude_deg,
            longitude_deg=longitude_deg,
            source_value=value,
            source_unit=self.source_unit,
            measurement=measurement,
            message="Modeled artificial sky brightness from local New World Atlas raster.",
        )

    def close(self) -> None:
        if self._dataset is not None and hasattr(self._dataset, "close"):
            self._dataset.close()
        self._dataset = None

    def _open_dataset(self) -> Any | None:
        if self._dataset is not None:
            return self._dataset
        if self.raster_path is None:
            return None
        if not self.raster_path.exists():
            return None

        opener = self._dataset_opener
        if opener is None:
            if rasterio is None:
                return None
            opener = rasterio.open

        try:
            self._dataset = opener(self.raster_path)
        except (OSError, RasterioIOError):
            self._dataset = None
        return self._dataset

    def _to_dataset_coordinates(self, dataset: Any, *, latitude_deg: float, longitude_deg: float) -> tuple[float, float]:
        dataset_crs = getattr(dataset, "crs", None)
        if dataset_crs is None or _is_wgs84_crs(dataset_crs):
            return longitude_deg, latitude_deg

        transformer = self._coordinate_transformer or rasterio_transform
        if transformer is None:
            raise RuntimeError("Raster CRS transformation requires rasterio.")
        xs, ys = transformer("EPSG:4326", dataset_crs, [longitude_deg], [latitude_deg])
        return float(xs[0]), float(ys[0])

    def _read_cell(self, dataset: Any, *, row: int, col: int) -> float | None:
        if Window is not None:
            window = Window(col, row, 1, 1)
        else:
            window = (col, row, 1, 1)

        cell = dataset.read(1, window=window, masked=True)
        value = _extract_single_value(cell)
        if value is None:
            return None

        nodata = getattr(dataset, "nodata", None)
        if nodata is not None and math.isclose(value, float(nodata), rel_tol=0.0, abs_tol=1e-12):
            return None
        if not math.isfinite(value):
            return None
        return value

    def _unavailable(
        self,
        *,
        latitude_deg: float,
        longitude_deg: float,
        status: str,
        message: str,
    ) -> LightPollutionPointResult:
        return LightPollutionPointResult(
            status=status,
            provider_name=self.provider_name,
            dataset_name=self.dataset_name,
            source=self.source,
            latitude_deg=latitude_deg,
            longitude_deg=longitude_deg,
            source_value=None,
            source_unit=self.source_unit,
            measurement=None,
            message=message,
        )


def estimate_light_pollution_from_artificial_brightness(
    artificial_brightness_mcd_m2: float,
) -> LightPollutionMeasurement:
    """Convert artificial sky brightness to TSN DSS estimated observing values."""
    safe_artificial_brightness = max(0.0, float(artificial_brightness_mcd_m2))
    estimated_total_brightness = safe_artificial_brightness + NATURAL_SKY_BRIGHTNESS_MCD_M2
    estimated_sqm = math.log10(
        estimated_total_brightness / SQM_ZERO_POINT_LUMINANCE_MCD_M2,
    ) / -0.4
    return LightPollutionMeasurement(
        artificial_brightness_mcd_m2=safe_artificial_brightness,
        natural_sky_ratio=safe_artificial_brightness / NATURAL_SKY_BRIGHTNESS_MCD_M2,
        estimated_total_brightness_mcd_m2=estimated_total_brightness,
        estimated_sqm_mag_arcsec2=estimated_sqm,
        estimated_bortle_class=estimate_bortle_class(estimated_sqm),
    )


def estimate_bortle_class(sqm_mag_arcsec2: float) -> int:
    if sqm_mag_arcsec2 >= 21.99:
        return 1
    if sqm_mag_arcsec2 >= 21.89:
        return 2
    if sqm_mag_arcsec2 >= 21.69:
        return 3
    if sqm_mag_arcsec2 >= 20.49:
        return 4
    if sqm_mag_arcsec2 >= 19.50:
        return 5
    if sqm_mag_arcsec2 >= 18.94:
        return 6
    if sqm_mag_arcsec2 >= 18.38:
        return 7
    if sqm_mag_arcsec2 >= 17.80:
        return 8
    return 9


def _validate_coordinates(*, latitude_deg: float, longitude_deg: float) -> None:
    if not math.isfinite(latitude_deg) or latitude_deg < -90.0 or latitude_deg > 90.0:
        raise ValueError("Latitude must be a finite value between -90 and 90 degrees.")
    if not math.isfinite(longitude_deg) or longitude_deg < -180.0 or longitude_deg > 180.0:
        raise ValueError("Longitude must be a finite value between -180 and 180 degrees.")


def _is_wgs84_crs(crs: Any) -> bool:
    try:
        return crs.to_epsg() == 4326
    except AttributeError:
        return str(crs).upper() in {"EPSG:4326", "WGS84", "WGS 84"}


def _extract_single_value(cell: Any) -> float | None:
    if hasattr(cell, "mask"):
        mask = getattr(cell, "mask")
        if bool(mask.all() if hasattr(mask, "all") else mask):
            return None
    if hasattr(cell, "filled"):
        cell = cell.filled(math.nan)
    try:
        value = cell[0][0]
    except (TypeError, IndexError):
        try:
            value = cell[0, 0]
        except (TypeError, IndexError):
            value = cell
    try:
        value_float = float(value)
    except (TypeError, ValueError):
        return None
    return value_float if math.isfinite(value_float) else None
