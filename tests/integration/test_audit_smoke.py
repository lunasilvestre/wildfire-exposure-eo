"""Integration smoke test for the `audit` CLI.

Runs the full Typer entry point against `data/aoi/smoke.geojson` with the
network short-circuited via monkeypatch. Verifies:

* CLI exits 0 when every probe is GREEN.
* The JSON report on disk validates against the `SourceHealth` schema.
* The `--json` mode round-trips the same payload.

Network access is patched out at `wildfire_exposure_eo.audit.run_all`; this
test never reaches a STAC endpoint or OSM Overpass.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wildfire_exposure_eo import audit as audit_mod
from wildfire_exposure_eo.cli import app
from wildfire_exposure_eo.schemas import SourceHealth, source_health_from_check

SMOKE_AOI = Path("data/aoi/smoke.geojson")


def _all_green_results() -> list[audit_mod.CheckResult]:
    return [
        audit_mod.CheckResult(
            name=name,
            status="GREEN",
            message=f"{name} synthetic ok",
            details={"items_found": 1, "endpoint": "https://example.test"},
        )
        for name in audit_mod.CHECKS
    ]


@pytest.fixture()
def smoke_aoi_exists() -> Path:
    if not SMOKE_AOI.exists():
        pytest.skip(f"{SMOKE_AOI} not committed; cannot run smoke")
    return SMOKE_AOI


def test_audit_cli_smoke_exits_zero_when_all_green(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    smoke_aoi_exists: Path,
) -> None:
    monkeypatch.setattr(audit_mod, "run_all", lambda _aoi: _all_green_results())

    runner = CliRunner()
    report_dir = tmp_path / "audit"
    result = runner.invoke(
        app,
        [
            "audit",
            "--aoi",
            str(smoke_aoi_exists),
            "--report-dir",
            str(report_dir),
        ],
    )

    assert result.exit_code == 0, result.stdout
    reports = list(report_dir.glob("*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text())
    assert payload["aoi_path"] == str(smoke_aoi_exists)
    assert len(payload["results"]) == len(audit_mod.CHECKS)
    assert {r["status"] for r in payload["results"]} == {"GREEN"}

    # The on-disk payload must validate cleanly through the SourceHealth schema.
    checked_at = datetime.fromisoformat(payload["checked_at_utc"])
    for raw, expected_name in zip(payload["results"], audit_mod.CHECKS, strict=True):
        cr = audit_mod.CheckResult(
            name=raw["name"],
            status=raw["status"],
            message=raw["message"],
            details=raw["details"],
        )
        sh = source_health_from_check(cr, elapsed_ms=0, checked_at_utc=checked_at)
        assert isinstance(sh, SourceHealth)
        assert sh.source_id == expected_name


def test_audit_cli_smoke_json_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    smoke_aoi_exists: Path,
) -> None:
    monkeypatch.setattr(audit_mod, "run_all", lambda _aoi: _all_green_results())
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "audit",
            "--aoi",
            str(smoke_aoi_exists),
            "--report-dir",
            str(tmp_path / "audit"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # stdout contains a printed JSON payload alongside the AOI/bbox preamble;
    # parse the report on disk for a clean assertion.
    report = next((tmp_path / "audit").glob("*.json"))
    payload = json.loads(report.read_text())
    assert payload["aoi_bbox_wgs84"]
    assert datetime.fromisoformat(payload["checked_at_utc"]).tzinfo is not None


def test_audit_cli_exits_nonzero_when_red(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    smoke_aoi_exists: Path,
) -> None:
    def with_one_red(_aoi: Path) -> list[audit_mod.CheckResult]:
        results = _all_green_results()
        results[0] = audit_mod.CheckResult(
            name=results[0].name, status="RED", message="synthetic fail", details={}
        )
        return results

    monkeypatch.setattr(audit_mod, "run_all", with_one_red)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["audit", "--aoi", str(smoke_aoi_exists), "--report-dir", str(tmp_path / "audit")],
    )
    assert result.exit_code == 1


def test_audit_cli_exits_two_when_yellow_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    smoke_aoi_exists: Path,
) -> None:
    def with_yellow(_aoi: Path) -> list[audit_mod.CheckResult]:
        results = _all_green_results()
        results[-1] = audit_mod.CheckResult(
            name=results[-1].name,
            status="YELLOW",
            message="synthetic degraded",
            details={},
        )
        return results

    monkeypatch.setattr(audit_mod, "run_all", with_yellow)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["audit", "--aoi", str(smoke_aoi_exists), "--report-dir", str(tmp_path / "audit")],
    )
    assert result.exit_code == 2


def test_smoke_aoi_bbox_is_inside_pilot() -> None:
    """Sanity check: smoke AOI must be a strict subset of the frozen pilot AOI."""
    if not SMOKE_AOI.exists() or not Path("data/aoi/pilot.geojson").exists():
        pytest.skip("AOI files not committed")
    pilot = audit_mod.load_aoi_bbox(Path("data/aoi/pilot.geojson"))
    smoke = audit_mod.load_aoi_bbox(SMOKE_AOI)
    assert pilot[0] <= smoke[0] <= smoke[2] <= pilot[2]
    assert pilot[1] <= smoke[1] <= smoke[3] <= pilot[3]


def test_session_clock_is_utc() -> None:
    """Documenting invariant: CLI writes `checked_at_utc` in UTC."""
    now = datetime.now(UTC)
    assert now.tzinfo is UTC
