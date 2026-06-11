"""Hermetic end-to-end integration test for the scoring pipeline (WU-6).

Builds tiny synthetic fixtures (rasters + asset/burn GeoParquet + AOI) in a temp
directory and runs :func:`features.run_scoring` with the network rasters
(slope, NBR delta) injected, so the whole pipeline — features → composite rank →
two GeoParquet artefacts → per-row schema validation — runs offline and fast.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import rioxarray  # noqa: F401  (registers the .rio accessor used on injected rasters)
import xarray as xr
from pyproj import Transformer
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import Point, box, mapping

from wildfire_exposure_eo import features as feat
from wildfire_exposure_eo.schemas import ScoredAsset

CONFIG = Path("config/exposure_score.yaml")
TAXONOMY = Path("data/taxonomy/critical_infrastructure.yaml")
DUMMY_SHA = "a" * 64

# 32629 working grid: 2 km x 2 km at 10 m, top-left origin.
_X0, _Y0 = 560000.0, 4421000.0
_W = _H = 200
_RES = 10.0
# Four assets near the grid centre (offsets in metres), each a valid taxonomy class.
_ASSETS = [
    ("osm:node/1", "education.school", "node", 1, -300.0, -300.0),
    ("osm:node/2", "power.tower", "node", 2, 300.0, -300.0),
    ("osm:way/3", "emergency.fire_station", "way", 3, -300.0, 300.0),
    ("osm:way/4", "water.reservoir", "way", 4, 300.0, 300.0),
]


def _grid_coords() -> tuple[np.ndarray, np.ndarray]:
    xs = _X0 + (np.arange(_W) + 0.5) * _RES
    ys = _Y0 - (np.arange(_H) + 0.5) * _RES
    return xs, ys


def _write_2band_fuel(path: Path) -> None:
    transform = from_origin(_X0, _Y0, _RES, _RES)
    klass = np.full((_H, _W), 7, dtype="uint8")
    sev = np.tile(np.linspace(0, 100, _W, dtype="uint8"), (_H, 1))
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=_H,
        width=_W,
        count=2,
        dtype="uint8",
        crs="EPSG:32629",
        transform=transform,
        nodata=255,
    ) as ds:
        ds.write(klass, 1)
        ds.write(sev, 2)


def _write_gch(path: Path) -> None:
    transform = from_origin(_X0, _Y0, _RES, _RES)
    height = np.tile(np.linspace(0, 30, _W, dtype="uint8"), (_H, 1))
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=_H,
        width=_W,
        count=1,
        dtype="uint8",
        crs="EPSG:32629",
        transform=transform,
        nodata=255,
    ) as ds:
        ds.write(height, 1)


def _aoi_bounds_4326() -> tuple[float, float, float, float]:
    return transform_bounds("EPSG:32629", "EPSG:4326", _X0, _Y0 - _H * _RES, _X0 + _W * _RES, _Y0)


def _write_burn_scar(path: Path) -> None:
    left, bottom, right, top = _aoi_bounds_4326()
    n = 100
    transform = from_bounds(left, bottom, right, top, n, n)
    prob = np.full((n, n), 0.1, dtype="float32")
    prob[: n // 2, : n // 2] = 0.9  # a high-probability quadrant
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=n,
        width=n,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=-9999.0,
    ) as ds:
        ds.write(prob, 1)
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "window_start": "2025-06-09",
                "window_end": "2026-06-09",
                "binarisation_threshold": 0.5,
            }
        )
    )


def _injected_rasters() -> tuple[xr.DataArray, xr.DataArray]:
    xs, ys = _grid_coords()
    slope = np.tile(np.linspace(0, 40, _W), (_H, 1)).astype("float32")
    nbr = np.tile(np.linspace(-0.5, 0.5, _W), (_H, 1)).astype("float32")
    slope_da = xr.DataArray(slope, dims=("y", "x"), coords={"y": ys, "x": xs})
    slope_da = slope_da.rio.write_crs("EPSG:32629").rio.write_nodata(np.nan)
    nbr_da = xr.DataArray(nbr, dims=("y", "x"), coords={"y": ys, "x": xs})
    nbr_da = nbr_da.rio.write_crs("EPSG:32629").rio.write_nodata(np.nan)
    return slope_da, nbr_da


def _build_fixtures(tmp: Path) -> dict[str, Path]:
    to_wgs = Transformer.from_crs("EPSG:32629", "EPSG:4326", always_xy=True)
    cx, cy = _X0 + _W * _RES / 2, _Y0 - _H * _RES / 2

    rows = []
    for aid, klass, otype, oid, dx, dy in _ASSETS:
        lon, lat = to_wgs.transform(cx + dx, cy + dy)
        pt = Point(lon, lat)
        rows.append(
            {
                "asset_id": aid,
                "asset_class": klass,
                "osm_type": otype,
                "osm_id": oid,
                "geometry": pt,
                "centroid_lon": lon,
                "centroid_lat": lat,
                "geometry_wkb": pt.wkb,
                "tags": "{}",
            }
        )
    assets = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    osm_path = tmp / "osm.parquet"
    assets.to_parquet(osm_path, index=False)

    # One burn polygon overlapping the school's 75 m buffer, vintage 2018.
    burn_32629 = box(cx - 300 - 75, cy - 300 - 75, cx - 300, cy - 300 + 75)
    burns = gpd.GeoDataFrame(
        {"vintage_year": [2018], "geometry": [burn_32629]}, crs="EPSG:32629"
    ).to_crs("EPSG:4326")
    burns_path = tmp / "burns.parquet"
    burns.to_parquet(burns_path, index=False)

    aoi_path = tmp / "aoi.geojson"
    left, bottom, right, top = _aoi_bounds_4326()
    aoi_path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {},
                "geometry": mapping(box(left, bottom, right, top)),
            }
        )
    )

    fuel = tmp / "fuel_class_smoke_test.tif"
    gch = tmp / "gch.tif"
    burn_scar = tmp / "burn_scar_smoke_test.tif"
    _write_2band_fuel(fuel)
    _write_gch(gch)
    _write_burn_scar(burn_scar)
    return {
        "aoi": aoi_path,
        "osm": osm_path,
        "burns": burns_path,
        "fuel": fuel,
        "gch": gch,
        "burn_scar": burn_scar,
    }


def test_score_pipeline_end_to_end(tmp_path: Path) -> None:
    fx = _build_fixtures(tmp_path)
    slope_da, nbr_da = _injected_rasters()
    feats_out = tmp_path / "features.parquet"
    exp_out = tmp_path / "exposure.parquet"

    result = feat.run_scoring(
        aoi_path=fx["aoi"],
        taxonomy_path=TAXONOMY,
        exposure_config_path=CONFIG,
        crosswalk_sha=DUMMY_SHA,
        osm_parquet=fx["osm"],
        burns_parquet=fx["burns"],
        fuel_cog=fx["fuel"],
        gch_cog=fx["gch"],
        burn_scar_cog=fx["burn_scar"],
        # On/after the fixture COG's window end (2026-06-09): all features present.
        window_end=date(2026, 6, 9),
        run_id="test_smoke",
        code_commit_sha="deadbeef",
        features_out=feats_out,
        exposure_out=exp_out,
        slope_da=slope_da,
        nbr_delta_da=nbr_da,
    )

    assert result.n_assets == 4
    # In-window run: all six features present (incl. recent_burn_share_12mo).
    assert set(result.features_present_global) == {
        "fuel_class_severity_weight",
        "canopy_height_p90_m",
        "slope_max_deg",
        "historical_burn_share",
        "recent_burn_share_12mo",
        "nbr_delta_recent",
    }

    exp = gpd.read_parquet(exp_out)
    assert len(exp) == 4
    assert exp.crs is not None and exp.crs.to_epsg() == 4326
    assert bool(exp["exposure_score"].between(0.0, 1.0).all())
    assert sorted(exp["exposure_rank"]) == [1, 2, 3, 4]
    assert bool(exp["exposure_score"].notna().all())

    # Every row validates against the ScoredAsset schema (non-negotiable #3).
    for _, row in exp.iterrows():
        ScoredAsset.model_validate({k: v for k, v in row.items() if k != "geometry"})

    # Features parquet round-trips with the raw values.
    feats = gpd.read_parquet(feats_out)
    assert len(feats) == 4
    assert "fuel_class_severity_weight" in feats.columns


def test_backdated_run_drops_recent_burn_feature(tmp_path: Path) -> None:
    fx = _build_fixtures(tmp_path)
    slope_da, nbr_da = _injected_rasters()

    result = feat.run_scoring(
        aoi_path=fx["aoi"],
        taxonomy_path=TAXONOMY,
        exposure_config_path=CONFIG,
        crosswalk_sha=DUMMY_SHA,
        osm_parquet=fx["osm"],
        burns_parquet=fx["burns"],
        fuel_cog=fx["fuel"],
        gch_cog=fx["gch"],
        burn_scar_cog=fx["burn_scar"],
        window_end=date(2024, 12, 31),
        run_id="test_backdate",
        code_commit_sha="deadbeef",
        features_out=tmp_path / "f.parquet",
        exposure_out=tmp_path / "e.parquet",
        slope_da=slope_da,
        nbr_delta_da=nbr_da,
    )
    # Out-of-window: the fixed burn-scar COG cannot honour 2024 → feature absent.
    assert "recent_burn_share_12mo" not in result.features_present_global
    assert ScoredAsset.model_validate(result.sample_row).provenance.burn_scar_cog_sha is None
