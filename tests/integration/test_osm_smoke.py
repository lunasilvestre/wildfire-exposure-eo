"""Integration test: fetch-osm --smoke with Overpass monkeypatched."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
from typer.testing import CliRunner

from wildfire_exposure_eo import osm as osm_mod
from wildfire_exposure_eo.cli import app
from wildfire_exposure_eo.schemas import OsmAsset, OsmAssetProvenance

_SMOKE_AOI_SHA = "4041a084971e3642667cd25345f98819d9be5c109e2775a8c757fb9f64a1b377"
_FIXTURE_PATH = Path("tests/fixtures/overpass/smoke_multi_class.json")


def _make_overpass_result(elements: list[dict]) -> osm_mod.OverpassResult:  # type: ignore[type-arg]
    return osm_mod.OverpassResult(
        elements=elements,
        osm_snapshot_iso=datetime(2026, 6, 1, tzinfo=UTC),
        endpoint_used="https://overpass-api.de/api/interpreter",
        query_sha="a" * 64,
    )


def _stub_query_overpass(
    query: str,
    *,
    endpoint: str = osm_mod._DEFAULT_ENDPOINT,
    timeout_s: int = 60,
    retries: int = 2,
    fallback_endpoint: str | None = osm_mod._FALLBACK_ENDPOINT,
) -> osm_mod.OverpassResult:
    """Return the smoke multi-class fixture for every class query."""
    elements = json.loads(_FIXTURE_PATH.read_text())["elements"]
    return _make_overpass_result(elements)


def test_fetch_osm_smoke(tmp_path: Path) -> None:
    """fetch-osm --smoke produces a valid GeoParquet with ≥3 distinct asset classes."""
    out = tmp_path / "osm_assets_smoke_test.parquet"

    with patch("wildfire_exposure_eo.osm.query_overpass", side_effect=_stub_query_overpass):
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "fetch-osm",
                "--smoke",
                "--out",
                str(out),
            ],
        )

    assert result.exit_code == 0, f"CLI failed: {result.output}\n{result.exception}"

    # Output file exists
    assert out.exists(), "GeoParquet not written"

    # Round-trips through geopandas
    gdf = gpd.read_parquet(out)
    assert len(gdf) > 0

    # At least 3 distinct asset_class values
    distinct_classes = gdf["asset_class"].nunique()
    assert distinct_classes >= 3, (
        f"Expected ≥3 distinct asset classes, got {distinct_classes}: "
        f"{gdf['asset_class'].unique().tolist()}"
    )

    # CRS is EPSG:4326
    assert gdf.crs is not None
    assert gdf.crs.to_epsg() == 4326

    # Every row validates as OsmAsset
    for _, row in gdf.iterrows():
        prov_dict = row["provenance"]
        prov = OsmAssetProvenance.model_validate(prov_dict)
        OsmAsset.model_validate(
            {
                "asset_id": row["asset_id"],
                "osm_type": row["osm_type"],
                "osm_id": int(row["osm_id"]),
                "asset_class": row["asset_class"],
                "geometry_wkb": bytes(row["geometry_wkb"]),
                "centroid_lon": float(row["centroid_lon"]),
                "centroid_lat": float(row["centroid_lat"]),
                "tags": json.loads(str(row["tags"])),
                "provenance": prov,
            }
        )

    # aoi_geometry_sha matches the smoke AOI's actual SHA
    # (single-line assert: the pre-commit ruff (v0.7.4) and the uv-resolved ruff
    # disagree on multi-line assert formatting — see WU-2 review note)
    actual_sha = gdf.iloc[0]["provenance"]["aoi_geometry_sha"]
    assert actual_sha == _SMOKE_AOI_SHA, f"aoi_geometry_sha: {actual_sha!r} != {_SMOKE_AOI_SHA!r}"
