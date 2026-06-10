"""Unit tests for stac.py — deterministic resolution + provenance.

These tests stub `pystac_client.Client` with a recorder; no network is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from shapely.geometry import Polygon

from wildfire_exposure_eo import stac as stac_mod
from wildfire_exposure_eo.schemas import StacManifest


@dataclass
class _StubAsset:
    href: str


@dataclass
class _StubItem:
    id: str
    datetime: datetime
    bbox: list[float]
    properties: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, _StubAsset] = field(default_factory=dict)
    geometry: dict[str, Any] | None = None
    collection_id: str | None = None


class _StubItemSearch:
    def __init__(self, items: list[_StubItem]) -> None:
        self._items = items

    def items(self) -> list[_StubItem]:
        return list(self._items)


class _StubClient:
    """Records every `search()` kwargs invocation and returns scripted items."""

    def __init__(self, items_by_collection: dict[str, list[_StubItem]] | None = None) -> None:
        self.items_by_collection = items_by_collection or {}
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> _StubItemSearch:
        self.calls.append(kwargs)
        collections = kwargs.get("collections") or []
        coll = collections[0] if collections else "?"
        return _StubItemSearch(self.items_by_collection.get(coll, []))


_AOI_GEOJSON = (
    '{"type":"Feature","geometry":{"type":"Polygon","coordinates":'
    "[[[-8.4,40.7],[-8.3,40.7],[-8.3,40.75],[-8.4,40.75],[-8.4,40.7]]]"
    '},"properties":{}}'
)


def _aoi_geom() -> Polygon:
    return Polygon([(-8.4, 40.7), (-8.3, 40.7), (-8.3, 40.75), (-8.4, 40.75), (-8.4, 40.7)])


def _write_aoi(tmp_path: Path) -> Path:
    p = tmp_path / "aoi.geojson"
    p.write_text(_AOI_GEOJSON)
    return p


def _s2_item(item_id: str, dt: datetime, *, cloud: float = 10.0) -> _StubItem:
    """Stub S2 item with one SAS-tokened asset and one un-tokened asset."""
    return _StubItem(
        id=item_id,
        datetime=dt,
        bbox=[-8.5, 40.6, -8.2, 40.9],
        properties={"eo:cloud_cover": cloud},
        assets={
            "B04": _StubAsset(
                href=(
                    "https://sentinel2l2a01.blob.core.windows.net/sentinel2-l2/"
                    f"{item_id}/B04.tif?se=2030-01-01&sig=ABC123"
                )
            ),
            "B08": _StubAsset(
                href=(
                    f"https://sentinel2l2a01.blob.core.windows.net/sentinel2-l2/{item_id}/B08.tif"
                )
            ),
        },
        collection_id="sentinel-2-l2a",
    )


def test_resolution_orders_items_by_datetime_then_id() -> None:
    items = [
        _s2_item("S2A_LATE", datetime(2025, 5, 10, tzinfo=UTC)),
        _s2_item("S2A_EARLY", datetime(2025, 3, 5, tzinfo=UTC)),
        _s2_item("S2B_MID", datetime(2025, 4, 1, tzinfo=UTC)),
        _s2_item("S2A_MID", datetime(2025, 4, 1, tzinfo=UTC)),  # same dt as S2B_MID; id breaks tie
    ]
    client = _StubClient({"sentinel-2-l2a": items})
    refs = stac_mod.resolve_sentinel_2(
        _aoi_geom(),
        date(2025, 3, 1),
        date(2025, 6, 15),
        max_cloud_cover=30,
        client=client,  # type: ignore[arg-type]
    )
    assert [r.item_id for r in refs] == ["S2A_EARLY", "S2A_MID", "S2B_MID", "S2A_LATE"]


def test_two_pass_s2_cloud_cover_asymmetry(tmp_path: Path) -> None:
    aoi_path = _write_aoi(tmp_path)
    client = _StubClient({"sentinel-2-l2a": [_s2_item("S2A_X", datetime(2025, 5, 5, tzinfo=UTC))]})

    manifest = stac_mod.build_manifest(
        aoi_path,
        spring_start=date(2025, 3, 1),
        spring_end=date(2025, 6, 15),
        spring_cloud=30,
        summer_start=date(2025, 7, 1),
        summer_end=date(2025, 10, 31),
        summer_cloud=60,
        client=client,  # type: ignore[arg-type]
        run_id="20260514T120000Z",
        resolved_at_utc=datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        commit_sha="deadbeef",
    )

    s2_calls = [c for c in client.calls if c.get("collections") == ["sentinel-2-l2a"]]
    assert len(s2_calls) == 2
    assert s2_calls[0]["query"] == {"eo:cloud_cover": {"lte": 30}}
    assert s2_calls[1]["query"] == {"eo:cloud_cover": {"lte": 60}}

    spring_w, summer_w = manifest.collections["sentinel-2-l2a"]
    assert spring_w.label == "spring"
    assert spring_w.max_cloud_cover == 30
    assert spring_w.relaxed_threshold_reason is None
    assert summer_w.label == "summer"
    assert summer_w.max_cloud_cover == 60
    assert summer_w.relaxed_threshold_reason is not None
    assert "30%" in summer_w.relaxed_threshold_reason


def test_empty_collection_returns_zero_items(tmp_path: Path) -> None:
    aoi_path = _write_aoi(tmp_path)
    client = _StubClient({})  # every collection yields []

    manifest = stac_mod.build_manifest(
        aoi_path,
        spring_start=date(2025, 3, 1),
        spring_end=date(2025, 6, 15),
        spring_cloud=30,
        summer_start=date(2025, 7, 1),
        summer_end=date(2025, 10, 31),
        summer_cloud=60,
        client=client,  # type: ignore[arg-type]
        commit_sha="x",
    )

    for windows in manifest.collections.values():
        for w in windows:
            assert w.items_returned == 0
            assert w.items == ()
    assert manifest.totals == {
        "sentinel-2-l2a": 0,
        "sentinel-1-grd": 0,
        "cop-dem-glo-30": 0,
        "esa-worldcover": 0,
    }


def test_provenance_end_to_end(tmp_path: Path) -> None:
    aoi_path = _write_aoi(tmp_path)
    s1 = _StubItem(
        id="S1A_X",
        datetime=datetime(2025, 5, 1, tzinfo=UTC),
        bbox=[-8.5, 40.6, -8.2, 40.9],
        properties={},
        assets={
            "vv": _StubAsset(href="https://example.com/s1/X/vv.tif"),
            "vh": _StubAsset(href="https://example.com/s1/X/vh.tif"),
        },
        collection_id="sentinel-1-grd",
    )
    dem = _StubItem(
        id="DEM_T1",
        datetime=datetime(2021, 4, 21, tzinfo=UTC),
        bbox=[-9, 40, -8, 41],
        assets={"data": _StubAsset(href="https://example.com/dem/T1/data.tif")},
        collection_id="cop-dem-glo-30",
    )
    wc = _StubItem(
        id="WC_2021_T1",
        datetime=datetime(2021, 6, 30, tzinfo=UTC),
        bbox=[-9, 40, -8, 41],
        assets={"map": _StubAsset(href="https://example.com/wc/2021/T1/map.tif")},
        collection_id="esa-worldcover",
    )
    s2 = _s2_item("S2A_Y", datetime(2025, 5, 5, tzinfo=UTC))

    client = _StubClient(
        {
            "sentinel-2-l2a": [s2],
            "sentinel-1-grd": [s1],
            "cop-dem-glo-30": [dem],
            "esa-worldcover": [wc],
        }
    )

    manifest = stac_mod.build_manifest(
        aoi_path,
        spring_start=date(2025, 3, 1),
        spring_end=date(2025, 6, 15),
        spring_cloud=30,
        summer_start=date(2025, 7, 1),
        summer_end=date(2025, 10, 31),
        summer_cloud=60,
        worldcover_vintage=2021,
        client=client,  # type: ignore[arg-type]
        run_id="20260514T120000Z",
        resolved_at_utc=datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        commit_sha="deadbeefcafe",
    )

    # Round-trip through JSON ⇄ Pydantic.
    restored = StacManifest.model_validate_json(manifest.model_dump_json())
    assert restored.run_id == "20260514T120000Z"
    assert restored.code_commit_sha == "deadbeefcafe"
    assert restored.aoi_path == str(aoi_path)
    assert restored.aoi_geometry_sha and len(restored.aoi_geometry_sha) == 64
    assert restored.stac_catalog_url == stac_mod.PC_STAC_URL
    # Same stub list returned for both spring + summer S2 queries → totals = 2.
    assert restored.totals == {
        "sentinel-2-l2a": 2,
        "sentinel-1-grd": 1,
        "cop-dem-glo-30": 1,
        "esa-worldcover": 1,
    }

    s1_window = restored.collections["sentinel-1-grd"][0]
    assert s1_window.items[0].extra == {"mode": "IW", "polarizations": "VV,VH"}
    assert s1_window.items[0].assets_referenced == ("vh", "vv")

    s2_first = restored.collections["sentinel-2-l2a"][0].items[0]
    assert s2_first.assets_referenced == stac_mod.S2_ASSETS
    assert s2_first.cloud_cover == 10.0


def test_code_commit_sha_marks_dirty_tree(tmp_path: Path) -> None:
    """A dirty working tree gets a -dirty suffix; a clean one a bare SHA."""
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "HOME": str(tmp_path),
            },
        )

    git("init")
    (tmp_path / "a.txt").write_text("a")
    git("add", "a.txt")
    git("commit", "-m", "init")

    clean = stac_mod.code_commit_sha(cwd=tmp_path)
    assert len(clean) == 40 and not clean.endswith("-dirty")

    (tmp_path / "a.txt").write_text("b")
    dirty = stac_mod.code_commit_sha(cwd=tmp_path)
    assert dirty == f"{clean}-dirty"


def test_sas_tokens_stripped_from_href_root() -> None:
    item = _s2_item("S2A_SAS", datetime(2025, 4, 1, tzinfo=UTC))
    assert "sig=" in item.assets["B04"].href  # baseline: stub really has a SAS token
    client = _StubClient({"sentinel-2-l2a": [item]})

    refs = stac_mod.resolve_sentinel_2(
        _aoi_geom(),
        date(2025, 3, 1),
        date(2025, 6, 15),
        max_cloud_cover=30,
        client=client,  # type: ignore[arg-type]
    )

    assert len(refs) == 1
    assert "?" not in refs[0].href_root
    assert "sig=" not in refs[0].href_root
    assert "se=" not in refs[0].href_root
    assert refs[0].href_root.startswith(
        "https://sentinel2l2a01.blob.core.windows.net/sentinel2-l2/S2A_SAS"
    )
