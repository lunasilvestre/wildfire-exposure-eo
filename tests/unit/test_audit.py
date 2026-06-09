"""Unit tests for `wildfire_exposure_eo.audit` probe functions.

Every probe is exercised on both a success path and a failure path. Network
calls are stubbed at the `requests.get` / `requests.post` boundary, and STAC
calls at the `pystac_client.Client.open` boundary, so these tests never touch
the network. The contract under test is the `CheckResult` shape — name, status,
and message — plus the ability to round-trip through the `SourceHealth`
Pydantic schema.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from wildfire_exposure_eo import audit
from wildfire_exposure_eo.audit import CheckResult
from wildfire_exposure_eo.schemas import SourceHealth, source_health_from_check

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PILOT_BBOX = (-8.4, 40.7, -8.3, 40.8)


class _FakeSTACSearch:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def items(self) -> list[object]:
        return self._items


class _FakeSTACClient:
    """Minimal stand-in for `pystac_client.Client`."""

    def __init__(
        self,
        items: list[object] | None = None,
        per_collection: dict[str, list[object]] | None = None,
        missing_collections: set[str] | None = None,
        raise_on_search: Exception | None = None,
    ) -> None:
        self._items = items or []
        self._per_collection = per_collection or {}
        self._missing = missing_collections or set()
        self._raise = raise_on_search

    def search(self, **kwargs: Any) -> _FakeSTACSearch:
        if self._raise is not None:
            raise self._raise
        cols = kwargs.get("collections") or []
        if cols and cols[0] in self._per_collection:
            return _FakeSTACSearch(self._per_collection[cols[0]])
        return _FakeSTACSearch(self._items)

    def get_collection(self, cid: str) -> object:
        if cid in self._missing:
            raise RuntimeError(f"missing {cid}")
        return object()


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_payload: dict[str, Any] | None = None,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_payload or {}
        self.content = content
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_pc(monkeypatch: pytest.MonkeyPatch, client: _FakeSTACClient) -> None:
    monkeypatch.setattr(audit, "_pc_client", lambda: client)


# ---------------------------------------------------------------------------
# load_aoi_bbox
# ---------------------------------------------------------------------------


def test_load_aoi_bbox_returns_min_max_extent(tmp_path: Path) -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-8.4, 40.7], [-8.3, 40.7], [-8.3, 40.8], [-8.4, 40.8]]],
                },
            }
        ],
    }
    path = tmp_path / "aoi.geojson"
    path.write_text(json.dumps(fc))
    assert audit.load_aoi_bbox(path) == (-8.4, 40.7, -8.3, 40.8)


def test_load_aoi_bbox_raises_on_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    with pytest.raises(ValueError):
        audit.load_aoi_bbox(path)


# ---------------------------------------------------------------------------
# Sentinel-2 L2A
# ---------------------------------------------------------------------------


def test_sentinel2_green_when_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(items=[object()] * 60))
    r = audit.check_sentinel2_l2a(PILOT_BBOX, min_items=50)
    assert r.name == "Sentinel-2 L2A"
    assert r.status == "GREEN"
    assert r.details["items_found"] == 60


def test_sentinel2_yellow_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(items=[object()] * 5))
    r = audit.check_sentinel2_l2a(PILOT_BBOX, min_items=50)
    assert r.status == "YELLOW"
    assert "5/50" in r.message


def test_sentinel2_red_on_no_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(items=[]))
    r = audit.check_sentinel2_l2a(PILOT_BBOX)
    assert r.status == "RED"


def test_sentinel2_red_on_search_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(raise_on_search=RuntimeError("boom")))
    r = audit.check_sentinel2_l2a(PILOT_BBOX)
    assert r.status == "RED"
    assert "boom" in r.message


# ---------------------------------------------------------------------------
# Sentinel-1 GRD
# ---------------------------------------------------------------------------


def test_sentinel1_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(items=[object()] * 120))
    r = audit.check_sentinel1_grd(PILOT_BBOX, min_items=100)
    assert r.status == "GREEN"


def test_sentinel1_red_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(raise_on_search=RuntimeError("oops")))
    r = audit.check_sentinel1_grd(PILOT_BBOX)
    assert r.status == "RED"


# ---------------------------------------------------------------------------
# Cop-DEM GLO-30
# ---------------------------------------------------------------------------


def test_cop_dem_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(items=[object(), object()]))
    r = audit.check_cop_dem_glo30(PILOT_BBOX)
    assert r.status == "GREEN"


def test_cop_dem_red_on_no_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(items=[]))
    r = audit.check_cop_dem_glo30(PILOT_BBOX)
    assert r.status == "RED"


# ---------------------------------------------------------------------------
# ESA WorldCover 2021
# ---------------------------------------------------------------------------


def test_esa_worldcover_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(items=[object()]))
    r = audit.check_esa_worldcover(PILOT_BBOX)
    assert r.status == "GREEN"


def test_esa_worldcover_red(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pc(monkeypatch, _FakeSTACClient(items=[]))
    r = audit.check_esa_worldcover(PILOT_BBOX)
    assert r.status == "RED"


# ---------------------------------------------------------------------------
# ETH GCH
# ---------------------------------------------------------------------------


def test_eth_gch_green_on_tiff_magic(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **_: Any) -> _FakeResponse:
        return _FakeResponse(status_code=206, content=b"II*\x00" + b"\x00" * 12)

    monkeypatch.setattr(audit.requests, "get", fake_get)
    r = audit.check_eth_gch(PILOT_BBOX)
    assert r.status == "GREEN"
    assert "tile" in r.details


def test_eth_gch_red_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audit.requests, "get", lambda *_a, **_k: _FakeResponse(status_code=404, content=b"")
    )
    r = audit.check_eth_gch(PILOT_BBOX)
    assert r.status == "RED"


def test_eth_gch_red_on_non_tiff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audit.requests,
        "get",
        lambda *_a, **_k: _FakeResponse(status_code=206, content=b"<html>nope</html>"),
    )
    r = audit.check_eth_gch(PILOT_BBOX)
    assert r.status == "RED"


def test_eth_gch_red_on_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*_: Any, **__: Any) -> _FakeResponse:
        raise OSError("network down")

    monkeypatch.setattr(audit.requests, "get", fake_get)
    r = audit.check_eth_gch(PILOT_BBOX)
    assert r.status == "RED"
    assert "network down" in r.message


# ---------------------------------------------------------------------------
# OSM Overpass
# ---------------------------------------------------------------------------


def _overpass_count_response(total: int) -> _FakeResponse:
    return _FakeResponse(
        status_code=200,
        json_payload={"elements": [{"tags": {"total": str(total)}}]},
    )


def test_overpass_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit.requests, "post", lambda *_a, **_k: _overpass_count_response(500))
    r = audit.check_overpass_osm(PILOT_BBOX, min_features_per_class=100, min_classes=3)
    assert r.status == "GREEN"


def test_overpass_yellow_low_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit.requests, "post", lambda *_a, **_k: _overpass_count_response(2))
    r = audit.check_overpass_osm(PILOT_BBOX, min_features_per_class=100, min_classes=3)
    assert r.status == "YELLOW"


def test_overpass_red_on_all_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> _FakeResponse:
        raise OSError("dns failure")

    monkeypatch.setattr(audit.requests, "post", boom)
    r = audit.check_overpass_osm(PILOT_BBOX)
    assert r.status == "RED"


# ---------------------------------------------------------------------------
# ICNF Áreas Ardidas
# ---------------------------------------------------------------------------


def test_icnf_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audit.requests,
        "get",
        lambda *_a, **_k: _FakeResponse(status_code=200, json_payload={"count": 3}),
    )
    r = audit.check_icnf_areas_ardidas(PILOT_BBOX, layers={20: "2025"})
    assert r.status == "GREEN"
    assert r.details["counts_per_layer"]["2025"] == 3


def test_icnf_red_on_no_intersections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audit.requests,
        "get",
        lambda *_a, **_k: _FakeResponse(status_code=200, json_payload={"count": 0}),
    )
    r = audit.check_icnf_areas_ardidas(PILOT_BBOX, layers={20: "2025"})
    assert r.status == "RED"


def test_icnf_red_on_endpoint_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> _FakeResponse:
        raise OSError("connection reset")

    monkeypatch.setattr(audit.requests, "get", boom)
    r = audit.check_icnf_areas_ardidas(PILOT_BBOX, layers={20: "2025"})
    assert r.status == "RED"
    assert "unreachable" in r.message


# ---------------------------------------------------------------------------
# HLS S30/L30
# ---------------------------------------------------------------------------


def test_hls_green(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeSTACClient(
        per_collection={"HLSL30_2.0": [object()] * 8, "HLSS30_2.0": [object()] * 12}
    )
    monkeypatch.setattr(audit.Client, "open", staticmethod(lambda _url: client))
    r = audit.check_hls_lpcloud(PILOT_BBOX, min_items=10)
    assert r.status == "GREEN"


def test_hls_yellow_low_count(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeSTACClient(per_collection={"HLSL30_2.0": [object()], "HLSS30_2.0": [object()]})
    monkeypatch.setattr(audit.Client, "open", staticmethod(lambda _url: client))
    r = audit.check_hls_lpcloud(PILOT_BBOX, min_items=10)
    assert r.status == "YELLOW"


def test_hls_yellow_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_url: str) -> None:
        raise OSError("no route to host")

    monkeypatch.setattr(audit.Client, "open", staticmethod(boom))
    r = audit.check_hls_lpcloud(PILOT_BBOX)
    assert r.status == "YELLOW"
    assert "unreachable" in r.message


def test_hls_red_on_missing_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeSTACClient(missing_collections={"HLSL30_2.0"})
    monkeypatch.setattr(audit.Client, "open", staticmethod(lambda _url: client))
    r = audit.check_hls_lpcloud(PILOT_BBOX)
    assert r.status == "RED"


# ---------------------------------------------------------------------------
# IPMA FWI
# ---------------------------------------------------------------------------


def test_ipma_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit.requests, "get", lambda *_a, **_k: _FakeResponse(status_code=200))
    r = audit.check_ipma_fwi()
    assert r.status == "GREEN"


def test_ipma_yellow_on_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit.requests, "get", lambda *_a, **_k: _FakeResponse(status_code=302))
    r = audit.check_ipma_fwi()
    assert r.status == "YELLOW"


def test_ipma_yellow_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> _FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr(audit.requests, "get", boom)
    r = audit.check_ipma_fwi()
    assert r.status == "YELLOW"


# ---------------------------------------------------------------------------
# SourceHealth adapter
# ---------------------------------------------------------------------------


def test_source_health_round_trip_from_check_result() -> None:
    cr = CheckResult(
        name="Sentinel-2 L2A",
        status="GREEN",
        message="60 items",
        details={"items_found": 60, "endpoint": audit.PC_STAC_URL},
    )
    sh = source_health_from_check(cr, elapsed_ms=420)
    assert isinstance(sh, SourceHealth)
    assert sh.source_id == "Sentinel-2 L2A"
    assert sh.status == "GREEN"
    assert sh.items_found == 60
    assert sh.endpoint == audit.PC_STAC_URL
    assert sh.elapsed_ms == 420


def test_source_health_accepts_missing_items_found() -> None:
    cr = CheckResult(name="IPMA FWI", status="GREEN", message="reachable", details={})
    sh = source_health_from_check(cr, elapsed_ms=10, endpoint="https://ipma.example")
    assert sh.items_found is None
    assert sh.endpoint == "https://ipma.example"


def test_source_health_sums_per_layer_counts() -> None:
    cr = CheckResult(
        name="ICNF Áreas Ardidas",
        status="GREEN",
        message="ok",
        details={"counts_per_layer": {"2025": 3, "2024": 7, "failed_year": -1}},
    )
    sh = source_health_from_check(cr, elapsed_ms=200)
    assert sh.items_found == 10


def test_source_health_rejects_unknown_status() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SourceHealth(
            source_id="x",
            status="BLUE",  # pyright: ignore[reportArgumentType]
            items_found=None,
            endpoint="",
            message="",
            elapsed_ms=0,
            checked_at_utc=datetime.now(UTC),
        )


def test_source_health_rejects_negative_elapsed() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SourceHealth(
            source_id="x",
            status="GREEN",
            items_found=None,
            endpoint="",
            message="",
            elapsed_ms=-1,
            checked_at_utc=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# Prithvi Burn-Scar (HF hub probe)
# ---------------------------------------------------------------------------


def _burn_scar_config_file(
    tmp_path: Path,
    model_id: str = "ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars",
    revision: str = "a3f2c410e45b8ac7417976614528a872f024d831",
) -> Path:
    cfg = tmp_path / "burn_scar.yaml"
    cfg.write_text(f'model:\n  hf_model_id: "{model_id}"\n  hf_revision_sha: "{revision}"\n')
    return cfg


def test_prithvi_green_when_pin_is_hub_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sha = "a3f2c410e45b8ac7417976614528a872f024d831"
    monkeypatch.setattr(
        audit.requests,
        "get",
        lambda *_a, **_k: _FakeResponse(status_code=200, json_payload={"sha": sha}),
    )
    result = audit.check_prithvi_burn_scar(_burn_scar_config_file(tmp_path, revision=sha))
    assert result.status == "GREEN"
    assert result.details["hub_main_revision"] == sha


def test_prithvi_green_when_hub_main_moved_past_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        audit.requests,
        "get",
        lambda *_a, **_k: _FakeResponse(status_code=200, json_payload={"sha": "newer-sha"}),
    )
    result = audit.check_prithvi_burn_scar(_burn_scar_config_file(tmp_path))
    assert result.status == "GREEN"
    assert "pinned revision stays fetchable" in result.message


def test_prithvi_red_on_unknown_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(audit.requests, "get", lambda *_a, **_k: _FakeResponse(status_code=404))
    result = audit.check_prithvi_burn_scar(
        _burn_scar_config_file(tmp_path, model_id="nobody/no-such-model")
    )
    assert result.status == "RED"
    assert "404" in result.message


def test_prithvi_yellow_on_connection_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("no route to host")

    monkeypatch.setattr(audit.requests, "get", boom)
    result = audit.check_prithvi_burn_scar(_burn_scar_config_file(tmp_path))
    assert result.status == "YELLOW"
    assert "unreachable" in result.message


def test_prithvi_red_on_placeholder_id(tmp_path: Path) -> None:
    result = audit.check_prithvi_burn_scar(
        _burn_scar_config_file(tmp_path, model_id="TBD-verified-at-audit")
    )
    assert result.status == "RED"
    assert "placeholder" in result.message


def test_prithvi_red_on_missing_config(tmp_path: Path) -> None:
    result = audit.check_prithvi_burn_scar(tmp_path / "absent.yaml")
    assert result.status == "RED"
    assert "cannot read" in result.message
