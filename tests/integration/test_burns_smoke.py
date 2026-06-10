"""Integration test: fetch-burns --smoke with ICNF MapServer monkeypatched."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import geopandas as gpd
from typer.testing import CliRunner

from wildfire_exposure_eo import burns as burns_mod
from wildfire_exposure_eo.cli import app
from wildfire_exposure_eo.schemas import BurnPerimeter, BurnPerimeterProvenance

_SMOKE_AOI_SHA = "4041a084971e3642667cd25345f98819d9be5c109e2775a8c757fb9f64a1b377"
_FIXTURES = Path("tests/fixtures/icnf")

# Fixture years used in the smoke test (subset of the full 1975-2025 range)
_FIXTURE_YEARS = {
    2017: "layer_query_2017.json",
    2020: "layer_query_2020.json",
    2024: "layer_query_2024.json",
}


def _mock_response(body: dict | str, *, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=body if isinstance(body, dict) else json.loads(body))
    return resp


def _build_fake_get(
    index_data: dict,
    fixture_years: dict[int, str],
) -> object:
    """Return a side-effect function that routes GET calls to canned fixtures."""
    year_fixtures = {
        year: json.loads((_FIXTURES / fname).read_text()) for year, fname in fixture_years.items()
    }
    # Map layer_id → year for the index layers
    layer_id_to_year: dict[int, int] = {}
    for layer in index_data.get("layers", []):
        year = burns_mod._parse_layer_year(layer["name"])
        if year is not None:
            layer_id_to_year[layer["id"]] = year

    def _fake_get(url: str, *, params: dict | None = None, **kwargs: object) -> MagicMock:
        if "query" not in url:
            # MapServer index request
            return _mock_response(index_data)
        # Extract layer_id from URL: …/MapServer/{layer_id}/query
        parts = url.rstrip("/").split("/")
        try:
            layer_id = int(parts[-2])
        except (ValueError, IndexError):
            return _mock_response({"features": [], "exceededTransferLimit": False})

        # Count-only requests
        if params and str(params.get("returnCountOnly", "")).lower() == "true":
            year = layer_id_to_year.get(layer_id, 0)
            count = len(year_fixtures.get(year, {}).get("features", []))
            return _mock_response({"count": count})

        # Feature query: return fixture if available, else empty
        year = layer_id_to_year.get(layer_id, 0)
        fixture = year_fixtures.get(year, {"features": [], "exceededTransferLimit": False})
        return _mock_response(fixture)

    return _fake_get


def test_fetch_burns_smoke(tmp_path: Path) -> None:
    """fetch-burns --smoke produces a valid GeoParquet with rows from ≥3 distinct vintages."""
    out = tmp_path / "icnf_burns_smoke_test.parquet"

    # Use only the 3 fixture years so the test is self-contained
    index_data = {
        "layers": [
            {"id": 3, "name": "Áreas Ardidas 2017"},
            {"id": 0, "name": "Áreas Ardidas 2020"},
            {"id": 19, "name": "Áreas Ardidas 2024"},
        ]
    }
    fake_get = _build_fake_get(index_data, _FIXTURE_YEARS)

    with patch("wildfire_exposure_eo.burns._get_with_retry", side_effect=fake_get):
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["fetch-burns", "--smoke", "--out", str(out)],
        )

    assert result.exit_code == 0, f"CLI failed: {result.output}\n{result.exception}"
    assert out.exists(), "GeoParquet not written"

    # Round-trips through geopandas
    gdf = gpd.read_parquet(out)
    assert len(gdf) > 0

    # Vintage years match what the fixtures returned
    assert set(gdf["vintage_year"].unique()) == {2017, 2020, 2024}

    # CRS is EPSG:4326
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326

    # Every row Pydantic-validates as BurnPerimeter, and provenance is per-row:
    # vintage_year matches the row and icnf_layer_id points at the source layer
    expected_layer_by_year = {2017: 3, 2020: 0, 2024: 19}
    for _, row in gdf.iterrows():
        prov_dict = row["provenance"]
        prov = BurnPerimeterProvenance.model_validate(prov_dict)
        assert prov.vintage_year == int(row["vintage_year"])
        assert prov.icnf_layer_id == expected_layer_by_year[prov.vintage_year]
        BurnPerimeter.model_validate(
            {
                "row_id": str(row["row_id"]),
                "vintage_year": int(row["vintage_year"]),
                "icnf_feature_id": int(row["feature_id"]),
                "geometry_wkb": bytes(row["geometry_wkb"]),
                "area_ha": float(row["area_ha"]),
                "provenance": prov,
            }
        )

    # aoi_geometry_sha matches the smoke AOI's actual SHA
    actual_sha = gdf.iloc[0]["provenance"]["aoi_geometry_sha"]
    assert actual_sha == _SMOKE_AOI_SHA, f"aoi_geometry_sha: {actual_sha!r} != {_SMOKE_AOI_SHA!r}"
