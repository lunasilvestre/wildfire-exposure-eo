"""Unit + schema tests for the WU-9 geobrowser data bundle (prompt 15)."""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point

from wildfire_exposure_eo.schemas import (
    BurnHistoryLayer,
    BurnHistorySourceStyle,
    ExposureFeatureProperties,
    FirescopeLayer,
    FuelLegendEntry,
    FwiOverlay,
    FwiOverlayComponent,
    GeobrowserArtifact,
    GeobrowserStyleData,
    InputRampSpec,
    InputRasterLayer,
    ProvenanceSummary,
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


def test_exposure_feature_properties_accepts_impact_severity() -> None:
    # The merged full-extent (Iberia) layer carries the cross-AOI impact_severity.
    p = ExposureFeatureProperties.model_validate(_exposure_props(impact_severity=0.42))
    assert p.impact_severity == 0.42


def test_exposure_feature_properties_impact_severity_optional() -> None:
    # Per-AOI display copies omit it (the analyser derives score × weight client
    # side); only the merged Iberia layer bakes in the normalised value.
    props = _exposure_props()
    assert "impact_severity" not in props
    p = ExposureFeatureProperties.model_validate(props)
    assert p.impact_severity is None


def test_exposure_feature_properties_rejects_impact_severity_above_one() -> None:
    with pytest.raises(ValidationError):
        ExposureFeatureProperties.model_validate(_exposure_props(impact_severity=1.2))


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
# Per-AOI model-INPUT raster layers
# --------------------------------------------------------------------------- #


def _input_layer(**overrides: object) -> InputRasterLayer:
    base: dict[str, object] = {
        "kind": "canopy_height",
        "href": "https://wildfire.cheias.pt/canopy_height_pilot_3857_20260617T222204Z.tif",
        "crs": "EPSG:3857",
        "run_id": "20260617T222204Z",
    }
    base.update(overrides)
    return InputRasterLayer.model_validate(base)


def test_input_raster_layer_requires_known_kind() -> None:
    # The kind drives the legend/ramp lookup; an unknown token is a bug.
    with pytest.raises(ValidationError):
        _input_layer(kind="elevation")


def test_input_raster_layer_carries_explicit_crs() -> None:
    layer = _input_layer(kind="slope")
    assert layer.crs == "EPSG:3857"
    assert layer.kind == "slope"


def test_input_ramp_spec_continuous_carries_lut_and_range() -> None:
    ramp = InputRampSpec(
        kind="canopy_height",
        label="Canopy height",
        unit="m",
        cmap="YlGn",
        lut=[(0, 0, 0)] * 256,
        value_min=0.0,
        value_max=25.0,
        caption="relative input, not a probability",
    )
    assert ramp.value_min == 0.0
    assert ramp.value_max == 25.0
    assert ramp.lut is not None and len(ramp.lut) == 256


def test_input_ramp_spec_fuel_omits_continuous_ramp() -> None:
    # The categorical fuel kind reuses the existing fuel_legend → no LUT/range.
    ramp = InputRampSpec(
        kind="fuel_class",
        label="Fuel NFFL class",
        cmap="tab10",
        caption="categorical fuel input",
    )
    assert ramp.lut is None
    assert ramp.value_min is None and ramp.value_max is None


def test_study_area_carries_input_layers() -> None:
    sa = _study_area(
        input_layers=[
            _input_layer(
                kind="fuel_class",
                href="https://wildfire.cheias.pt/fuel_class_monchique_3857_20260617T222932Z.tif",
                run_id="20260617T222932Z",
            ),
        ],
    )
    assert len(sa.input_layers) == 1
    assert sa.input_layers[0].kind == "fuel_class"


def test_study_area_input_layers_default_empty() -> None:
    # A study area published before the input-layer wiring carries none.
    assert _study_area().input_layers == []


def test_input_layers_round_trip_in_style_data() -> None:
    lut = [(0, 0, 0)] * 256
    style = GeobrowserStyleData(
        generated_by="x",
        code_commit_sha="x",
        viridis_lut=lut,
        ylorrd_lut=lut,
        fuel_legend=[],
        validation=ValidationHeadline.model_validate(_headline()),
        artifacts={},
        pilot_input_layers=[_input_layer(kind="canopy_height"), _input_layer(kind="slope")],
        input_ramps=geobrowser_mod.build_input_ramps(),
        study_areas=[_study_area(input_layers=[_input_layer(kind="nbr_delta")])],
    )
    parsed = GeobrowserStyleData.model_validate_json(style.model_dump_json())
    assert [layer.kind for layer in parsed.pilot_input_layers] == ["canopy_height", "slope"]
    assert parsed.study_areas[0].input_layers[0].kind == "nbr_delta"
    assert {r.kind for r in parsed.input_ramps} == {"canopy_height", "slope", "nbr_delta"}


def test_build_input_ramps_continuous_only_with_measured_ranges() -> None:
    # The helper emits the three continuous kinds (fuel is categorical, excluded),
    # each with a 256-step LUT and a non-degenerate measured display range.
    ramps = geobrowser_mod.build_input_ramps()
    assert {r.kind for r in ramps} == {"canopy_height", "slope", "nbr_delta"}
    for ramp in ramps:
        assert ramp.lut is not None and len(ramp.lut) == 256
        assert ramp.value_min is not None
        assert ramp.value_max is not None
        assert ramp.value_max > ramp.value_min


def test_style_data_input_layers_default_empty() -> None:
    """Bundles built before the input-layer wiring omit them; defaults are []."""
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
    assert style.pilot_input_layers == []
    assert style.input_ramps == []


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


def _scored_parquet(tmp_path: Path, name: str, rows: list[dict[str, object]]) -> Path:
    """Write a ScoredAsset-valid scored GeoParquet (EPSG:4326) for merged-export.

    Each ``rows`` entry overrides ``exposure_score`` / ``criticality_weight`` on
    a complete, schema-valid base row (features + provenance serialised as JSON
    strings, exactly as the real ``exposure_<run>.parquet`` stores them).
    """
    import json as _json

    sha = "a" * 64
    prov = {
        "model_version": "0.3.0",
        "config_sha": sha,
        "crosswalk_sha": sha,
        "run_id": "x",
        "code_commit_sha": "deadbeef",
        "aoi_path": "data/aoi/smoke.geojson",
        "aoi_geometry_sha": sha,
        "window_start": "2025-06-16",
        "window_end": "2026-06-16",
        "osm_parquet_sha": sha,
        "burns_parquet_sha": sha,
        "fuel_cog_sha": sha,
        "gch_cache_sha": sha,
        "burn_share_threshold": 0.5,
    }
    full_rows = []
    geoms = []
    for i, r in enumerate(rows):
        pt = Point(-8.0 + i * 0.01, 40.0 + i * 0.01)
        geoms.append(pt)
        base: dict[str, object] = {
            "asset_id": f"osm:node/{i + 1}",
            "osm_type": "node",
            "osm_id": i + 1,
            "asset_class": "power.substation",
            "criticality_weight": 1.0,
            "centroid_lon": pt.x,
            "centroid_lat": pt.y,
            "geometry_wkb": pt.wkb,
            "features": _json.dumps({"historical_burn_share": 0.0}),
            "features_present": ["historical_burn_share"],
            "exposure_score": 0.5,
            "exposure_rank": i + 1,
            "provenance": _json.dumps(prov),
        }
        base.update(r)
        full_rows.append(base)
    gdf = gpd.GeoDataFrame(full_rows, geometry=geoms, crs="EPSG:4326")
    dst = tmp_path / f"exposure_{name}.parquet"
    gdf.to_parquet(dst)
    return dst


def test_pooled_impact_severity_max_is_global(tmp_path: Path) -> None:
    # Two AOIs; the pool's max severity (score × weight) is the global maximum.
    a = _scored_parquet(
        tmp_path,
        "a",
        [{"exposure_score": 0.8, "criticality_weight": 0.5}],  # 0.40
    )
    b = _scored_parquet(
        tmp_path,
        "b",
        [{"exposure_score": 0.9, "criticality_weight": 0.9}],  # 0.81
    )
    gmax = geobrowser_mod.pooled_impact_severity_max([a, b])
    assert abs(gmax - 0.81) < 1e-9


def test_pooled_impact_severity_max_rejects_degenerate(tmp_path: Path) -> None:
    z = _scored_parquet(tmp_path, "z", [{"exposure_score": 0.0, "criticality_weight": 1.0}])
    with pytest.raises(ValueError, match="pooled impact-severity"):
        geobrowser_mod.pooled_impact_severity_max([z])


def test_export_merged_iberia_normalises_against_global_max(tmp_path: Path) -> None:
    import json as _json

    # pilot raw severity 1.0 × 0.8 = 0.80 (the global max); sa raw 0.5 × 0.8 = 0.40.
    pilot = _scored_parquet(tmp_path, "pilot", [{"exposure_score": 1.0, "criticality_weight": 0.8}])
    sa = _scored_parquet(tmp_path, "sa", [{"exposure_score": 0.5, "criticality_weight": 0.8}])
    out = tmp_path / "merged.geojson"
    n = geobrowser_mod.export_merged_iberia_geojson(
        [("pilot", pilot), ("monchique", sa)],
        out,
        global_sev_max=0.80,
        coord_precision=6,
    )
    assert n == 2
    feats = {
        f["properties"]["aoi_name"]: f["properties"]
        for f in _json.loads(out.read_text())["features"]
    }
    # Global max row normalises to 1.0; the half-score row to 0.40/0.80 = 0.5.
    assert abs(feats["pilot"]["impact_severity"] - 1.0) < 1e-9
    assert abs(feats["monchique"]["impact_severity"] - 0.5) < 1e-9
    # aoi_name is carried; the per-AOI rank is preserved verbatim.
    assert feats["pilot"]["aoi_name"] == "pilot"
    assert feats["monchique"]["exposure_rank"] == 1


def test_export_merged_iberia_requires_explicit_crs(tmp_path: Path) -> None:
    gdf = gpd.GeoDataFrame(
        {
            "asset_id": ["osm:node/1"],
            "osm_type": ["node"],
            "osm_id": [1],
            "asset_class": ["power.substation"],
            "criticality_weight": [1.0],
            "exposure_score": [0.5],
            "exposure_rank": [1],
        },
        geometry=[Point(0, 0)],
    )  # no CRS
    src = tmp_path / "exposure_nocrs.parquet"
    gdf.to_parquet(src)
    with pytest.raises(ValueError, match="EPSG:4326"):
        geobrowser_mod.export_merged_iberia_geojson(
            [("pilot", src)], tmp_path / "m.geojson", global_sev_max=1.0, coord_precision=6
        )


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


# --------------------------------------------------------------------------- #
# Thematic Iberia layers (firescope / burn-history / provenance summary)
# --------------------------------------------------------------------------- #


def test_firescope_layer_round_trips_in_style_data() -> None:
    lut = [(0, 0, 0)] * 256
    fs = FirescopeLayer(
        href="https://wildfire.cheias.pt/firescope_iberia_3857_20260618T122124Z.tif",
        crs="EPSG:3857",
        run_id="20260618T122124Z",
        value_min=0.0,
        value_max=254.0,
        cmap="magma",
        lut=lut,
        attribution="FireScope (CC-BY-4.0) — INSAIT-Institute + ETH, arXiv:2511.17171",
        caption="Relative wildfire-risk RANK (SOTA reference), not a probability.",
    )
    style = GeobrowserStyleData(
        generated_by="x",
        code_commit_sha="x",
        viridis_lut=lut,
        ylorrd_lut=lut,
        fuel_legend=[],
        validation=ValidationHeadline.model_validate(_headline()),
        artifacts={},
        firescope=fs,
    )
    parsed = GeobrowserStyleData.model_validate_json(style.model_dump_json())
    assert parsed.firescope is not None
    assert parsed.firescope.value_max == 254.0
    assert "arXiv:2511.17171" in parsed.firescope.attribution


def test_burn_history_layer_round_trips_by_source() -> None:
    bh = BurnHistoryLayer(
        href="https://wildfire.cheias.pt/iberia_burn_history_20260618T131535Z.geojson",
        crs="EPSG:4326",
        run_id="20260618T131535Z",
        sources=[
            BurnHistorySourceStyle(
                source="ICNF",
                label="ICNF (Portugal — fine, 1990–2025)",
                color=(179, 0, 0),
                vintage_min=1990,
                vintage_max=2025,
                n_perimeters=25117,
            ),
            BurnHistorySourceStyle(
                source="EFFIS",
                label="EFFIS (Spain — coarse, 2016–2025)",
                color=(230, 159, 0),
                vintage_min=2016,
                vintage_max=2025,
                n_perimeters=10284,
            ),
        ],
        caption="Observed history, not a forecast; PT/ES temporal+resolution asymmetry.",
    )
    parsed = BurnHistoryLayer.model_validate_json(bh.model_dump_json())
    assert [s.source for s in parsed.sources] == ["ICNF", "EFFIS"]
    assert parsed.sources[0].vintage_min == 1990
    assert parsed.sources[1].vintage_min == 2016


def test_burn_history_source_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        BurnHistorySourceStyle(
            source="NASA",  # type: ignore[arg-type]
            label="x",
            color=(1, 2, 3),
            vintage_min=2000,
            vintage_max=2020,
            n_perimeters=1,
        )


def test_provenance_summary_truncates_commit_sha() -> None:
    ps = ProvenanceSummary(
        run_id="20260617T035233Z",
        model_version="0.3.1",
        code_commit_sha="71681fe0508ce459728b9deb8232d8f80fa8c26b",
        window_start="2023-12-31",
        window_end="2024-12-31",
        validation_years=[2025],
        s2_item_count=56,
        fwi_valid_date="2026-06-12",
    )
    assert len(ps.code_commit_sha) == 40
    assert ps.s2_item_count == 56


def test_provenance_summary_fwi_date_optional() -> None:
    ps = ProvenanceSummary(
        run_id="x",
        model_version="0.3.1",
        code_commit_sha="deadbeef",
        window_start="2023-12-31",
        window_end="2024-12-31",
        validation_years=[2025],
        s2_item_count=0,
    )
    assert ps.fwi_valid_date is None


def test_style_data_thematic_fields_default_none_or_empty() -> None:
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
    assert style.iberia_inputs == []
    assert style.firescope is None
    assert style.burn_history is None
    assert style.provenance_summary is None


def test_build_iberia_inputs_uses_published_filenames() -> None:
    layers = geobrowser_mod.build_iberia_inputs("https://wildfire.cheias.pt")
    kinds = {layer.kind for layer in layers}
    assert kinds == {"fuel_class", "slope", "canopy_height"}
    for layer in layers:
        assert layer.crs == "EPSG:3857"
        assert layer.href.startswith("https://wildfire.cheias.pt/")
        # run-id parsed from the published filename (canonical YYYYMMDDThhmmssZ)
        assert geobrowser_mod._RUN_ID_RE.match(layer.run_id)


def test_build_firescope_carries_attribution_and_range() -> None:
    fs = geobrowser_mod.build_firescope("https://wildfire.cheias.pt")
    assert fs.value_min == 0.0
    assert fs.value_max == 254.0
    assert fs.cmap == "magma"
    assert len(fs.lut) == 256
    assert "arXiv:2511.17171" in fs.attribution


def test_build_full_nffl_fuel_legend_covers_all_codes() -> None:
    legend = geobrowser_mod.full_nffl_fuel_legend()
    codes = {e.code for e in legend}
    # Non-fuel 0 plus every NFFL code in the crosswalk (1..13).
    assert 0 in codes
    assert {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}.issubset(codes)


def test_build_mosaics_tile_counts() -> None:
    # Asymmetric by design: NBR-delta spans the pilot + 4 study areas (5 tiles),
    # but the burn-scar mosaic serves only the 4 DE-GRIDDED study areas — the
    # pilot's burn-scar tile is WITHHELD (residual ViT-tiling artifact its dense
    # scene coverage leaves). NBR tiles reuse the study-area nbr_delta hrefs.
    sa = [
        _study_area(
            name=name,
            input_layers=[
                _input_layer(
                    kind="nbr_delta",
                    href=f"https://wildfire.cheias.pt/nbr_delta_{name}_3857_20260101T000000Z.tif",
                    run_id="20260101T000000Z",
                )
            ],
        )
        for name in ("pedrogao_grande", "serra_da_estrela", "peneda_geres", "monchique")
    ]
    mosaics = geobrowser_mod.build_mosaics("https://wildfire.cheias.pt", [], sa)
    by_kind = {mo.kind: mo for mo in mosaics}
    assert set(by_kind) == {"burn_scar", "nbr_delta"}
    # Burn-scar: 3 de-gridded study-area tiles, NO pilot and NO monchique.
    # Monchique was withheld (2026-06-23 validation vs ICNF: a diffuse wash with no
    # coherent recent footprint even at score>=0.60). See scripts/32.
    assert len(by_kind["burn_scar"].tiles) == 3
    assert {t.aoi_name for t in by_kind["burn_scar"].tiles} == {
        "pedrogao_grande",
        "serra_da_estrela",
        "peneda_geres",
    }
    assert all("degrid" in t.href for t in by_kind["burn_scar"].tiles)
    # NBR-delta: pilot + 4 study areas (5 tiles), pilot first.
    assert len(by_kind["nbr_delta"].tiles) == 5
    assert by_kind["nbr_delta"].tiles[0].aoi_name == "pilot"
    # Every tile carries an explicit CRS (#2) and a canonical run-id.
    for mo in mosaics:
        for tile in mo.tiles:
            assert tile.crs == "EPSG:3857"
            assert geobrowser_mod._RUN_ID_RE.match(tile.run_id)
