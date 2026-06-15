"""WU-10 de-grid metric: quantify the burn-scar tiling artifact in a COG.

CONFIRMED ROOT CAUSE (2026-06-15): the tiling artifact is the Prithvi/ViT
per-crop POSITIONAL bias. Each terratorch `tiled_inference` 512px crop carries a
tent-shaped class-1 response (~5x core/border), PHASE-LOCKED to the same UTM crop
lattice (crop 512 / stride 448) across all ~179 scenes, then amplified by the
per-pixel composite into saturated squares. The composite autocorrelation period
is anisotropic ~499x385 px = the REPROJECTED inference stride (not the isotropic
512 COG block — so it is an inference artifact, not a display/blocking artifact).

This script makes the artifact a measured quantity, so the de-grid claim is
script-reproducible (CLAUDE.md fact-checking checklist). It computes, on the
finite (valid) pixels of a burn-scar COG:

  * 2D autocorrelation of the mean-removed composite via FFT (Wiener-Khinchin).
    The grid shows up as off-origin peaks at the crop-stride lag; we report the
    strongest non-trivial peak (its lag in px and its normalised height) inside a
    plausible stride band, plus the lag-row/lag-col 1D autocorrelation peaks so
    an anisotropic period (rows != cols) is visible.
  * The saturated-square fraction `frac_ge_095`: fraction of valid pixels at or
    above 0.95. A `max` composite of phase-locked tents drives this high; a
    de-gridded / percentile composite collapses it.
  * `frac_ge_05` for continuity with the validation harness.

Determinism: no RNG; pure function of the COG. Terminology guard
(non-negotiable #6): the COG value is a burn-scar inference score (relative model
score), never a calibrated probability or a forecast. Burn SCARS = post-event
spectral signatures of fires that already happened. Not ignition prediction.

Usage:

    uv run python scripts/16_burn_scar_gridmetric.py \\
        --cog outputs/cogs/burn_scar_<run_id>.tif \\
        --out outputs/diagnostics/16_gridmetric_<tag>.json

    # smoke (no network, no COG): synthetic phase-locked grid vs de-gridded
    uv run python scripts/16_burn_scar_gridmetric.py --smoke
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# Plausible crop-stride lag band (px) to search for the periodic peak. The
# inference stride is 448 px in UTM 10 m; after reprojection to EPSG:4326 the
# composite period stretches anisotropically (measured ~385..499 px), so the
# search band is widened around it. Lags below the floor are dominated by the
# autocorrelation main lobe and are excluded.
_STRIDE_LAG_MIN = 200
_STRIDE_LAG_MAX = 700
COG_CRS = "EPSG:4326"


def _highpass(field: np.ndarray, finite: np.ndarray, box: int) -> np.ndarray:
    """Subtract a `box`x`box` local mean so only grid-scale structure remains.

    The composite carries genuine low-frequency structure (real scars, terrain)
    that would dominate the autocorrelation and mask the periodic grid. A
    high-pass (field minus a box-blur wider than the crop stride) removes that
    smooth component, isolating the stride-scale periodicity the de-grid targets.
    Computed via a separable cumulative-sum box filter; masked pixels are 0.
    """
    filled = np.where(finite, field, 0.0).astype(np.float64)
    weight = finite.astype(np.float64)
    pad = box // 2

    def _box_sum(arr: np.ndarray) -> np.ndarray:
        padded = np.pad(arr, pad, mode="reflect")
        cs = np.cumsum(np.cumsum(padded, axis=0), axis=1)
        cs = np.pad(cs, ((1, 0), (1, 0)), mode="constant")
        h, w = arr.shape
        return (
            cs[box : box + h, box : box + w]
            - cs[0:h, box : box + w]
            - cs[box : box + h, 0:w]
            + cs[0:h, 0:w]
        )

    local_sum = _box_sum(filled)
    local_cnt = _box_sum(weight)
    with np.errstate(invalid="ignore", divide="ignore"):
        local_mean = np.where(local_cnt > 0, local_sum / local_cnt, 0.0)
    return np.where(finite, field - local_mean, 0.0)


def _autocorr_2d(field: np.ndarray, *, highpass_box: int | None = None) -> np.ndarray:
    """Normalised 2D autocorrelation of a (optionally high-passed) field via FFT.

    NaNs are zero-filled after mean removal (the standard estimator). When
    `highpass_box` is given, a box-blur of that width is subtracted first so the
    autocorrelation reflects only stride-scale periodicity, not the smooth
    large-scale structure of the scene. Normalised so zero-lag is 1.0; returned
    fftshift-ed so the zero lag sits at the centre.
    """
    finite = np.isfinite(field)
    if highpass_box is not None:
        centred = _highpass(field, finite, highpass_box)
    else:
        vals = field[finite]
        mean = float(vals.mean()) if vals.size else 0.0
        centred = np.where(finite, field - mean, 0.0).astype(np.float64)
    spectrum = np.fft.fft2(centred)
    power = np.abs(spectrum) ** 2
    ac = np.fft.ifft2(power).real
    if ac[0, 0] != 0:
        ac = ac / ac[0, 0]
    return np.fft.fftshift(ac)


def _peak_in_band(
    profile: np.ndarray, center: int, lag_min: int, lag_max: int
) -> tuple[int, float]:
    """Strongest 1D autocorrelation peak whose |lag| is in the stride band.

    `profile` is fftshift-ed (zero lag at `center`). Returns `(lag_px, height)`;
    `(0, 0.0)` when the band is empty.
    """
    best_lag, best_val = 0, 0.0
    n = profile.shape[0]
    for idx in range(n):
        lag = abs(idx - center)
        if lag_min <= lag <= lag_max and profile[idx] > best_val:
            best_lag, best_val = lag, float(profile[idx])
    return best_lag, best_val


def grid_metric(
    prob: np.ndarray,
    valid: np.ndarray,
    *,
    lag_min: int = _STRIDE_LAG_MIN,
    lag_max: int = _STRIDE_LAG_MAX,
) -> dict[str, Any]:
    """Periodic-peak + saturated-fraction metrics for one composite.

    `lag_min`/`lag_max` bound the crop-stride lag (px) the grid is searched in
    (defaults to the module constants; the synthetic smoke uses a smaller band).
    Returns the strongest off-origin autocorrelation peak inside that band (2D
    and per-axis), the spectral `grid_power_ratio`, the saturated-square fraction
    (>=0.95) and frac>=0.5.
    """
    field = np.where(valid, prob, np.nan).astype(np.float64)
    valid_vals = field[np.isfinite(field)]
    if valid_vals.size == 0:
        raise ValueError("no finite pixels in the composite")

    # High-pass at ~2x the top of the stride band so the autocorrelation reflects
    # the crop-grid periodicity, not the scene's smooth large-scale structure.
    highpass_box = max(8, 2 * lag_max)
    ac = _autocorr_2d(field, highpass_box=highpass_box)
    cy, cx = ac.shape[0] // 2, ac.shape[1] // 2

    # Per-axis 1D autocorrelation through the centre row/col.
    row_lag, row_val = _peak_in_band(ac[cy, :], cx, lag_min, lag_max)
    col_lag, col_val = _peak_in_band(ac[:, cx], cy, lag_min, lag_max)

    # 2D peak: scan the band (excluding the central main lobe) for the strongest
    # off-origin autocorrelation value — this is the grid's signature.
    h, w = ac.shape
    yy, xx = np.ogrid[:h, :w]
    radius = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    band = (radius >= lag_min) & (radius <= lag_max)
    if band.any():
        flat_idx = int(np.argmax(np.where(band, ac, -np.inf)))
        py, px = divmod(flat_idx, w)
        peak2d_val = float(ac[py, px])
        peak2d_lag = (int(py - cy), int(px - cx))
    else:
        peak2d_val = 0.0
        peak2d_lag = (0, 0)

    # Spectral grid signature: the share of the high-passed field's power that
    # sits at the crop-grid frequency. The grid lives at spatial period ~stride;
    # in the FFT that is a ring of frequencies |f| in [N/lag_max, N/lag_min]. A
    # phase-locked grid concentrates a large fraction of power in a few cells of
    # that ring; the de-grid spreads it back into the broadband floor. This
    # `grid_power_ratio` (peak-ring power / total high-passed power) is the
    # headline "grid removed?" number — it is bounded in [0, 1] and, unlike the
    # normalised autocorrelation, is not pinned near 1 by smooth structure.
    finite = np.isfinite(field)
    hp = _highpass(field, finite, highpass_box)
    spectrum = np.fft.fftshift(np.fft.fft2(hp))
    power = np.abs(spectrum) ** 2
    power[cy, cx] = 0.0  # drop the DC component
    fy = (yy - cy).astype(np.float64)
    fx = (xx - cx).astype(np.float64)
    freq_radius = np.sqrt(fy**2 + fx**2)
    # frequency (cycles/array) corresponding to a spatial period (px): N / period
    n = float(max(h, w))
    f_lo = n / lag_max
    f_hi = n / lag_min
    ring = (freq_radius >= f_lo) & (freq_radius <= f_hi)
    total_power = float(power.sum())
    ring_peak = float(power[ring].max()) if ring.any() else 0.0
    grid_power_ratio = (ring_peak / total_power) if total_power > 0 else 0.0

    frac_ge_095 = float(np.mean(valid_vals >= 0.95))
    frac_ge_05 = float(np.mean(valid_vals >= 0.5))
    return {
        "valid_pixels": int(valid_vals.size),
        "shape": [int(prob.shape[0]), int(prob.shape[1])],
        "mean": round(float(valid_vals.mean()), 6),
        "median": round(float(np.median(valid_vals)), 6),
        "frac_ge_05": round(frac_ge_05, 6),
        "frac_ge_095": round(frac_ge_095, 6),
        "grid_power_ratio": round(grid_power_ratio, 6),
        "autocorr_peak_2d": round(peak2d_val, 6),
        "autocorr_peak_2d_lag_px": peak2d_lag,
        "autocorr_peak_row_lag_px": row_lag,
        "autocorr_peak_row": round(row_val, 6),
        "autocorr_peak_col_lag_px": col_lag,
        "autocorr_peak_col": round(col_val, 6),
        "stride_lag_band_px": [lag_min, lag_max],
        "highpass_box_px": highpass_box,
    }


def _load_cog(cog_path: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Read a burn-scar COG; assert EPSG:4326; return (prob, valid, provenance)."""
    import rasterio

    with rasterio.open(cog_path) as src:
        prob = src.read(1).astype(np.float32)
        nodata = src.nodata
        crs = src.crs
        tags = src.tags()
    assert crs is not None and crs.to_epsg() == 4326, f"COG must be {COG_CRS}, got {crs}"
    valid = (prob != nodata) if nodata is not None else np.isfinite(prob)
    prov_raw = tags.get("WILDFIRE_EXPOSURE_EO_PROVENANCE")
    provenance = json.loads(prov_raw) if prov_raw else {}
    return prob, valid, provenance


