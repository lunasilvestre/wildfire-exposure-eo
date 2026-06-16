"""Unit tests for the seasonal fire-weather feature (WU-17, pillar 0).

Network-free: the WMS fetch is exercised only through injected synthetic
rasters, so the default ``uv run pytest`` gate stays offline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rioxarray  # noqa: F401  (registers the .rio accessor used on DataArrays)
import xarray as xr
from shapely.geometry import box

from wildfire_exposure_eo import fire_weather as fw

CONFIG_PATH = Path("config/fire_weather.yaml")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_config_loads_and_pins_real_identity() -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)
    assert cfg.layer == "nasa.fwi_gpm.fwi"
    assert cfg.product_id == "GWIS/nasa.fwi_gpm.fwi"
    assert cfg.crs == "EPSG:4326"
    assert cfg.raster_format == "image/tiff"  # raw values, not styled RGB
    assert cfg.feature_name == "fire_danger_seasonal"
    assert cfg.season_start_month <= cfg.season_end_month
    assert cfg.archive_start < cfg.archive_end
    assert cfg.version  # never empty (provenance non-negotiable)


def test_config_rejects_bad_season_months() -> None:
    with pytest.raises(ValueError, match="invalid season months"):
        fw.FireWeatherConfig(
            version="0.1.0",
            endpoint="x",
            wms_version="1.1.1",
            layer="l",
            raster_format="image/tiff",
            crs="EPSG:4326",
            product_id="p",
            provider="prov",
            index="fwi",
            attribution="a",
            archive_start=date(2014, 5, 1),
            archive_end=date(2020, 12, 31),
            feature_name="fire_danger_seasonal",
            season_reducer="p95",
            season_start_month=10,
            season_end_month=6,
            sample_days=(1, 15),
            zonal_stat="mean",
        )


def test_config_rejects_unknown_reducer() -> None:
    with pytest.raises(ValueError, match="unsupported season_reducer"):
        fw.FireWeatherConfig(
            version="0.1.0",
            endpoint="x",
            wms_version="1.1.1",
            layer="l",
            raster_format="image/tiff",
            crs="EPSG:4326",
            product_id="p",
            provider="prov",
            index="fwi",
            attribution="a",
            archive_start=date(2014, 5, 1),
            archive_end=date(2020, 12, 31),
            feature_name="fire_danger_seasonal",
            season_reducer="mean",  # not allowed
            season_start_month=6,
            season_end_month=10,
            sample_days=(1, 15),
            zonal_stat="mean",
        )


# ---------------------------------------------------------------------------
# Season sampling + archive bounds
# ---------------------------------------------------------------------------
def test_season_sample_dates_is_cross_product_sorted() -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)
    dates = fw.season_sample_dates(2017, cfg)
    n_months = cfg.season_end_month - cfg.season_start_month + 1
    assert len(dates) == n_months * len(cfg.sample_days)
    assert dates == sorted(dates)
    assert all(d.year == 2017 for d in dates)
    # Deterministic: identical inputs → identical output.
    assert fw.season_sample_dates(2017, cfg) == dates


def test_season_in_archive_boundary() -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)
    assert fw.season_in_archive(2017, cfg) is True
    assert fw.season_in_archive(2020, cfg) is True
    assert fw.season_in_archive(2024, cfg) is False  # archive ends 2020


# ---------------------------------------------------------------------------
# WMS GetMap URL builder (lon/lat axis order, raw-tiff format, time)
# ---------------------------------------------------------------------------
def test_getmap_url_uses_lonlat_and_time() -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)
    endpoint, params = fw._wms_getmap_url(
        cfg, (-8.6, 40.6, -8.2, 40.9), width=72, height=54, when=date(2017, 8, 15)
    )
    assert endpoint == cfg.endpoint
    assert params["bbox"] == "-8.6,40.6,-8.2,40.9"  # WMS 1.1.1 EPSG:4326 = lon,lat
    assert params["format"] == "image/tiff"
    assert params["time"] == "2017-08-15"
    assert params["layers"] == cfg.layer


# ---------------------------------------------------------------------------
# Zonal aggregator — known-answer on a synthetic FWI surface
# ---------------------------------------------------------------------------
def _synthetic_surface(value: float, *, is_null: bool = False) -> fw.SeasonalFwiSurface:
    # 200 m x 200 m grid at 10 m in EPSG:32629 (the surface CRS for this test).
    x0, y0, res, n = 560000.0, 4420200.0, 10.0, 20
    xs = x0 + (np.arange(n) + 0.5) * res
    ys = y0 - (np.arange(n) + 0.5) * res
    arr = np.full((n, n), value, dtype="float32")
    da = xr.DataArray(arr, dims=("y", "x"), coords={"y": ys, "x": xs})
    da = da.rio.write_crs("EPSG:4326")  # tag a CRS; overwrite below to be explicit
    da = da.rio.write_crs("EPSG:32629").rio.write_nodata(np.nan)
    return fw.SeasonalFwiSurface(
        surface=da, sample_dates=("2017-08-01", "2017-08-15"), is_null=is_null
    )


def _buffers() -> gpd.GeoDataFrame:
    # One 100 m x 100 m buffer fully inside the synthetic surface, EPSG:32629.
    return gpd.GeoDataFrame(
        {"asset_id": ["A"], "geometry": [box(560050, 4420050, 560150, 4420150)]},
        crs="EPSG:32629",
    )


def test_fire_danger_seasonal_constant_surface_known_answer() -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)
    surface = _synthetic_surface(42.5)
    series = fw.fire_danger_seasonal(_buffers(), surface, cfg)
    assert series is not None
    assert series.name == "fire_danger_seasonal"
    # Zonal mean of a constant surface is the constant.
    assert series["A"] == pytest.approx(42.5, rel=1e-4)


def test_fire_danger_seasonal_null_surface_returns_none() -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)
    surface = _synthetic_surface(0.0, is_null=True)
    assert fw.fire_danger_seasonal(_buffers(), surface, cfg) is None


def test_fire_danger_seasonal_rejects_wrong_buffer_crs() -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)
    bad = _buffers().to_crs("EPSG:4326")  # not the metric ASSET_CRS
    with pytest.raises(ValueError, match="expected EPSG:32629"):
        fw.fire_danger_seasonal(bad, _synthetic_surface(10.0), cfg)


# ---------------------------------------------------------------------------
# Surface reduction (p95) on injected daily rasters (no network)
# ---------------------------------------------------------------------------
def test_build_surface_p95_and_null_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)

    # Stub the per-day fetch with deterministic synthetic rasters in EPSG:4326.
    def fake_fetch(_cfg, _bbox, *, width, height, when, session=None, timeout=60.0):
        xs = -8.6 + (np.arange(width) + 0.5) * 0.005
        ys = 40.9 - (np.arange(height) + 0.5) * 0.005
        # Value rises with day-of-year so p95 picks the late, high samples.
        val = float(when.timetuple().tm_yday) / 10.0
        arr = np.full((height, width), val, dtype="float32")
        da = xr.DataArray(arr, dims=("y", "x"), coords={"y": ys, "x": xs})
        return da.rio.write_crs("EPSG:4326")

    monkeypatch.setattr(fw, "_fetch_daily_fwi", fake_fetch)
    surface = fw.build_seasonal_fwi_surface(box(-8.6, 40.6, -8.2, 40.9), 2017, cfg)
    assert surface.is_null is False
    assert surface.surface.rio.crs is not None
    assert surface.surface.rio.crs.to_epsg() == 4326
    # p95 across the season ≈ the late-season high value (Oct day-of-year ~288).
    peak = float(surface.surface.max())
    assert peak > 25.0
    assert len(surface.sample_dates) == len(fw.season_sample_dates(2017, cfg))


def test_build_surface_all_zero_is_null(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)

    def zero_fetch(_cfg, _bbox, *, width, height, when, session=None, timeout=60.0):
        xs = -8.6 + (np.arange(width) + 0.5) * 0.005
        ys = 40.9 - (np.arange(height) + 0.5) * 0.005
        arr = np.zeros((height, width), dtype="float32")
        da = xr.DataArray(arr, dims=("y", "x"), coords={"y": ys, "x": xs})
        return da.rio.write_crs("EPSG:4326")

    monkeypatch.setattr(fw, "_fetch_daily_fwi", zero_fetch)
    surface = fw.build_seasonal_fwi_surface(box(-8.6, 40.6, -8.2, 40.9), 2024, cfg)
    assert surface.is_null is True


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def test_provenance_carries_source_identity() -> None:
    cfg = fw.load_fire_weather_config(CONFIG_PATH)
    surface = _synthetic_surface(30.0)
    prov = fw.fire_weather_provenance(cfg, surface, 2017)
    assert prov["fire_weather_product_id"] == "GWIS/nasa.fwi_gpm.fwi"
    assert prov["fire_weather_season_year"] == 2017
    assert prov["fire_weather_out_of_archive"] is False
    assert prov["fire_weather_sample_dates"] == list(surface.sample_dates)
    assert "JRC" in prov["fire_weather_provider"]
