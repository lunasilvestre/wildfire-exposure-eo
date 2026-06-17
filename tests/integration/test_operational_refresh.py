"""Integration tests for the operational-refresh orchestrator (scripts/26).

Exercises the orchestrator's graceful-failure contract (EWDS down / no data ->
keep last-good, exit non-zero, nothing published) and its offline ``--smoke``
config check, without any network. The script module name starts with a digit,
so it is loaded via importlib.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))


def _load_orchestrator() -> ModuleType:
    path = _ROOT / "scripts" / "26_operational_refresh.py"
    spec = importlib.util.spec_from_file_location("_operational_refresh_26", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_is_offline_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_orchestrator()
    # No credentials available -> --smoke must still pass (it touches no network).
    monkeypatch.delenv("CDSAPI_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["26", "--aoi", "pilot", "--smoke"])
    assert mod.main() == 0


def test_unknown_aoi_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_orchestrator()
    monkeypatch.setattr(sys, "argv", ["26", "--aoi", "does_not_exist", "--smoke"])
    assert mod.main() == 2


def test_graceful_failure_when_ewds_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """EWDS pull raises -> exit 1, no watch artefacts written, last-good kept."""
    mod = _load_orchestrator()

    # Avoid needing real credentials; the FWI refresh is what we force to fail.
    monkeypatch.setattr(mod, "load_ewds_key", lambda: "fake-key")

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("no published EWDS FWI day (simulated EWDS outage)")

    monkeypatch.setattr(mod, "refresh_fwi_cogs", _boom)

    # If the orchestrator wrongly proceeded, these would be called — make them fail loudly.
    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("must not publish after a failed FWI refresh")

    monkeypatch.setattr(mod, "upload_cogs_to_r2", _must_not_run)
    monkeypatch.setattr(mod, "patch_style_fwi_overlay", _must_not_run)
    monkeypatch.setattr(mod, "_write_watch_artifacts", _must_not_run)

    monkeypatch.setattr(sys, "argv", ["26", "--aoi", "pilot"])
    assert mod.main() == 1


def test_graceful_failure_when_r2_upload_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """R2 upload fails -> exit 1, style_data NOT patched (no dangling hrefs)."""
    mod = _load_orchestrator()
    monkeypatch.setattr(mod, "load_ewds_key", lambda: "fake-key")

    fake_surface = object()
    fake_components: list[dict[str, object]] = [{"filename": "fwi_fwi_3857_2026-06-11.tif"}]
    fake_manifest = {"provenance": {"fwi_valid_date": "2026-06-11"}}
    monkeypatch.setattr(
        mod,
        "refresh_fwi_cogs",
        lambda **_kw: (fake_surface, fake_components, fake_manifest),
    )

    def _upload_boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("rclone upload failed (simulated)")

    monkeypatch.setattr(mod, "upload_cogs_to_r2", _upload_boom)

    def _must_not_run(*_a: object, **_k: object) -> object:
        raise AssertionError("must not patch style_data after a failed upload")

    monkeypatch.setattr(mod, "patch_style_fwi_overlay", _must_not_run)

    monkeypatch.setattr(sys, "argv", ["26", "--aoi", "pilot"])
    assert mod.main() == 1


def test_watch_list_run_schema_roundtrips() -> None:
    """WatchListRun carries the full per-run provenance (#3) and never leaks a key."""
    from wildfire_exposure_eo.operational import FWI_REF, FWI_REF_RATIONALE, WATCH_PRIORITY_FORMULA
    from wildfire_exposure_eo.schemas import WatchListRun

    run = WatchListRun(
        run_id="20260611T000000Z",
        code_commit_sha="abc123",
        model_version="0.3.1",
        seed=42,
        aoi_name="pilot",
        aoi_path="data/aoi/pilot.geojson",
        exposure_run_id="20260611T170549Z",
        formula=WATCH_PRIORITY_FORMULA,
        fwi_ref=FWI_REF,
        fwi_ref_rationale=FWI_REF_RATIONALE,
        fwi_valid_date="2026-06-11",
        fwi_requested_date="2026-06-13",
        fwi_product_id="cems-fire-historical-v1",
        fwi_doi="10.24381/cds.0e89c522",
        fwi_dataset_type="intermediate_dataset",
        fwi_system_version="4_1",
        fwi_attribution="Source: CEMS Early Warning Data Store — Copernicus / ECMWF (CC-BY-4.0)",
        fwi_lag_note="~2-day lag",
        n_assets=3045,
        n_with_fwi=3045,
        top_n=25,
    )
    dumped = run.model_dump_json()
    assert "10.24381/cds.0e89c522" in dumped
    assert "fake-key" not in dumped  # no key field exists at all
    assert WatchListRun.model_validate_json(dumped).fwi_doi == "10.24381/cds.0e89c522"