def _scene_with_tents(
    rng: np.random.Generator, h: int, w: int, stride: int, dy: int, dx: int
) -> np.ndarray:
    """One scene: a saturating tent per crop centre + a low unburned floor.

    Each crop's class-1 response peaks (~0.97) at the crop centre and decays to
    the floor (~0.06) at the border — the measured ViT positional bias. The crop
    lattice is anchored at (dy, dx): phase-locked when (dy,dx) is fixed across
    scenes, jittered when it varies.
    """
    yy, xx = np.mgrid[0:h, 0:w]
    # distance (in px) from each pixel to its nearest crop centre on the lattice
    cy = (((yy - dy) % stride) - stride / 2).astype(np.float32)
    cx = (((xx - dx) % stride) - stride / 2).astype(np.float32)
    ty = (1 - np.abs(2 * cy / stride)).clip(0, 1)
    tx = (1 - np.abs(2 * cx / stride)).clip(0, 1)
    tent = ty * tx
    floor = 0.06 + 0.02 * rng.standard_normal((h, w))
    return (floor + 0.91 * tent).clip(0, 1).astype(np.float32)


def _synthetic_phase_locked(stride: int = 64, n_scenes: int = 30) -> np.ndarray:
    """Max-composite of PHASE-LOCKED tents — the artifact, reproduced in-memory.

    Every scene anchors the crop lattice at the SAME origin, so the per-pixel max
    pins the saturated tent centres to the same pixels -> a saturated grid.
    """
    rng = np.random.default_rng(42)
    h = w = 256
    composite = np.zeros((h, w), dtype=np.float32)
    for _ in range(n_scenes):
        composite = np.maximum(composite, _scene_with_tents(rng, h, w, stride, 0, 0))
    return composite.astype(np.float32)


