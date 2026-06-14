"""Committed STAC catalog + geobrowser bundle consistency (WU-9, prompt 15).

Guards the publishing invariants: no committed STAC item may point its local
asset href at a gitignored path, the published GeoParquet must satisfy the
ScoredAsset contract, and every local artefact the site references must exist.
Runs in CI on the committed tree — no pipeline outputs required.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pystac

from wildfire_exposure_eo.schemas import GeobrowserStyleData, ScoredAsset

_ROOT = Path(__file__).resolve().parents[2]
_STAC = _ROOT / "stac"
_DOCS = _ROOT / "docs"


def _items() -> list[pystac.Item]:
    catalog = pystac.Catalog.from_file(str(_STAC / "catalog.json"))
    return list(catalog.get_items(recursive=True))


def test_local_asset_hrefs_resolve_to_committed_files() -> None:
    """Every non-HTTP asset href resolves inside the repo and exists on disk."""
    for item in _items():
        self_href = item.get_self_href()
        assert self_href is not None
        item_dir = Path(self_href).parent
        for name, asset in item.assets.items():
            if asset.href.startswith(("http://", "https://")):
                continue
            target = (item_dir / asset.href).resolve()
            assert target.is_file(), f"{item.id}:{name} → missing file {target}"
            assert target.is_relative_to(_ROOT), f"{item.id}:{name} escapes the repo"
            assert "outputs" not in target.parts, f"{item.id}:{name} gitignored: {asset.href}"


def test_remote_asset_hrefs_are_r2_urls() -> None:
    remote = [
        (item.id, asset.href)
        for item in _items()
        for asset in item.assets.values()
        if asset.href.startswith(("http://", "https://"))
    ]
    assert remote, "expected at least the burn-scar COG on R2"
    for item_id, href in remote:
        is_r2 = href.startswith("https://wildfire.cheias.pt/")
        assert is_r2, f"{item_id}: unexpected remote asset host: {href}"


def test_published_exposure_parquet_satisfies_scored_asset_contract() -> None:
    dirs = sorted((_STAC / "exposure-assets").glob("exposure-assets-*"))
    assert dirs, "exposure-assets collection has no published item"
    parquets = sorted(dirs[-1].glob("*.parquet"))
    assert len(parquets) == 1
    gdf = gpd.read_parquet(parquets[0])
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    ScoredAsset.model_validate(gdf.drop(columns="geometry").iloc[0].to_dict())
    item_json = json.loads(next(dirs[-1].glob("*.json")).read_text())
    assert item_json["properties"]["wildfire_exposure_eo:n_assets"] == len(gdf)


def test_geobrowser_bundle_is_complete() -> None:
    assert (_DOCS / ".nojekyll").exists()
    assert (_DOCS / "index.html").exists()
    assert (_DOCS / "app" / "app.js").exists()
    style = GeobrowserStyleData.model_validate_json(
        (_DOCS / "app" / "data" / "style_data.json").read_text()
    )
    for key, artifact in style.artifacts.items():
        if artifact.href.startswith(("http://", "https://")):
            continue
        assert (_DOCS / artifact.href).is_file(), f"site artefact missing: {key}"
    # Public-surface terminology guard (non-negotiable #6): the validated
    # headline must exist and the site bundle must carry the rank run id.
    assert style.validation.run_id
