"""Unit tests for wildfire_exposure_eo.static_rasters."""

from __future__ import annotations

import re
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shapely.geometry import box

from wildfire_exposure_eo import static_rasters as sr
from wildfire_exposure_eo.schemas.static_raster_manifest import FetchRecord, StaticRasterManifest

# ── helpers ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent / "fixtures" / "static_rasters"
_TIFF_LE_MAGIC = b"\x49\x49\x2a\x00"
_TIFF_BE_MAGIC = b"\x4d\x4d\x00\x2a"


def _make_tif_bytes(magic: bytes = _TIFF_LE_MAGIC) -> bytes:
    return magic + struct.pack("<I", 8) + struct.pack("<H", 0)


def _now() -> datetime:
    return datetime.now(UTC)


def _make_record(**kwargs: Any) -> FetchRecord:
    defaults: dict[str, Any] = dict(
        source_id="eth-gch-2020",
        vintage="2020",
        tile_id="N39W009",
        source_url="https://example.com/tile.tif",
        local_path="/tmp/tile.tif",
        bytes_downloaded=1024,
        sha256="a" * 64,
        fetched_at_utc=_now(),
        cache_hit=False,
        license="CC-BY 4.0",
        attribution="ETH Zurich",
    )
    defaults.update(kwargs)
    return FetchRecord(**defaults)


# ── tile-name computation ──────────────────────────────────────────────────────


class TestComputeEthGchTileIds:
    def test_smoke_aoi_returns_n39w009(self) -> None:
        # Smoke AOI: lon -8.37..-8.36, lat 40.73..40.74 (Sever do Vouga area)
        aoi = box(-8.3738, 40.731, -8.3620, 40.740)
        tiles = sr.compute_eth_gch_tile_ids(aoi)
        assert tiles == ["N39W009"]

    def test_pilot_aoi_returns_n39w009(self) -> None:
        # Full pilot AOI per docs/data_sources.md shell script comment
        aoi = box(-8.30, 39.80, -7.70, 40.30)
        tiles = sr.compute_eth_gch_tile_ids(aoi)
        assert tiles == ["N39W009"]

    def test_aoi_spanning_two_lat_bands(self) -> None:
        # AOI spanning lat 38..40 → two 3-degree bands: N36 and N39
        aoi = box(-9.5, 38.0, -9.0, 40.5)
        tiles = sr.compute_eth_gch_tile_ids(aoi)
        assert "N36W012" in tiles
        assert "N39W012" in tiles

    def test_aoi_spanning_two_lon_bands(self) -> None:
        # AOI spanning lon -10..-7 → two 3-degree lon bands: W012 and W009
        aoi = box(-10.5, 39.5, -7.5, 40.0)
        tiles = sr.compute_eth_gch_tile_ids(aoi)
        assert "N39W012" in tiles
        assert "N39W009" in tiles

    def test_equator_prime_meridian(self) -> None:
        # SW corner at (0, 0) → tile N00E000
        aoi = box(0.0, 0.0, 1.0, 1.0)
        tiles = sr.compute_eth_gch_tile_ids(aoi)
        assert "N00E000" in tiles

    def test_southern_hemisphere(self) -> None:
        # Lat -1 → floor(-1/3)*3 = -3 → S03
        aoi = box(10.0, -1.5, 11.0, -0.5)
        tiles = sr.compute_eth_gch_tile_ids(aoi)
        assert len(tiles) == 1
        assert tiles[0].startswith("S")


@given(
    min_lon=st.floats(min_value=-179.9, max_value=179.9),
    min_lat=st.floats(min_value=-89.9, max_value=89.9),
    delta=st.floats(min_value=0.01, max_value=2.9),
)
@settings(max_examples=200)
def test_tile_names_match_regex(min_lon: float, min_lat: float, delta: float) -> None:
    max_lon = min(min_lon + delta, 179.9)
    max_lat = min(min_lat + delta, 89.9)
    aoi = box(min_lon, min_lat, max_lon, max_lat)
    tiles = sr.compute_eth_gch_tile_ids(aoi)
    pattern = re.compile(r"^[NS]\d{2}[EW]\d{3}$")
    for tile in tiles:
        assert pattern.match(tile), f"tile {tile!r} does not match N/S dd E/W ddd"


# ── idempotency ────────────────────────────────────────────────────────────────


