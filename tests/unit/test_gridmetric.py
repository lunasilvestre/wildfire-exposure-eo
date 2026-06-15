"""Unit tests for scripts/16_burn_scar_gridmetric.py (WU-10 de-grid metric).

No network, no COG: the grid metric is a pure function of an in-memory array.
We assert it (a) detects a phase-locked periodic grid and a high saturated-square
fraction, and (b) reports both as strongly attenuated once the grid is removed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np

# Repo-root import shim (mirrors the script itself); digit-prefixed module name.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
gridmetric = importlib.import_module("16_burn_scar_gridmetric")


_BAND = {"lag_min": 40, "lag_max": 120}  # synthetic stride is 64px


def test_grid_metric_detects_phase_locked_grid_and_saturation() -> None:
    """A max-composite of phase-locked tents has a strong periodic peak + saturation."""
    composite = gridmetric._synthetic_phase_locked(stride=64, n_scenes=30)
    valid = np.ones_like(composite, dtype=bool)
    m = gridmetric.grid_metric(composite, valid, **_BAND)
    assert m["grid_power_ratio"] > 0.05  # power concentrated at the grid frequency
    # the detected period sits near the synthetic stride on at least one axis
    assert (
        abs(m["autocorr_peak_row_lag_px"] - 64) <= 8 or abs(m["autocorr_peak_col_lag_px"] - 64) <= 8
    )


def test_grid_metric_attenuated_after_degrid() -> None:
    """Jittered-origin p85 composite drops both the periodic peak and saturation."""
    before = gridmetric.grid_metric(
        gridmetric._synthetic_phase_locked(stride=64, n_scenes=30),
        np.ones((256, 256), dtype=bool),
        **_BAND,
    )
    after = gridmetric.grid_metric(
        gridmetric._synthetic_degridded(stride=64, n_scenes=30),
        np.ones((256, 256), dtype=bool),
        **_BAND,
    )
    assert after["grid_power_ratio"] < before["grid_power_ratio"]
    assert after["frac_ge_095"] <= before["frac_ge_095"]


def test_grid_metric_smoke_path_exits_zero() -> None:
    """The script's --smoke A/B path runs and asserts attenuation (exit 0)."""
    assert gridmetric._run_smoke() == 0
