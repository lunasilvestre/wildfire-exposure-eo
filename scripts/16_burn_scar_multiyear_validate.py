"""WU-10 detection validation against MULTI-YEAR persistent burn scars.

`scripts/16_burn_scar_validate.py` grades the burn-scar inference COG against a
single ICNF vintage (default 2025) under a hard temporal-leakage gate — the
correct rule for the *forecasting* exposure score (WU-7), which must not see any
imagery after the perimeter date. But it is the wrong rule for grading a
*detection* layer: burn scars are post-event spectral signatures that persist
for years, so the imagery in the trailing window legitimately shows scars from
fires of earlier vintages. Grading the detector only against 2025 perimeters
(≈0.1 % of the pilot AOI) makes a good detector look broken.

This script grades the detector against the truth a detector should be graded
against: recent multi-year burns. It evaluates the same COG over a sequence of
vintage windows (2025-only / 2023-2025 / 2021-2025), reporting, for each:

  * coverage — share of valid AOI pixels inside any in-window ICNF perimeter
    (the burned base rate for that window);
  * best-F1 over the threshold sweep, and the threshold that achieves it;
  * precision at the feature's 0.5 binarisation threshold.

It reuses the rasterise / metric helpers from 16_burn_scar_validate.py and the
ICNF fetch from 09_burn_scar_audit.py, so the perimeter source can never drift
between the leakage-bound and detection validations. Determinism: seed 42
everywhere (CLAUDE.md non-negotiable #4).

The leakage gate is deliberately NOT applied here: leakage-safety binds the
forecasting score, not detection (see docstring above). The script asserts CRS
explicitly on both the COG (EPSG:4326) and the rasterised truth (non-negotiable
#2). The COG value is a burn-scar inference score — a relative model score
thresholded into above/below — NEVER a calibrated probability, a risk, or a
forecast. Burn SCARS detected = post-event spectral signatures of fires that
already happened. Not ignition prediction.

Usage:

    uv run python scripts/16_burn_scar_multiyear_validate.py \\
        --cog outputs/cogs/burn_scar_<run_id>.tif \\
        --out outputs/diagnostics/16_multiyear_detection_<run_id>.json

A smoke gate (`--smoke`) runs the identical metric path on a tiny synthetic grid
with no network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

_SCRIPTS = Path(__file__).resolve().parent


def _load_sibling(mod_name: str, file_name: str) -> Any:
    """Import a sibling script whose module name starts with a digit."""
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPTS / file_name)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_audit = _load_sibling("burn_scar_audit_09", "09_burn_scar_audit.py")
_validate = _load_sibling("burn_scar_validate_16", "16_burn_scar_validate.py")

sys.path.insert(0, str(_SCRIPTS.parent / "src"))
from wildfire_exposure_eo.stac import code_commit_sha

SEED = 42

#: ICNF MapServer layer ids per vintage year, confirmed against the live service
#: in scripts/00_icnf_fetch.sh. The detection windows below select recent years
#: whose scars are plausibly still visible in the trailing-window imagery.
VINTAGE_LAYER_ID = {
    2021: 15,
    2022: 17,
    2023: 18,
    2024: 19,
    2025: 20,
}

#: Detection truth windows, newest-first inside each label for readability.
DETECTION_WINDOWS: list[tuple[str, list[int]]] = [
    ("2025", [2025]),
    ("2023-2025", [2023, 2024, 2025]),
    ("2021-2025", [2021, 2022, 2023, 2024, 2025]),
]


def _rasterise_vintages(
    bounds: Any,
    shape: tuple[int, int],
    transform: Any,
    years: list[int],
) -> tuple[np.ndarray, int]:
    """Boolean burned mask on the COG grid for the union of `years` perimeters.

    No date filter inside the year (we take every perimeter ICNF files under
    that vintage layer); both sides are explicitly EPSG:4326 before rasterising
    (the MapServer query uses outSR=4326; the COG CRS is asserted by the caller).
    Returns `(mask, n_polys)`.
    """
    from rasterio.features import rasterize
    from shapely.geometry import shape as shp_shape

    bbox = (bounds.left, bounds.bottom, bounds.right, bounds.top)
    geoms: list[tuple[Any, int]] = []
    for year in years:
        feats = _audit.fetch_icnf_features(bbox, layer_id=VINTAGE_LAYER_ID[year])
        geoms.extend((shp_shape(f["geometry"]), 1) for f in feats if f.get("geometry"))
    if not geoms:
        return np.zeros(shape, dtype=bool), 0
    mask = rasterize(geoms, out_shape=shape, transform=transform, fill=0, dtype="uint8")
    assert mask is not None
    return mask.astype(bool), len(geoms)


def _detection_metrics(
    prob: np.ndarray,
    valid: np.ndarray,
    burned: np.ndarray,
) -> dict[str, Any]:
    """Full-grid detection metrics: coverage, sweep, best-F1, precision@0.5.

    Full-grid (every valid pixel) rather than a TN sample — coverage is high
    enough under multi-year truth that the whole-AOI confusion matrix is the
    honest readout. Reuses 16_burn_scar_validate._metrics_at for one threshold.
    """
    burned_valid = burned & valid
    flat_prob = prob[valid]
    y_true = burned_valid[valid]
    coverage = float(np.mean(y_true)) if y_true.size else 0.0

    sweep = [_validate._metrics_at(flat_prob, y_true, t) for t in _validate.THRESHOLD_SWEEP]
    best = max(sweep, key=lambda s: s["f1"])
    at_05 = _validate._metrics_at(flat_prob, y_true, 0.5)
    return {
        "valid_pixels": int(y_true.size),
        "burned_pixels": int(np.sum(y_true)),
        "coverage": round(coverage, 6),
        "best_f1": best["f1"],
        "best_f1_threshold": best["threshold"],
        "best_f1_precision": best["precision"],
        "best_f1_recall": best["recall"],
        "precision_at_05": at_05["precision"],
        "recall_at_05": at_05["recall"],
        "sweep": sweep,
    }


def _build_synthetic_smoke() -> dict[str, Any]:
    rng = np.random.default_rng(SEED)
    shape = (64, 64)
    prob = rng.random(shape, dtype=np.float64).astype(np.float32)
    burned = np.zeros(shape, dtype=bool)
    burned[10:30, 10:30] = True
    prob[burned] = 0.8
    valid = np.ones(shape, dtype=bool)
    return {"prob": prob, "burned": burned, "valid": valid}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cog", type=Path, default=None, help="burn-scar inference COG")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON result here")
    parser.add_argument("--smoke", action="store_true", help="synthetic grid, no network")
    args = parser.parse_args()

    commit = code_commit_sha(cwd=Path.cwd())
    run_at = datetime.now(UTC)

    windows: list[dict[str, Any]] = []
    if args.smoke:
        data = _build_synthetic_smoke()
        prob, valid = data["prob"], data["valid"]
        for label, _years in DETECTION_WINDOWS[:1]:
            m = _detection_metrics(prob, valid, data["burned"])
            m.update({"label": label, "years": [0], "n_polys": 1})
            windows.append(m)
        cog_path: Any = "(synthetic smoke grid)"
        provenance: dict[str, Any] = {"run_id": "smoke", "reducer": "p85"}
    else:
        if args.cog is None:
            parser.error("--cog is required unless --smoke is set")
        cog_path = args.cog
        cog = _validate._load_cog(cog_path)  # asserts EPSG:4326
        provenance = cog["provenance"]
        prob, valid = cog["prob"], cog["valid"]
        for label, years in DETECTION_WINDOWS:
            burned, n_polys = _rasterise_vintages(
                cog["bounds"], cog["shape"], cog["transform"], years
            )
            print(f"[multiyear] {label}: {n_polys} perimeters rasterised", file=sys.stderr)
            m = _detection_metrics(prob, valid, burned)
            m.update({"label": label, "years": years, "n_polys": n_polys})
            windows.append(m)

    print("", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print("WU-10 MULTI-YEAR DETECTION VALIDATION (no leakage gate)", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"COG: {cog_path}  reducer={provenance.get('reducer', 'p85')}", file=sys.stderr)
    print("| window | coverage | best-F1 (thr) | precision@0.5 |", file=sys.stderr)
    print("|---|---|---|---|", file=sys.stderr)
    for w in windows:
        print(
            f"| {w['label']} | {w['coverage'] * 100:.1f}% | "
            f"{w['best_f1']:.3f} (thr {w['best_f1_threshold']:.2f}) | "
            f"{w['precision_at_05']:.3f} |",
            file=sys.stderr,
        )
    print("=" * 72, file=sys.stderr)

    payload = {
        "run_id": run_at.strftime("%Y%m%dT%H%M%SZ"),
        "generated_by": "scripts/16_burn_scar_multiyear_validate.py",
        "code_commit_sha": commit,
        "created_at_utc": run_at.isoformat(),
        "seed": SEED,
        "smoke": bool(args.smoke),
        "cog_path": str(cog_path),
        "cog_provenance_run_id": provenance.get("run_id"),
        "reducer": provenance.get("reducer", "p85"),
        "leakage_gate_applied": False,
        "leakage_note": (
            "Detection truth = recent multi-year burn scars. Burn scars persist "
            "for years and are legitimately visible in the trailing-window "
            "imagery, so no temporal-leakage gate is applied here. The leakage "
            "gate binds the forecasting exposure score (WU-7), not detection."
        ),
        "value_semantics": (
            "burn-scar inference score (Prithvi-Burn-Scar class-1 softmax); "
            "relative model score thresholded into above/below; NOT a calibrated "
            "probability and NOT a fire forecast"
        ),
        "windows": windows,
    }
    out = args.out or (
        Path("outputs/diagnostics")
        / (
            "16_multiyear_detection_smoke.json"
            if args.smoke
            else f"16_multiyear_detection_{provenance.get('run_id', 'unknown')}.json"
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"[multiyear] JSON: {out}", file=sys.stderr)
    print(json.dumps({"out": str(out), "windows": [w["label"] for w in windows]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