class TestFetchEthGchTileIdempotency:
    def test_cache_hit_skips_request(self, tmp_path: Path) -> None:
        """When cached file + matching SHA sidecar exist, no HTTP request is made."""
        tile_id = "N39W009"
        dest_dir = tmp_path / "eth-gch-2020"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / f"ETH_GlobalCanopyHeight_10m_2020_{tile_id}_Map.tif"

        tif_bytes = _make_tif_bytes()
        dest.write_bytes(tif_bytes)
        sha = sr._sha256_file(dest)
        sr._write_sidecar_sha(dest, sha)

        with patch("requests.get") as mock_get:
            record = sr.fetch_eth_gch_tile(tile_id, cache_dir=tmp_path)

        mock_get.assert_not_called()
        assert record.cache_hit is True
        assert record.sha256 == sha
        assert record.tile_id == tile_id

    def test_force_flag_re_downloads(self, tmp_path: Path) -> None:
        """--force bypasses the cache even when the SHA matches."""
        tile_id = "N39W009"
        dest_dir = tmp_path / "eth-gch-2020"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / f"ETH_GlobalCanopyHeight_10m_2020_{tile_id}_Map.tif"
        tif_bytes = _make_tif_bytes()
        dest.write_bytes(tif_bytes)
        sr._write_sidecar_sha(dest, sr._sha256_file(dest))

        magic_resp = MagicMock()
        magic_resp.content = tif_bytes[:16]
        magic_resp.raise_for_status = MagicMock()

        download_resp = MagicMock()
        download_resp.raise_for_status = MagicMock()
        download_resp.iter_content = MagicMock(return_value=[tif_bytes])
        download_resp.__enter__ = MagicMock(return_value=download_resp)
        download_resp.__exit__ = MagicMock(return_value=False)

        with patch("requests.get", side_effect=[magic_resp, download_resp]) as mock_get:
            record = sr.fetch_eth_gch_tile(tile_id, cache_dir=tmp_path, force=True)

        assert mock_get.call_count == 2
        assert record.cache_hit is False

    def test_sha_mismatch_triggers_re_download(self, tmp_path: Path) -> None:
        """When SHA sidecar does not match the local file, the file is re-downloaded."""
        tile_id = "N39W009"
        dest_dir = tmp_path / "eth-gch-2020"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / f"ETH_GlobalCanopyHeight_10m_2020_{tile_id}_Map.tif"
        tif_bytes = _make_tif_bytes()
        dest.write_bytes(tif_bytes)
        # Write a wrong (stale) SHA to the sidecar
        sr._write_sidecar_sha(dest, "a" * 64)

        magic_resp = MagicMock()
        magic_resp.content = tif_bytes[:16]
        magic_resp.raise_for_status = MagicMock()

        download_resp = MagicMock()
        download_resp.raise_for_status = MagicMock()
        download_resp.iter_content = MagicMock(return_value=[tif_bytes])
        download_resp.__enter__ = MagicMock(return_value=download_resp)
        download_resp.__exit__ = MagicMock(return_value=False)

        with patch("requests.get", side_effect=[magic_resp, download_resp]) as mock_get:
            record = sr.fetch_eth_gch_tile(tile_id, cache_dir=tmp_path)

        assert mock_get.call_count == 2
        assert record.cache_hit is False


# ── TIFF magic check ───────────────────────────────────────────────────────────


class TestTiffMagicCheck:
    def test_bad_magic_raises_before_full_download(self, tmp_path: Path) -> None:
        """A Range-GET returning non-TIFF magic raises ValueError immediately."""
        bad_magic = b"\x25\x50\x44\x46" + b"\x00" * 12  # PDF magic

        magic_resp = MagicMock()
        magic_resp.content = bad_magic
        magic_resp.raise_for_status = MagicMock()

        with (
            patch("requests.get", return_value=magic_resp) as mock_get,
            pytest.raises(ValueError, match="unexpected magic bytes"),
        ):
            sr.fetch_eth_gch_tile("N39W009", cache_dir=tmp_path)

        # Only the Range-GET was called (1 call), not the full download
        assert mock_get.call_count == 1

    def test_be_magic_is_accepted(self, tmp_path: Path) -> None:
        """Big-endian TIFF magic (MM 00 2A) is also a valid TIFF."""
        be_bytes = _TIFF_BE_MAGIC + b"\x00" * 12

        magic_resp = MagicMock()
        magic_resp.content = be_bytes
        magic_resp.raise_for_status = MagicMock()

        tif_bytes = be_bytes
        download_resp = MagicMock()
        download_resp.raise_for_status = MagicMock()
        download_resp.iter_content = MagicMock(return_value=[tif_bytes])
        download_resp.__enter__ = MagicMock(return_value=download_resp)
        download_resp.__exit__ = MagicMock(return_value=False)

        with patch("requests.get", side_effect=[magic_resp, download_resp]):
            record = sr.fetch_eth_gch_tile("N39W009", cache_dir=tmp_path)

        assert record.cache_hit is False


