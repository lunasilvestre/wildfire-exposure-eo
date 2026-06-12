"""Unit tests for scripts/12_make_figures.py data-loading helpers (WU-8, prompt 12)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point

# Repo-root import shim (mirrors the script itself)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import importlib

figures_mod = importlib.import_module("12_make_figures")

_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# _latest
# --------------------------------------------------------------------------- #


def test_latest_returns_newest(tmp_path: Path) -> None:
    (tmp_path / "foo_20230101.json").touch()
    (tmp_path / "foo_20240601.json").touch()
    result = figures_mod._latest("foo", tmp_path, ".json", smoke=False)
    assert result.name == "foo_20240601.json"


def test_latest_smoke_excludes_pilot(tmp_path: Path) -> None:
    (tmp_path / "foo_20240601.json").touch()
    (tmp_path / "foo_smoke_20240601.json").touch()
    result = figures_mod._latest("foo", tmp_path, ".json", smoke=True)
    assert "smoke" in result.name


def test_latest_pilot_excludes_smoke(tmp_path: Path) -> None:
    (tmp_path / "foo_20240601.json").touch()
    (tmp_path / "foo_smoke_20240601.json").touch()
    result = figures_mod._latest("foo", tmp_path, ".json", smoke=False)
    assert "smoke" not in result.name


def test_latest_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        figures_mod._latest("missing", tmp_path, ".json", smoke=False)


# --------------------------------------------------------------------------- #
# load_crosswalk
# --------------------------------------------------------------------------- #


def test_load_crosswalk_returns_dict() -> None:
    cw = figures_mod.load_crosswalk()
    assert isinstance(cw, dict)
    assert len(cw) >= 13  # NFFL-13 has 13 codes
    assert all(isinstance(k, int) for k in cw)
    for entry in cw.values():
        assert "nffl_name" in entry
        assert "internal_class" in entry


def test_load_crosswalk_code_range() -> None:
    cw = figures_mod.load_crosswalk()
    assert all(1 <= k <= 13 for k in cw)


# --------------------------------------------------------------------------- #
# load_exposure (with monkey-patched parquet)
# --------------------------------------------------------------------------- #


def _fake_exposure_gdf(n: int = 5) -> gpd.GeoDataFrame:
    """Minimal exposure GeoDataFrame with required columns."""
    return gpd.GeoDataFrame(
        {
            "asset_id": [f"node/{i}" for i in range(n)],
            "asset_class": ["power.tower"] * n,
            "exposure_rank": list(range(1, n + 1)),
            "exposure_score": [float(i) / n for i in range(1, n + 1)],
            "features": [
                json.dumps(
                    {
                        "fuel_class_severity_weight": 0.5,
                        "canopy_height_p90_m": 10.0,
                        "slope_max_deg": 15.0,
                        "historical_burn_share": 0.1,
                        "nbr_delta_recent": 0.05,
                    }
                )
            ]
            * n,
        },
        geometry=[Point(float(-8 + i * 0.01), 40.7) for i in range(n)],
        crs="EPSG:4326",
    )


def test_load_exposure_adds_rank_norm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gdf = _fake_exposure_gdf(n=10)
    parquet_path = tmp_path / "exposure_20260101T000000Z.parquet"
    gdf.to_parquet(parquet_path)
    monkeypatch.setattr(figures_mod, "_PARQUET_DIR", tmp_path)
    result = figures_mod.load_exposure(smoke=False)
    assert "rank_norm" in result.columns
    # rank 1 (most exposed) → rank_norm = 1.0
    top_norm = float(result.loc[result["exposure_rank"] == 1, "rank_norm"].iloc[0])
    assert top_norm == pytest.approx(1.0)
    # rank N (least exposed) → rank_norm = 0.0
    n = len(result)
    bot_norm = float(result.loc[result["exposure_rank"] == n, "rank_norm"].iloc[0])
    assert bot_norm == pytest.approx(0.0)


def test_load_exposure_parses_features(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gdf = _fake_exposure_gdf(n=3)
    parquet_path = tmp_path / "exposure_20260101T000000Z.parquet"
    gdf.to_parquet(parquet_path)
    monkeypatch.setattr(figures_mod, "_PARQUET_DIR", tmp_path)
    result = figures_mod.load_exposure(smoke=False)
    assert "fuel_class_severity_weight" in result.columns
    assert result["fuel_class_severity_weight"].notna().all()


# --------------------------------------------------------------------------- #
# _top3_features
# --------------------------------------------------------------------------- #


def test_top3_features_returns_html() -> None:
    feat_dict = {"slope_max_deg": 20.0, "canopy_height_p90_m": 5.0, "nbr_delta_recent": 0.1}
    weights = {"slope_max_deg": 0.25, "canopy_height_p90_m": 0.20, "nbr_delta_recent": 0.15}
    html = figures_mod._top3_features(feat_dict, weights)
    assert "<ul>" in html
    assert "slope_max_deg" in html


def test_top3_features_no_weights_gives_zero_contrib() -> None:
    # Features with no matching weight get contrib=0 but still appear
    feat_dict = {"foo": 1.0}
    weights: dict[str, float] = {}
    html = figures_mod._top3_features(feat_dict, weights)
    # contrib = 1.0 * 0.0 = 0; feature still listed since value is valid
    assert "foo" in html


def test_top3_features_skips_nan() -> None:
    feat_dict = {"slope_max_deg": float("nan"), "canopy_height_p90_m": 5.0}
    weights = {"slope_max_deg": 0.25, "canopy_height_p90_m": 0.20}
    html = figures_mod._top3_features(feat_dict, weights)
    assert "slope_max_deg" not in html
    assert "canopy_height_p90_m" in html
