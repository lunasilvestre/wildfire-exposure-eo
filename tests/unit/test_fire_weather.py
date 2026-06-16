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


# ===========================================================================
# EWDS current-season FWI source (CEMS Early Warning Data Store)
# ===========================================================================
# All tests are HERMETIC: the cdsapi download is exercised only via an injected
# fake xarray Dataset / fake client, so the default gate stays offline and never
# touches a live key.


def test_ewds_config_loads_full_system_and_pins_identity() -> None:
    cfg = fw.load_ewds_fwi_config(CONFIG_PATH)
    assert cfg.api_base == "https://ewds.climate.copernicus.eu/api"
    assert cfg.dataset == "cems-fire-historical-v1"
    assert cfg.doi == "10.24381/cds.0e89c522"
    assert cfg.license == "CC-BY-4.0"
    assert cfg.dataset_type == "intermediate_dataset"
    assert cfg.system_version == "4_1"  # underscore form
    assert cfg.crs == "EPSG:4326"
    # Full Canadian FWI system + the U.S. NFDRS burning index.
    feats = cfg.feature_names
    assert "fwi_fwi_current" in feats
    for comp in ("bui", "dc", "dmc", "ffmc", "isi", "bi"):
        assert f"fwi_{comp}_current" in feats
    # Verified live request->netcdf var map.
    by_req = {v.request_name: v.netcdf_var for v in cfg.variables}
    assert by_req["fire_weather_index"] == "fwinx"
    assert by_req["drought_code"] == "drtcode"
    assert by_req["burning_index"] == "buinfdr"


def test_ewds_config_rejects_dotted_system_version() -> None:
    with pytest.raises(ValueError, match="dotted"):
        fw.EwdsFwiConfig(
            version="0.2.0",
            api_base="x",
            dataset="cems-fire-historical-v1",
            product_type="reanalysis",
            dataset_type="intermediate_dataset",
            system_version="4.1",  # dotted -> HTTP 400 on EWDS
            grid="0.25/0.25",
            data_format="netcdf",
            crs="EPSG:4326",
            product_id="p",
            provider="prov",
            doi="d",
            license="CC-BY-4.0",
            attribution="a",
            variables=(fw.EwdsFwiVariable("fire_weather_index", "fwinx", "fwi_fwi_current"),),
            zonal_stat="mean",
        )


def test_ewds_config_rejects_empty_variables() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        fw.EwdsFwiConfig(
            version="0.2.0",
            api_base="x",
            dataset="cems-fire-historical-v1",
            product_type="reanalysis",
            dataset_type="intermediate_dataset",
            system_version="4_1",
            grid="0.25/0.25",
            data_format="netcdf",
            crs="EPSG:4326",
            product_id="p",
            provider="prov",
            doi="d",
            license="CC-BY-4.0",
            attribution="a",
            variables=(),
            zonal_stat="mean",
        )


def test_ewds_load_key_prefers_env_then_rc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDSAPI_KEY", "env-token")
    assert fw.load_ewds_key() == "env-token"
    # Falls back to ~/.cdsapirc 'key:' line; the 'url:' (CDS) is ignored.
    monkeypatch.delenv("CDSAPI_KEY", raising=False)
    rc = tmp_path / ".cdsapirc"
    rc.write_text("url: https://cds.climate.copernicus.eu/api\nkey: rc-token\n")
    assert fw.load_ewds_key(rc) == "rc-token"


def test_ewds_load_key_errors_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CDSAPI_KEY", raising=False)
    with pytest.raises(ValueError, match="no EWDS credentials"):
        fw.load_ewds_key(tmp_path / "absent.cdsapirc")


def test_normalize_longitude_known_answer() -> None:
    # 0..360 convention (351.4 == -8.6) -> -180..180, re-sorted ascending.
    arr = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    da = xr.DataArray(
        arr,
        dims=("y", "x"),
        coords={"y": [40.6, 40.5], "x": [351.4, 351.65]},
    )
    out = fw._normalize_longitude(da)
    np.testing.assert_allclose(out["x"].values, [-8.6, -8.35], atol=1e-4)
    assert list(out["x"].values) == sorted(out["x"].values)