# ── error path handling ────────────────────────────────────────────────────────


class TestErrorPaths:
    def test_404_raises_immediately_no_retry(self, tmp_path: Path) -> None:
        """HTTP 404 is a permanent client error; no retries should be attempted."""
        import requests as req_mod

        magic_resp = MagicMock()
        magic_resp.content = _TIFF_LE_MAGIC + b"\x00" * 12
        magic_resp.raise_for_status = MagicMock()

        http_404 = req_mod.HTTPError(response=MagicMock(status_code=404))
        http_404.response.status_code = 404

        with (
            patch("requests.get", side_effect=[magic_resp, http_404]) as mock_get,
            pytest.raises(req_mod.HTTPError),
        ):
            sr.fetch_eth_gch_tile("N39W009", cache_dir=tmp_path)

        # magic GET + one download attempt (no retries on 404)
        assert mock_get.call_count == 2

    def test_5xx_retries_then_raises(self, tmp_path: Path) -> None:
        """HTTP 5xx triggers retries (up to 2) then re-raises."""
        import requests as req_mod

        magic_resp = MagicMock()
        magic_resp.content = _TIFF_LE_MAGIC + b"\x00" * 12
        magic_resp.raise_for_status = MagicMock()

        http_500 = req_mod.HTTPError(response=MagicMock(status_code=500))
        http_500.response.status_code = 500

        sides = [magic_resp, http_500, http_500, http_500]
        with (
            patch("requests.get", side_effect=sides) as mock_get,
            patch("time.sleep"),
            pytest.raises(req_mod.HTTPError),
        ):
            sr.fetch_eth_gch_tile("N39W009", cache_dir=tmp_path)

        # magic GET + 3 download attempts (initial + 2 retries)
        assert mock_get.call_count == 4

    def test_unknown_cosc_vintage_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown COSc vintage"):
            sr.fetch_dgt_cosc("9999_unknown", cache_dir=Path("/tmp"))

    def test_unknown_cos_vintage_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown COS vintage"):
            sr.fetch_dgt_cos("9999_unknown", cache_dir=Path("/tmp"))


# ── provenance population ──────────────────────────────────────────────────────


class TestProvenancePopulation:
    def test_fetch_record_all_fields_populated(self) -> None:
        record = _make_record()
        # Pydantic validates on construction; model_validate is also clean.
        validated = FetchRecord.model_validate(record.model_dump())
        assert validated.source_id == "eth-gch-2020"
        assert validated.vintage == "2020"
        assert validated.tile_id == "N39W009"
        assert validated.cache_hit is False
        assert len(validated.sha256) == 64
        assert validated.license != ""
        assert validated.attribution != ""

    def test_fetch_record_rejects_unknown_source_id(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            FetchRecord(
                source_id="unknown-source",  # type: ignore[arg-type]
                vintage="2020",
                tile_id=None,
                source_url="https://example.com",
                local_path="/tmp/x.tif",
                bytes_downloaded=100,
                sha256="a" * 64,
                fetched_at_utc=_now(),
                cache_hit=False,
                license="CC-BY",
                attribution="test",
            )

    def test_static_raster_manifest_round_trips_json(self) -> None:
        records = [_make_record()]
        manifest = sr.build_fetch_manifest(
            records,
            aoi_path="data/aoi/smoke.geojson",
            run_id="20260610T120000Z",
            code_commit_sha="abc" * 13 + "d",
            aoi_geometry_sha="def" * 21 + "g",
            resolved_at_utc=_now(),
        )
        json_str = manifest.model_dump_json()
        loaded = StaticRasterManifest.model_validate_json(json_str)
        assert loaded.run_id == manifest.run_id
        assert loaded.totals_bytes == manifest.totals_bytes
        assert len(loaded.records) == 1


# ── SHA sidecar helpers ────────────────────────────────────────────────────────


class TestShaSidecar:
    def test_write_and_read_sidecar(self, tmp_path: Path) -> None:
        p = tmp_path / "test.tif"
        p.write_bytes(b"some content")
        sha = sr._sha256_file(p)
        sr._write_sidecar_sha(p, sha)
        assert sr._read_sidecar_sha(p) == sha

    def test_read_sidecar_returns_none_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "test.tif"
        p.write_bytes(b"data")
        assert sr._read_sidecar_sha(p) is None
