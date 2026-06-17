"""Hermetic unit + property tests for the FireScope reader (WU-21).

These tests build a synthetic in-memory GeoTIFF (EPSG:3857, like the real
FireScope raster) with a known per-pixel value and assert the CRS-explicit
point sampler reads the right cell. No network: the live ``/vsicurl/`` read in
``sample_firescope_at_points`` is excluded from the default pytest gate by being
exercised only through ``scripts/28_firescope_benchmark.py --live``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from wildfire_exposure_eo import firescope

if TYPE_CHECKING:
    from rasterio.io import DatasetReader


def _synthetic_3857_raster(
    *,
    width: int = 20,
    height: int = 16,
    res: float = 30.0,
    origin_x: float = -1_000_000.0,
    origin_y: float = 5_000_000.0,
    nodata: int = firescope.FIRESCOPE_NODATA,
) -> MemoryFile:
    """An EPSG:3857 uint8 raster whose value equals ``row * width + col`` (mod 255).

    A deterministic, invertible per-cell value so a point landing in cell (r, c)
    must read back ``(r * width + c) % 255``. One cell is set to ``nodata``.
    """
    data = np.arange(width * height, dtype="int64").reshape(height, width)
    data = (data % 255).astype("uint8")  # keep clear of the 255 nodata sentinel
    data[2, 3] = nodata  # a known nodata cell
    transform = from_origin(origin_x, origin_y, res, res)
    mem = MemoryFile()
    with mem.open(
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=transform,
        nodata=nodata,
    ) as ds:
        ds.write(data, 1)
    return mem


def _cell_center_lonlat(ds: DatasetReader, row: int, col: int) -> tuple[float, float]:
    """Lon/lat (EPSG:4326) of the center of cell (row, col) of a 3857 raster."""
    x, y = ds.xy(row, col)  # center of the cell, in EPSG:3857
    to_wgs = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon, lat = to_wgs.transform(x, y)
    return float(lon), float(lat)


def test_nearest_cell_value_known_answer() -> None:
    """A point at a cell center reads that exact cell's value (CRS round-trip)."""
    mem = _synthetic_3857_raster()
    with mem.open() as ds:
        targets = [(0, 0), (5, 7), (10, 11), (15, 19)]
        lons, lats, expected = [], [], []
        for r, c in targets:
            lon, lat = _cell_center_lonlat(ds, r, c)
            lons.append(lon)
            lats.append(lat)
            expected.append((r * ds.width + c) % 255)
        out = firescope.sample_raster_at_points(
            ds, np.array(lons), np.array(lats), points_crs="EPSG:4326"
        )
    assert out.n_valid == len(targets)
    np.testing.assert_array_equal(out.values, np.array(expected, dtype="float64"))


def test_nodata_cell_becomes_nan() -> None:
    """A point in the declared-nodata cell returns NaN, counted as nodata."""
    mem = _synthetic_3857_raster()
    with mem.open() as ds:
        lon, lat = _cell_center_lonlat(ds, 2, 3)  # the nodata cell
        out = firescope.sample_raster_at_points(ds, np.array([lon]), np.array([lat]))
    assert np.isnan(out.values[0])
    assert out.n_nodata == 1
    assert out.n_valid == 0


def test_point_outside_footprint_is_nan() -> None:
    """A far-away point falls outside the footprint and is NaN (not an exception)."""
    mem = _synthetic_3857_raster()
    with mem.open() as ds:
        # A point in Portugal is nowhere near this synthetic North-Atlantic raster.
        out = firescope.sample_raster_at_points(ds, np.array([-8.0]), np.array([40.0]))
    assert np.isnan(out.values[0])
    assert out.n_outside == 1
    assert out.n_valid == 0


def test_mixed_points_partition_counts() -> None:
    """Valid + nodata + outside points partition cleanly and align by index."""
    mem = _synthetic_3857_raster()
    with mem.open() as ds:
        valid_lon, valid_lat = _cell_center_lonlat(ds, 4, 4)
        nd_lon, nd_lat = _cell_center_lonlat(ds, 2, 3)
        out = firescope.sample_raster_at_points(
            ds,
            np.array([valid_lon, nd_lon, -8.0]),
            np.array([valid_lat, nd_lat, 40.0]),
        )
    assert out.values[0] == (4 * ds.width + 4) % 255
    assert np.isnan(out.values[1])
    assert np.isnan(out.values[2])
    assert (out.n_valid, out.n_nodata, out.n_outside) == (1, 1, 1)


def test_length_mismatch_raises() -> None:
    mem = _synthetic_3857_raster()
    with mem.open() as ds, pytest.raises(ValueError, match="length mismatch"):
        firescope.sample_raster_at_points(ds, np.array([1.0, 2.0]), np.array([1.0]))


@given(
    row=st.integers(min_value=0, max_value=15),
    col=st.integers(min_value=0, max_value=19),
)
def test_property_cell_center_round_trips(row: int, col: int) -> None:
    """For any cell, lon/lat of its center samples back to that cell's value."""
    mem = _synthetic_3857_raster()
    with mem.open() as ds:
        if (row, col) == (2, 3):  # the nodata cell — covered separately
            return
        lon, lat = _cell_center_lonlat(ds, row, col)
        out = firescope.sample_raster_at_points(ds, np.array([lon]), np.array([lat]))
    assert out.values[0] == (row * 20 + col) % 255


def test_provenance_has_real_identifiers() -> None:
    """Provenance carries the verified HF id / revision / oid / license (#1, #3)."""
    prov = firescope.provenance()
    assert prov["firescope_dataset_id"] == "INSAIT-Institute/firescope-risk-2026"
    assert prov["firescope_license"] == "CC-BY-4.0"
    assert prov["firescope_arxiv"] == "2511.17171"
    assert len(str(prov["firescope_raster_lfs_oid_sha256"])) == 64
    assert len(str(prov["firescope_dataset_revision"])) == 40
