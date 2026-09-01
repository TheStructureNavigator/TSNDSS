from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from tsn_dss.engine.light_pollution import (
    LocalRasterLightPollutionProvider,
    estimate_bortle_class,
    estimate_light_pollution_from_artificial_brightness,
)


class FakeCrs:
    def to_epsg(self) -> int:
        return 4326


class FakeRasterDataset:
    """Tiny in-memory raster with WGS84 bounds lon 10..13, lat 47..50."""

    width = 3
    height = 3
    crs = FakeCrs()

    def __init__(self, values: list[list[float]], *, nodata: float | None = -9999.0) -> None:
        self.values = values
        self.nodata = nodata
        self.closed = False

    def index(self, x: float, y: float) -> tuple[int, int]:
        col = math.floor(x - 10.0)
        row = math.floor(50.0 - y)
        return row, col

    def read(self, band: int, *, window, masked: bool = True):
        del band, masked
        if isinstance(window, tuple):
            col = int(window[0])
            row = int(window[1])
        else:
            col = int(window.col_off)
            row = int(window.row_off)
        return [[self.values[row][col]]]

    def close(self) -> None:
        self.closed = True


class LightPollutionConversionTests(unittest.TestCase):
    def test_converts_artificial_brightness_to_estimated_sky_values(self) -> None:
        measurement = estimate_light_pollution_from_artificial_brightness(0.171168465)

        self.assertAlmostEqual(measurement.artificial_brightness_mcd_m2, 0.171168465)
        self.assertAlmostEqual(measurement.natural_sky_ratio, 1.0)
        self.assertAlmostEqual(measurement.estimated_total_brightness_mcd_m2, 0.34233693)
        self.assertGreater(measurement.estimated_sqm_mag_arcsec2, 21.0)
        self.assertLessEqual(measurement.estimated_bortle_class, 4)

    def test_estimates_bortle_class_from_sqm(self) -> None:
        self.assertEqual(estimate_bortle_class(22.0), 1)
        self.assertEqual(estimate_bortle_class(21.8), 3)
        self.assertEqual(estimate_bortle_class(19.0), 6)
        self.assertEqual(estimate_bortle_class(17.0), 9)


class LocalRasterLightPollutionProviderTests(unittest.TestCase):
    def test_reads_single_cell_from_small_raster(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raster_path = Path(temp_dir) / "falchi_test.tif"
            raster_path.write_bytes(b"fake")
            dataset = FakeRasterDataset(
                [
                    [0.1, 0.2, 0.3],
                    [0.4, 0.5, 0.6],
                    [0.7, 0.8, 0.9],
                ]
            )
            provider = LocalRasterLightPollutionProvider(raster_path, dataset_opener=lambda path: dataset)

            result = provider.lookup(latitude_deg=48.5, longitude_deg=11.5)

            self.assertEqual(result.status, "available")
            self.assertEqual(result.dataset_name, "New World Atlas")
            self.assertEqual(result.source, "Falchi et al. 2016")
            self.assertAlmostEqual(result.source_value or 0.0, 0.5)
            self.assertIsNotNone(result.measurement)

    def test_reports_nodata_cell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raster_path = Path(temp_dir) / "falchi_test.tif"
            raster_path.write_bytes(b"fake")
            dataset = FakeRasterDataset([[0.1, 0.2, 0.3], [0.4, -9999.0, 0.6], [0.7, 0.8, 0.9]])
            provider = LocalRasterLightPollutionProvider(raster_path, dataset_opener=lambda path: dataset)

            result = provider.lookup(latitude_deg=48.5, longitude_deg=11.5)

            self.assertEqual(result.status, "no data")
            self.assertIsNone(result.source_value)
            self.assertIsNone(result.measurement)

    def test_reports_out_of_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raster_path = Path(temp_dir) / "falchi_test.tif"
            raster_path.write_bytes(b"fake")
            dataset = FakeRasterDataset([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]])
            provider = LocalRasterLightPollutionProvider(raster_path, dataset_opener=lambda path: dataset)

            result = provider.lookup(latitude_deg=60.0, longitude_deg=11.5)

            self.assertEqual(result.status, "out of bounds")
            self.assertIsNone(result.measurement)

    def test_rejects_invalid_coordinates(self) -> None:
        provider = LocalRasterLightPollutionProvider(None)

        with self.assertRaises(ValueError):
            provider.lookup(latitude_deg=91.0, longitude_deg=18.0)
        with self.assertRaises(ValueError):
            provider.lookup(latitude_deg=50.0, longitude_deg=181.0)

    def test_reports_missing_dataset(self) -> None:
        provider = LocalRasterLightPollutionProvider(Path("missing_falchi_raster.tif"))

        result = provider.lookup(latitude_deg=50.0, longitude_deg=18.0)

        self.assertEqual(result.status, "dataset unavailable")
        self.assertEqual(result.message, "Light pollution dataset unavailable.")
        self.assertIsNone(result.measurement)

    def test_caches_open_dataset_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raster_path = Path(temp_dir) / "falchi_test.tif"
            raster_path.write_bytes(b"fake")
            dataset = FakeRasterDataset([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]])
            open_count = 0

            def open_dataset(path: Path):
                nonlocal open_count
                open_count += 1
                return dataset

            provider = LocalRasterLightPollutionProvider(raster_path, dataset_opener=open_dataset)
            provider.lookup(latitude_deg=48.5, longitude_deg=11.5)
            provider.lookup(latitude_deg=47.5, longitude_deg=12.5)

            self.assertEqual(open_count, 1)


if __name__ == "__main__":
    unittest.main()