def _fake_ewds_dataset(value: float) -> xr.Dataset:
    """A 2x2 EWDS-shaped netCDF Dataset with all configured netcdf vars."""
    cfg = fw.load_ewds_fwi_config(CONFIG_PATH)
    lat = np.array([40.855, 40.605], dtype="float64")
    lon = np.array([351.402, 351.652], dtype="float64")  # 0..360 convention
    vt = np.array([np.datetime64("2026-06-10T00:00:00")])
    data_vars: dict[str, object] = {}
    for i, var in enumerate(cfg.variables):
        arr = np.full((1, 2, 2), value + i, dtype="float32")
        data_vars[var.netcdf_var] = (("valid_time", "latitude", "longitude"), arr)
    return xr.Dataset(
        data_vars,
        coords={
            "valid_time": ("valid_time", vt),
            "latitude": ("latitude", lat),
            "longitude": ("longitude", lon),
        },
    )


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, ds: xr.Dataset) -> None:
    """Patch the cdsapi-backed fetch so build_ewds_fwi_surface stays offline."""

    class _FakeClient:
        def retrieve(self, _dataset: str, _request: dict, target: str) -> None:
            ds.to_netcdf(target)

    monkeypatch.setattr("cdsapi.Client", lambda *_a, **_k: _FakeClient())


def test_build_ewds_surface_crs_and_lon_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = fw.load_ewds_fwi_config(CONFIG_PATH)
    _patch_fetch(monkeypatch, _fake_ewds_dataset(20.0))
    surface = fw.build_ewds_fwi_surface(
        box(-8.6, 40.6, -8.2, 40.9), date(2026, 6, 10), cfg, key="fake"
    )
    assert surface.is_null is False
    assert surface.valid_date == date(2026, 6, 10)
    assert set(surface.components) == set(cfg.feature_names)
    for da in surface.components.values():
        assert da.rio.crs is not None
        assert da.rio.crs.to_epsg() == 4326  # EPSG:4326 asserted explicitly
        # Longitude normalised to -180..180 and ascending.
        assert float(da["x"].min()) < 0.0
        assert list(da["x"].values) == sorted(da["x"].values)


def test_build_ewds_surface_all_null_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = fw.load_ewds_fwi_config(CONFIG_PATH)
    ds = _fake_ewds_dataset(0.0)
    for name in list(ds.data_vars):
        ds[name].values[...] = np.nan
    _patch_fetch(monkeypatch, ds)
    surface = fw.build_ewds_fwi_surface(
        box(-8.6, 40.6, -8.2, 40.9), date(2026, 6, 10), cfg, key="fake"
    )
    assert surface.is_null is True


def _ewds_buffers() -> gpd.GeoDataFrame:
    # One buffer at the AOI centre, reprojected to ASSET_CRS (EPSG:32629).
    return gpd.GeoDataFrame(
        {"asset_id": ["A"], "geometry": [box(-8.5, 40.7, -8.4, 40.8)]},
        crs="EPSG:4326",
    ).to_crs("EPSG:32629")


def test_ewds_components_constant_surface_known_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = fw.load_ewds_fwi_config(CONFIG_PATH)
    _patch_fetch(monkeypatch, _fake_ewds_dataset(30.0))
    surface = fw.build_ewds_fwi_surface(
        box(-8.6, 40.6, -8.2, 40.9), date(2026, 6, 10), cfg, key="fake"
    )
    series = fw.ewds_fwi_components(_ewds_buffers(), surface, cfg)
    assert series is not None
    assert set(series) == set(cfg.feature_names)
    # Each var was filled with (30 + index); zonal mean of a constant = constant.
    for i, var in enumerate(cfg.variables):
        s = series[var.feature_name]
        assert s.name == var.feature_name
        assert s["A"] == pytest.approx(30.0 + i, abs=1e-3)


def test_ewds_components_null_surface_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = fw.load_ewds_fwi_config(CONFIG_PATH)
    ds = _fake_ewds_dataset(0.0)
    for name in list(ds.data_vars):
        ds[name].values[...] = np.nan
    _patch_fetch(monkeypatch, ds)
    surface = fw.build_ewds_fwi_surface(
        box(-8.6, 40.6, -8.2, 40.9), date(2026, 6, 10), cfg, key="fake"
    )
    assert fw.ewds_fwi_components(_ewds_buffers(), surface, cfg) is None


