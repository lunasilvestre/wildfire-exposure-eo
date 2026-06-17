"""Unit + schema tests for the WU-9 geobrowser data bundle (prompt 15)."""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point

from wildfire_exposure_eo.schemas import (
    ExposureFeatureProperties,
    FuelLegendEntry,
    FwiOverlay,
    FwiOverlayComponent,
    GeobrowserArtifact,
    GeobrowserStyleData,
    ValidationHeadline,
)

# Repo-root import shim (mirrors the script itself)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import importlib

geobrowser_mod = importlib.import_module("15_make_geobrowser_data")

_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


def _headline(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": "20260611T170549Z",
        "n_assets": 3045,
        "n_burned": 5,
        "base_rate": 0.0016420361247947454,
        "degenerate": False,
        "top_decile_lift": 0.0,
        "cumulative_lift_top30pct": 2.66,
        "spearman_rho": 0.0371,
        "spearman_p": 0.0409,
        "ablation_top_decile_lift": 2.0,
        "window_end": "2024-12-31",
        "validation_years": [2025],
    }
    base.update(overrides)
    return base


def test_validation_headline_parses_full() -> None:
    v = ValidationHeadline.model_validate(_headline())
    assert v.cumulative_lift_top30pct == 2.66


def test_validation_headline_degenerate_allows_missing_lift() -> None:
    v = ValidationHeadline.model_validate(
        {
            "run_id": "20260612T061227Z",
            "n_assets": 14,
            "n_burned": 0,
            "base_rate": 0.0,
            "degenerate": True,
            "window_end": "2026-06-09",
            "validation_years": [],
        }
    )
    assert v.top_decile_lift is None


def test_exposure_feature_properties_rejects_score_above_one() -> None:
    props = {
        "asset_id": "osm:node/1",
        "osm_type": "node",
        "osm_id": 1,
        "asset_class": "education.school",
        "criticality_weight": 0.8,
        "exposure_score": 1.2,
        "exposure_rank": 1,
    }
    with pytest.raises(ValidationError):
        ExposureFeatureProperties.model_validate(props)


def test_style_data_round_trip() -> None:
    lut = [(0, 0, 0)] * 256
    style = GeobrowserStyleData(
        generated_by="scripts/15_make_geobrowser_data.py at deadbeef",
        code_commit_sha="deadbeef",
        viridis_lut=lut,
        ylorrd_lut=lut,
        fuel_legend=[FuelLegendEntry(code=0, label="Non-fuel (0)", color=(204, 204, 204))],
        validation=ValidationHeadline.model_validate(_headline()),
        artifacts={
            "aoi": GeobrowserArtifact(
                href="app/data/aoi.geojson",
                crs="EPSG:4326",
                run_id="frozen",
                role="authoritative",
                description="AOI boundary",
            )
        },
    )
    parsed = GeobrowserStyleData.model_validate_json(style.model_dump_json())
    assert parsed.artifacts["aoi"].crs == "EPSG:4326"


def test_style_data_rejects_short_lut() -> None:
    with pytest.raises(ValidationError):
        GeobrowserStyleData(
            generated_by="x",
            code_commit_sha="x",
            viridis_lut=[(0, 0, 0)] * 16,
            ylorrd_lut=[(0, 0, 0)] * 256,
            fuel_legend=[],
            validation=ValidationHeadline.model_validate(_headline()),
            artifacts={},
        )


# --------------------------------------------------------------------------- #
# FWI operational overlay schema
# --------------------------------------------------------------------------- #


def _fwi_component(**overrides: object) -> FwiOverlayComponent:
    base: dict[str, object] = {
        "component": "fwi",
        "label": "Fire Weather Index (FWI)",
        "href": "https://wildfire.cheias.pt/fwi_fwi_3857_2026-06-11.tif",
        "crs": "EPSG:3857",
        "value_min": 6.67,
        "value_max": 51.36,
    }
    base.update(overrides)
    return FwiOverlayComponent.model_validate(base)


def test_fwi_overlay_component_rejects_inverted_range() -> None:
    with pytest.raises(ValidationError):
        _fwi_component(value_min=50.0, value_max=10.0)


