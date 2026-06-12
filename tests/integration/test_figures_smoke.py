"""Smoke integration test: scripts/12_make_figures.py --smoke produces all artefacts.

Marked slow (skipped by default in CI) because it requires the full smoke-AOI
pipeline outputs (exposure parquet, fuel COG, burn-scar COG, ICNF burns
parquet, metrics JSON) to be present under outputs/.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "12_make_figures.py"
_FIGS_DIR = _ROOT / "docs" / "figures"

# All artefacts the smoke run must produce. The HTML map carries the smoke
# suffix too — a smoke run must never clobber the pilot exposure_map.html.
_EXPECTED_SMOKE = [
    "fig1_exposure_map_smoke.png",
    "fig2_fuel_layer_smoke.png",
    "fig3_burn_scar_smoke.png",
    "fig4_icnf_overlay_smoke.png",
    "fig5_lift_curve_smoke.png",
    "exposure_map_smoke.html",
]


@pytest.mark.slow
def test_smoke_produces_all_artefacts() -> None:
    """Running --smoke generates all six expected artefacts with non-zero size."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--smoke"],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    assert result.returncode == 0, (
        f"Script exited with code {result.returncode}.\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    for name in _EXPECTED_SMOKE:
        path = _FIGS_DIR / name
        assert path.exists(), f"Expected artefact not found: {path}"
        assert path.stat().st_size > 0, f"Artefact is empty: {path}"


@pytest.mark.slow
def test_smoke_html_under_size_limit() -> None:
    """The smoke HTML map must be under 25 MB."""
    html_path = _FIGS_DIR / "exposure_map_smoke.html"
    if not html_path.exists():
        pytest.skip("exposure_map_smoke.html not present; run the smoke test first")
    size_mb = html_path.stat().st_size / 1e6
    assert size_mb < 25, f"exposure_map_smoke.html is {size_mb:.1f} MB, exceeds 25 MB limit"


@pytest.mark.slow
def test_smoke_no_prohibited_language() -> None:
    """No HTML map under docs/figures/ may contain prohibited risk-probability language."""
    html_paths = sorted(_FIGS_DIR.glob("*.html"))
    if not html_paths:
        pytest.skip("no HTML maps present; run the smoke test first")
    prohibited = ("risk probability", "chance of fire", "fire probability", "risk score")
    for html_path in html_paths:
        text = html_path.read_text(errors="replace").lower()
        for term in prohibited:
            assert term not in text, f"Prohibited term '{term}' found in {html_path.name}"