def _synthetic_degridded(stride: int = 64, n_scenes: int = 30) -> np.ndarray:
    """p85-composite of JITTERED-origin tents — the de-grid, reproduced in-memory.

    Each scene's lattice origin is randomly offset, so no pixel is a tent centre
    every scene; the p85 composite no longer pins saturation to a fixed grid.
    """
    rng = np.random.default_rng(42)
    h = w = 256
    stack = np.empty((n_scenes, h, w), dtype=np.float32)
    for k in range(n_scenes):
        dy, dx = int(rng.integers(0, stride)), int(rng.integers(0, stride))
        stack[k] = _scene_with_tents(rng, h, w, stride, dy, dx)
    return np.percentile(stack, 85, axis=0).astype(np.float32)


#: Stride-lag band for the 256px synthetic smoke grid (its synthetic stride is
#: 64px, so the band brackets it without touching the module's real-data band).
_SMOKE_LAG_MIN, _SMOKE_LAG_MAX = 40, 120


def _run_smoke() -> int:
    """Synthetic A/B: phase-locked-max vs jittered-p85 on a small grid band."""
    before = _synthetic_phase_locked()
    after = _synthetic_degridded()
    valid = np.ones_like(before, dtype=bool)
    m_before = grid_metric(before, valid, lag_min=_SMOKE_LAG_MIN, lag_max=_SMOKE_LAG_MAX)
    m_after = grid_metric(after, valid, lag_min=_SMOKE_LAG_MIN, lag_max=_SMOKE_LAG_MAX)
    print("[gridmetric] SMOKE synthetic A/B (phase-locked max vs jittered p85):", file=sys.stderr)
    print(
        f"[gridmetric]   BEFORE: grid_power_ratio={m_before['grid_power_ratio']:.4f} "
        f"frac_ge_095={m_before['frac_ge_095']:.4f}",
        file=sys.stderr,
    )
    print(
        f"[gridmetric]   AFTER : grid_power_ratio={m_after['grid_power_ratio']:.4f} "
        f"frac_ge_095={m_after['frac_ge_095']:.4f}",
        file=sys.stderr,
    )
    # The de-grid must attenuate both the periodic spectral peak and the saturation.
    assert m_after["grid_power_ratio"] < m_before["grid_power_ratio"], "grid power not attenuated"
    assert m_after["frac_ge_095"] <= m_before["frac_ge_095"], "saturation not reduced"
    print("[gridmetric] SMOKE OK", file=sys.stderr)
    print(json.dumps({"smoke": True, "before": m_before, "after": m_after}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cog", type=Path, default=None, help="burn-scar inference COG")
    parser.add_argument("--out", type=Path, default=None, help="metric JSON output path")
    parser.add_argument("--tag", default=None, help="label recorded in JSON (e.g. before/after)")
    parser.add_argument("--smoke", action="store_true", help="synthetic A/B, no COG/network")
    args = parser.parse_args()

    if args.smoke:
        return _run_smoke()

    if args.cog is None:
        parser.error("--cog is required unless --smoke is set")

    prob, valid, provenance = _load_cog(args.cog)
    metric = grid_metric(prob, valid)
    run_at = datetime.now(UTC)
    payload: dict[str, Any] = {
        "generated_by": "scripts/16_burn_scar_gridmetric.py",
        "created_at_utc": run_at.isoformat(),
        "tag": args.tag,
        "cog_path": str(args.cog),
        "cog_run_id": provenance.get("run_id"),
        "reducer": provenance.get("reducer", "max"),
        "tile_origin_jitter": provenance.get("tile_origin_jitter", False),
        "tile_size": provenance.get("tile_size"),
        "tile_stride": provenance.get("tile_stride"),
        "value_semantics": (
            "burn-scar inference score (Prithvi-Burn-Scar class-1 softmax); relative "
            "model score, NOT a calibrated probability and NOT a fire forecast"
        ),
        **metric,
    }
    print("[gridmetric] =====================================================", file=sys.stderr)
    print(f"[gridmetric] COG: {args.cog}  reducer={payload['reducer']}", file=sys.stderr)
    print(f"[gridmetric] tile_origin_jitter={payload['tile_origin_jitter']}", file=sys.stderr)
    print(
        f"[gridmetric] grid_power_ratio={metric['grid_power_ratio']:.4f}  "
        f"autocorr_peak_2d={metric['autocorr_peak_2d']:.4f} "
        f"@lag={metric['autocorr_peak_2d_lag_px']} "
        f"(row {metric['autocorr_peak_row_lag_px']}px={metric['autocorr_peak_row']:.4f}, "
        f"col {metric['autocorr_peak_col_lag_px']}px={metric['autocorr_peak_col']:.4f})",
        file=sys.stderr,
    )
    print(
        f"[gridmetric] frac_ge_095={metric['frac_ge_095']:.4f} "
        f"frac_ge_05={metric['frac_ge_05']:.4f}",
        file=sys.stderr,
    )
    print("[gridmetric] =====================================================", file=sys.stderr)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"[gridmetric] metric JSON: {args.out}", file=sys.stderr)
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
