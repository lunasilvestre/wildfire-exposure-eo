"""Integration smoke test for fetch-rasters --smoke --only eth-gch.

Uses monkeypatching (no network) to serve canned TIFF bytes.
Tests: manifest file created, Pydantic round-trip, cache directory populated,
cache_hit=False on first run and cache_hit=True on second run.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from wildfire_exposure_eo.cli import app
from wildfire_exposure_eo.schemas.static_raster_manifest import StaticRasterManifest

_TIFF_LE_MAGIC = b"\x49\x49\x2a\x00"


def _make_tif_bytes() -> bytes:
    return _TIFF_LE_MAGIC + struct.pack("<I", 8) + struct.pack("<H", 0)


def _make_mock_get(tif_bytes: bytes) -> Any:
    """Factory for requests.get mock that handles both Range-GET and full GET."""

    def _get(url: str, **kwargs: Any) -> Any:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        headers = kwargs.get("headers", {})
        if "Range" in headers:
            # Range-GET for magic check
            resp.content = tif_bytes[:16]
        else:
            # Full download (streaming=True)
            resp.iter_content = MagicMock(return_value=[tif_bytes])
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
        return resp

    return _get


class TestFetchRastersSmoke:
    """End-to-end smoke test for fetch-rasters --smoke --only eth-gch."""

    def test_first_run_creates_manifest_and_cache_file(self, tmp_path: Path) -> None:
        tif_bytes = _make_tif_bytes()
        runner = CliRunner()

        with patch("requests.get", side_effect=_make_mock_get(tif_bytes)):
            result = runner.invoke(
                app,
                [
                    "fetch-rasters",
                    "--smoke",
                    "--only",
                    "eth-gch",
                    "--cache-dir",
                    str(tmp_path / "cache"),
                    "--out",
                    str(tmp_path / "manifest.json"),
                ],
            )

        assert result.exit_code == 0, result.output

        manifest_path = tmp_path / "manifest.json"
        assert manifest_path.exists(), "manifest JSON was not written"

        manifest = StaticRasterManifest.model_validate_json(manifest_path.read_text())
        assert len(manifest.records) == 1
        rec = manifest.records[0]
        assert rec.source_id == "eth-gch-2020"
        assert rec.tile_id == "N39W009"
        assert rec.cache_hit is False
        assert rec.bytes_downloaded > 0

        cache_file = Path(rec.local_path)
        assert cache_file.exists()

    def test_second_run_produces_cache_hit(self, tmp_path: Path) -> None:
        tif_bytes = _make_tif_bytes()
        runner = CliRunner()
        cache_dir = tmp_path / "cache"
        manifest_path = tmp_path / "manifest.json"

        with patch("requests.get", side_effect=_make_mock_get(tif_bytes)):
            result = runner.invoke(
                app,
                [
                    "fetch-rasters",
                    "--smoke",
                    "--only",
                    "eth-gch",
                    "--cache-dir",
                    str(cache_dir),
                    "--out",
                    str(manifest_path),
                ],
            )
        assert result.exit_code == 0, result.output

        with patch("requests.get") as mock_get:
            result2 = runner.invoke(
                app,
                [
                    "fetch-rasters",
                    "--smoke",
                    "--only",
                    "eth-gch",
                    "--cache-dir",
                    str(cache_dir),
                    "--out",
                    str(manifest_path),
                ],
            )

        assert result2.exit_code == 0, result2.output
        mock_get.assert_not_called()

        manifest2 = StaticRasterManifest.model_validate_json(manifest_path.read_text())
        assert manifest2.records[0].cache_hit is True

    def test_manifest_round_trips_pydantic(self, tmp_path: Path) -> None:
        tif_bytes = _make_tif_bytes()
        runner = CliRunner()

        with patch("requests.get", side_effect=_make_mock_get(tif_bytes)):
            runner.invoke(
                app,
                [
                    "fetch-rasters",
                    "--smoke",
                    "--only",
                    "eth-gch",
                    "--cache-dir",
                    str(tmp_path / "cache"),
                    "--out",
                    str(tmp_path / "manifest.json"),
                ],
            )

        raw = (tmp_path / "manifest.json").read_text()
        manifest = StaticRasterManifest.model_validate_json(raw)
        assert manifest.totals_bytes > 0
        assert manifest.run_id != ""
        assert manifest.aoi_geometry_sha != ""
        for rec in manifest.records:
            assert len(rec.sha256) == 64
            assert rec.license != ""
            assert rec.attribution != ""

    def test_force_flag_re_downloads(self, tmp_path: Path) -> None:
        tif_bytes = _make_tif_bytes()
        runner = CliRunner()
        cache_dir = tmp_path / "cache"
        manifest_path = tmp_path / "manifest.json"

        with patch("requests.get", side_effect=_make_mock_get(tif_bytes)):
            runner.invoke(
                app,
                [
                    "fetch-rasters",
                    "--smoke",
                    "--only",
                    "eth-gch",
                    "--cache-dir",
                    str(cache_dir),
                    "--out",
                    str(manifest_path),
                ],
            )

        with patch("requests.get", side_effect=_make_mock_get(tif_bytes)) as mock_get:
            result = runner.invoke(
                app,
                [
                    "fetch-rasters",
                    "--smoke",
                    "--only",
                    "eth-gch",
                    "--cache-dir",
                    str(cache_dir),
                    "--out",
                    str(manifest_path),
                    "--force",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock_get.call_count >= 2  # magic GET + download GET

        manifest = StaticRasterManifest.model_validate_json(manifest_path.read_text())
        assert manifest.records[0].cache_hit is False
