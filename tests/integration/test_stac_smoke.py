"""Integration smoke test for the `resolve-stac` CLI.

Drives the full Typer entry point against `data/aoi/smoke.geojson` with the
network short-circuited via monkeypatch on `stac._default_client_factory`.
The fixture client returns scripted `pystac.Item`s loaded from
`tests/fixtures/stac/`; no MS Planetary Computer call is made.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pystac
import pytest
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from typer.testing import CliRunner

from wildfire_exposure_eo import stac as stac_mod
from wildfire_exposure_eo.cli import app
from wildfire_exposure_eo.schemas import StacManifest

SMOKE_AOI = Path("data/aoi/smoke.geojson")
FIXTURES = Path("tests/fixtures/stac")


class _FixtureSearch:
    def __init__(self, items: list[pystac.Item]) -> None:
        self._items = items

    def items(self) -> list[pystac.Item]:
        return list(self._items)


class _FixtureClient:
    def __init__(self, items_by_collection: dict[str, list[pystac.Item]]) -> None:
        self.items_by_collection = items_by_collection

    def search(self, **kwargs: Any) -> _FixtureSearch:
        coll = (kwargs.get("collections") or ["?"])[0]
        return _FixtureSearch(self.items_by_collection.get(coll, []))


def _load_fixture(name: str) -> pystac.Item:
    return pystac.Item.from_file(str(FIXTURES / name))


def _fixture_items() -> dict[str, list[pystac.Item]]:
    return {
        "sentinel-2-l2a": [
            _load_fixture("s2_l2a_spring.json"),
            _load_fixture("s2_l2a_summer.json"),
        ],
        "sentinel-1-grd": [_load_fixture("s1_grd.json")],
        "cop-dem-glo-30": [_load_fixture("cop_dem_glo30.json")],
        "esa-worldcover": [_load_fixture("esa_worldcover.json")],
    }


def _expected_aoi_sha(aoi_path: Path) -> str:
    payload = json.loads(aoi_path.read_text())
    geoms = [shape(f["geometry"]) for f in payload.get("features", []) if f.get("geometry")]
    union = unary_union(geoms) if len(geoms) > 1 else geoms[0]
    canonical = json.dumps(mapping(union), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture()
def smoke_aoi_exists() -> Path:
    if not SMOKE_AOI.exists():
        pytest.skip(f"{SMOKE_AOI} not committed; cannot run smoke")
    return SMOKE_AOI


def test_resolve_stac_smoke_writes_validated_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    smoke_aoi_exists: Path,
) -> None:
    items = _fixture_items()
    monkeypatch.setattr(
        stac_mod,
        "_default_client_factory",
        lambda _url: _FixtureClient(items),
    )

    out_path = tmp_path / "stac_smoke.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["resolve-stac", "--smoke", "--out", str(out_path)],
    )
    assert result.exit_code == 0, result.output

    assert out_path.exists(), "manifest file was not written"

    payload = out_path.read_text()
    manifest = StacManifest.model_validate_json(payload)

    # totals match per-window sums
    for coll, windows in manifest.collections.items():
        assert manifest.totals[coll] == sum(w.items_returned for w in windows)
        for w in windows:
            assert w.items_returned == len(w.items)

    # AOI geometry hash matches a fresh recomputation from the smoke file
    assert manifest.aoi_geometry_sha == _expected_aoi_sha(SMOKE_AOI)

    # Provenance shape
    assert manifest.run_id
    assert manifest.code_commit_sha
    assert manifest.aoi_path.endswith("smoke.geojson")
    assert manifest.stac_catalog_url == stac_mod.PC_STAC_URL

    # S2 has the two-pass spring/summer split
    s2_windows = manifest.collections["sentinel-2-l2a"]
    assert {w.label for w in s2_windows} == {"spring", "summer"}
    summer = next(w for w in s2_windows if w.label == "summer")
    assert summer.relaxed_threshold_reason is not None

    # SAS token stripped from href_root everywhere
    for windows in manifest.collections.values():
        for w in windows:
            for it in w.items:
                assert "sig=" not in it.href_root
                assert "?" not in it.href_root
