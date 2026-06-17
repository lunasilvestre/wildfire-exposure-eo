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
    StudyAreaLayer,
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


def _exposure_props(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "asset_id": "osm:way/1497200647",
        "osm_type": "way",
        "osm_id": 1497200647,
        "asset_class": "power.transmission_line",
        "criticality_weight": 1.0,
        "exposure_score": 0.864,
        "exposure_rank": 15,
        "historical_burn_share": 0.5,
    }
    base.update(overrides)
    return base


def test_exposure_feature_properties_accepts_historical_burn_share() -> None:
    p = ExposureFeatureProperties.model_validate(_exposure_props())
    assert p.historical_burn_share == 0.5


def test_exposure_feature_properties_historical_burn_share_optional() -> None:
    # Display copies exported before the column existed (the study-area GeoJSONs)
    # omit it; the analyser table then shows no burned dot for those rows.
    props = _exposure_props()
    del props["historical_burn_share"]
    p = ExposureFeatureProperties.model_validate(props)
    assert p.historical_burn_share is None


def test_exposure_feature_properties_rejects_burn_share_above_one() -> None:
    with pytest.raises(ValidationError):
        ExposureFeatureProperties.model_validate(_exposure_props(historical_burn_share=1.2))


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
# Study-area (Wave-2 validation AOI) schema
# --------------------------------------------------------------------------- #


def _study_area(**overrides: object) -> StudyAreaLayer:
    base: dict[str, object] = {
        "name": "monchique",
        "label": "Monchique",
        "exposure_href": "https://wildfire.cheias.pt/exposure_monchique_20260616T185021Z.geojson",
        "exposure_crs": "EPSG:4326",
        "outline_href": "app/data/aoi_monchique.geojson",
        "outline_crs": "EPSG:4326",
        "run_id": "20260616T185021Z",
        "model_version": "0.3.0",
        "n_assets": 4178,
        "committed": False,
        "bbox_4326": (-8.75, 37.1, -8.3, 37.5),
    }
    base.update(overrides)
    return StudyAreaLayer.model_validate(base)


def test_study_area_keeps_model_version_verbatim() -> None:
    # The honesty bar: a v0.3.0 study area must not be relabelled to the pilot's
    # v0.3.1 — the schema simply carries whatever the parquet provenance held.
    sa = _study_area(model_version="0.3.0")
    assert sa.model_version == "0.3.0"


def test_study_area_rejects_empty_model_version() -> None:
    with pytest.raises(ValidationError):
        _study_area(model_version="")


def test_study_area_icnf_fields_default_none() -> None:
    # A study area without a published per-AOI ICNF overlay carries no ICNF
    # fields (the geobrowser then simply omits its burns layer).
    sa = _study_area()
    assert sa.icnf_href is None
    assert sa.icnf_crs is None
    assert sa.icnf_n_perimeters is None


def test_study_area_carries_icnf_overlay() -> None:
    # When the per-AOI ICNF burns are published the layer carries the R2 href,
    # an explicit CRS (#2), and the perimeter count for the caption.
    sa = _study_area(
        icnf_href="https://wildfire.cheias.pt/icnf_burns_monchique_20260617T214454Z.geojson",
        icnf_crs="EPSG:4326",
        icnf_n_perimeters=231,
    )
    assert sa.icnf_href is not None
    assert sa.icnf_href.endswith("icnf_burns_monchique_20260617T214454Z.geojson")
    assert sa.icnf_crs == "EPSG:4326"
    assert sa.icnf_n_perimeters == 231


def test_study_area_rejects_negative_icnf_count() -> None:
    with pytest.raises(ValidationError):
        _study_area(icnf_n_perimeters=-1)


def test_study_areas_round_trip_in_style_data() -> None:
    lut = [(0, 0, 0)] * 256
    style = GeobrowserStyleData(
        generated_by="x",
        code_commit_sha="x",
        viridis_lut=lut,
        ylorrd_lut=lut,
        fuel_legend=[],
        validation=ValidationHeadline.model_validate(_headline()),
        artifacts={},
        study_areas=[
            _study_area(),
            _study_area(name="peneda_geres", label="Peneda-Gerês", committed=True, n_assets=1920),
        ],
    )
    parsed = GeobrowserStyleData.model_validate_json(style.model_dump_json())
    assert [s.name for s in parsed.study_areas] == ["monchique", "peneda_geres"]
    assert parsed.study_areas[1].committed is True
    assert parsed.study_areas[0].model_version == "0.3.0"


def test_style_data_study_areas_default_empty() -> None:
    """Bundles built before the Wave-2 wiring omit study areas; default is []."""
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
    assert style.study_areas == []


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


def test_with_historical_burn_share_lifts_nested_feature() -> None:
    import json as _json
    import math

    gdf = gpd.GeoDataFrame(
        {
            "features": [
                _json.dumps({"slope_max_deg": 13.9, "historical_burn_share": 1.0}),
                _json.dumps({"slope_max_deg": 4.0, "historical_burn_share": 0.0}),
                _json.dumps({"slope_max_deg": 4.0}),  # key absent -> NaN -> JSON null
            ]
        },
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:4326",
    )
    out = geobrowser_mod._with_historical_burn_share(gdf)
    vals = list(out["historical_burn_share"])
    assert vals[0] == 1.0
    assert vals[1] == 0.0
    assert math.isnan(vals[2])  # geopandas writes NaN as JSON null on export


def test_with_historical_burn_share_absent_column_yields_nan() -> None:
    import math

    gdf = gpd.GeoDataFrame({"a": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    out = geobrowser_mod._with_historical_burn_share(gdf)
    assert math.isnan(out["historical_burn_share"].iloc[0])


def test_with_historical_burn_share_writes_json_null(tmp_path: Path) -> None:
    """A NaN burn share serialises to JSON null on GeoJSON export (never a bare
    NaN token), and present values stay numeric — what the analyser filters on."""
    import json as _json

    gdf = gpd.GeoDataFrame(
        {
            "asset_id": ["osm:way/1", "osm:node/2"],
            "features": [
                _json.dumps({"historical_burn_share": 1.0}),
                _json.dumps({"slope_max_deg": 2.0}),  # no burn share -> null
            ],
        },
        geometry=[Point(0, 0), Point(1, 1)],
        crs="EPSG:4326",
    )
    out = geobrowser_mod._with_historical_burn_share(gdf)
    dst = tmp_path / "out.geojson"
    out.to_file(dst, driver="GeoJSON")
    text = dst.read_text()
    assert "NaN" not in text  # geopandas serialises NaN as JSON null, not a NaN token
    feats = {f["properties"]["asset_id"]: f["properties"] for f in _json.loads(text)["features"]}
    assert feats["osm:way/1"]["historical_burn_share"] == 1.0
    assert feats["osm:node/2"]["historical_burn_share"] is None


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