def test_ewds_components_rejects_wrong_buffer_crs(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = fw.load_ewds_fwi_config(CONFIG_PATH)
    _patch_fetch(monkeypatch, _fake_ewds_dataset(10.0))
    surface = fw.build_ewds_fwi_surface(
        box(-8.6, 40.6, -8.2, 40.9), date(2026, 6, 10), cfg, key="fake"
    )
    bad = _ewds_buffers().to_crs("EPSG:4326")  # not the metric ASSET_CRS
    with pytest.raises(ValueError, match="expected EPSG:32629"):
        fw.ewds_fwi_components(bad, surface, cfg)


def test_ewds_provenance_carries_identity_and_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = fw.load_ewds_fwi_config(CONFIG_PATH)
    _patch_fetch(monkeypatch, _fake_ewds_dataset(15.0))
    surface = fw.build_ewds_fwi_surface(
        box(-8.6, 40.6, -8.2, 40.9), date(2026, 6, 10), cfg, key="secret-token"
    )
    prov = fw.ewds_fwi_provenance(cfg, surface)
    assert prov["fwi_product_id"] == "cems-fire-historical-v1"
    assert prov["fwi_doi"] == "10.24381/cds.0e89c522"
    assert prov["fwi_dataset_type"] == "intermediate_dataset"
    assert prov["fwi_system_version"] == "4_1"
    assert prov["fwi_valid_date"] == "2026-06-10"
    assert prov["fwi_variable_map"]["fire_weather_index"] == "fwinx"
    # The API key must NEVER leak into provenance.
    assert "secret-token" not in repr(prov)


# ---------------------------------------------------------------------------
# Schema acceptance: AssetFeatures + ScoredAssetProvenance carry EWDS fields
# ---------------------------------------------------------------------------
def test_asset_features_accepts_ewds_components() -> None:
    from wildfire_exposure_eo.schemas.scored_asset import (
        EWDS_FWI_FEATURE_NAMES,
        AssetFeatures,
    )

    feats = AssetFeatures(
        fwi_fwi_current=29.7,
        fwi_bui_current=86.0,
        fwi_dc_current=237.4,  # widest bound (drought code)
        fwi_dmc_current=79.0,
        fwi_ffmc_current=90.7,
        fwi_isi_current=10.3,
        fwi_bi_current=10.0,
    )
    assert feats.fwi_fwi_current == pytest.approx(29.7)
    assert feats.fwi_dc_current == pytest.approx(237.4)
    # All seven EWDS component names are AVAILABLE (declared on the model).
    for name in EWDS_FWI_FEATURE_NAMES:
        assert hasattr(feats, name)


def test_ewds_features_available_but_unweighted() -> None:
    # All seven EWDS names are AVAILABLE (declared in FEATURE_NAMES). At 0.3.1 NONE
    # carry a score weight — Wave-2 validation showed backdated FWI does not improve
    # burn discrimination, so the full EWDS FWI system is the operational overlay,
    # not part of the validated structural score.
    import yaml

    from wildfire_exposure_eo.schemas.scored_asset import (
        EWDS_FWI_FEATURE_NAMES,
        FEATURE_NAMES,
    )

    for name in EWDS_FWI_FEATURE_NAMES:
        assert name in FEATURE_NAMES
    weights = yaml.safe_load(Path("config/exposure_score.yaml").read_text())["weights"]
    assert set(EWDS_FWI_FEATURE_NAMES).isdisjoint(weights)  # all seven stay unweighted


def test_provenance_accepts_ewds_fields() -> None:
    from wildfire_exposure_eo.schemas.scored_asset import ScoredAssetProvenance

    sha = "a" * 64
    prov = ScoredAssetProvenance(
        model_version="0.2.0",
        config_sha=sha,
        crosswalk_sha=sha,
        run_id="ewds-test",
        code_commit_sha="deadbeef",
        aoi_path="data/aoi/pilot.geojson",
        aoi_geometry_sha=sha,
        window_start=date(2025, 6, 16),
        window_end=date(2026, 6, 16),
        osm_parquet_sha=sha,
        burns_parquet_sha=sha,
        fuel_cog_sha=sha,
        gch_cache_sha=sha,
        burn_share_threshold=0.5,
        fwi_product_id="cems-fire-historical-v1",
        fwi_doi="10.24381/cds.0e89c522",
        fwi_dataset_type="intermediate_dataset",
        fwi_system_version="4_1",
        fwi_requested_date="2026-06-10",
        fwi_valid_date="2026-06-10",
        fwi_variable_map={"fire_weather_index": "fwinx"},
    )
    assert prov.fwi_product_id == "cems-fire-historical-v1"
    assert prov.fwi_valid_date == "2026-06-10"
    assert prov.fwi_variable_map["fire_weather_index"] == "fwinx"