def test_fwi_overlay_round_trip_in_style_data() -> None:
    lut = [(0, 0, 0)] * 256
    overlay = FwiOverlay(
        valid_date="2026-06-11",
        lag_note="~2-day lag",
        attribution="Source: CEMS Early Warning Data Store — Copernicus / ECMWF (CC-BY-4.0)",
        components=[
            _fwi_component(),
            _fwi_component(component="bui", label="Build-Up Index (BUI)"),
        ],
    )
    style = GeobrowserStyleData(
        generated_by="x",
        code_commit_sha="x",
        viridis_lut=lut,
        ylorrd_lut=lut,
        fuel_legend=[],
        validation=ValidationHeadline.model_validate(_headline()),
        artifacts={},
        fwi_overlay=overlay,
    )
    parsed = GeobrowserStyleData.model_validate_json(style.model_dump_json())
    assert parsed.fwi_overlay is not None
    assert parsed.fwi_overlay.valid_date == "2026-06-11"
    assert [c.component for c in parsed.fwi_overlay.components] == ["fwi", "bui"]


def test_style_data_fwi_overlay_defaults_none() -> None:
    """Bundles built before the EWDS pull omit the overlay; the field is optional."""
    lut = [(0, 0, 0)] * 256
    style = GeobrowserStyleData(
        generated_by="x",
        code_commit_sha="x",
        viridis_lut=lut,
        ylorrd_lut=lut,
        fuel_legend=[],
        validation=ValidationHeadline.model_validate(_headline()),
        artifacts={},
    )
    assert style.fwi_overlay is None


def test_build_fwi_overlay_returns_none_when_manifest_absent(tmp_path: Path) -> None:
    missing = tmp_path / "no_manifest.json"
    assert geobrowser_mod.build_fwi_overlay(missing, "https://wildfire.cheias.pt") is None


def test_build_fwi_overlay_reads_manifest(tmp_path: Path) -> None:
    import json

    manifest = {
        "fwi_valid_date": "2026-06-11",
        "display_crs": "EPSG:3857",
        "components": [
            {
                "component": "fwi",
                "filename": "fwi_fwi_3857_2026-06-11.tif",
                "value_min": 6.67,
                "value_max": 51.36,
            },
            {
                "component": "ffmc",
                "filename": "fwi_ffmc_3857_2026-06-11.tif",
                "value_min": 80.45,
                "value_max": 96.78,
            },
        ],
    }
    mp = tmp_path / "fwi_overlay_manifest.json"
    mp.write_text(json.dumps(manifest))
    overlay = geobrowser_mod.build_fwi_overlay(mp, "https://wildfire.cheias.pt")
    assert overlay is not None
    assert overlay.valid_date == "2026-06-11"
    assert overlay.components[0].href == "https://wildfire.cheias.pt/fwi_fwi_3857_2026-06-11.tif"
    assert overlay.components[0].crs == "EPSG:3857"
    assert overlay.components[1].label == "Fine Fuel Moisture Code (FFMC)"
    # Attribution is read from config/fire_weather.yaml (no invented identifiers).
    assert "CEMS" in overlay.attribution


# --------------------------------------------------------------------------- #
# Script helpers
# --------------------------------------------------------------------------- #


def test_lut_samples_256_rgb_triples() -> None:
    lut = geobrowser_mod._lut("viridis")
    assert len(lut) == 256
    assert all(len(c) == 3 and all(0 <= v <= 255 for v in c) for c in lut)
    # Viridis orientation: index 0 is dark (purple), index 255 bright (yellow).
    assert sum(lut[0]) < sum(lut[255])


def test_export_exposure_geojson_requires_explicit_crs(tmp_path: Path) -> None:
    gdf = gpd.GeoDataFrame({"a": [1]}, geometry=[Point(0, 0)])  # no CRS set
    src = tmp_path / "exposure_x.parquet"
    gdf.to_parquet(src)
    with pytest.raises(ValueError, match="EPSG:4326"):
        geobrowser_mod.export_exposure_geojson(src, tmp_path / "out.geojson")


def test_validation_headline_from_degenerate_metrics() -> None:
    metrics = {
        "full": {"n": 14, "n_burned": 0, "base_rate": 0.0, "degenerate": True},
        "ablation": {"n": 14, "n_burned": 0, "base_rate": 0.0, "degenerate": True},
        "window_end": "2026-06-09",
        "validation_years": [],
    }
    v = geobrowser_mod.validation_headline("rid", metrics)
    assert v.degenerate is True
    assert v.spearman_rho is None
