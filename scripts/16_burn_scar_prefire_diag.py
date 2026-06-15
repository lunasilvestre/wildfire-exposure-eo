"""Phase-0 diagnostic: per-scene burn-scar inference on ONE pre-fire S2 scene.

GOAL — disentangle the two candidate causes of the published WU-1 COG's
massive over-prediction over the pilot AOI:

  (a) the 179-scene per-pixel MAX composite inflating the score (a reducer
      artefact: max over many scenes promotes any single noisy high pixel), or
  (b) per-scene domain shift / spectral confusion (the US-HLS-trained
      BurnScars checkpoint scoring Portuguese eucalyptus/pinus mosaics high
      even on a single, essentially-unburned observation).

The decisive test is to run the EXISTING inference pipeline on a SINGLE
pre-fire Sentinel-2 L2A observation over the pilot AOI and look at the raw
per-scene class-1 probability distribution. June 2025 is pre-fire for the
pilot: the big 2025 Portuguese fire season is Aug–Sep 2025, and ICNF reports
only ~43 ha burned inside the AOI across the whole matched window, so a clean
June-2025 scene is almost entirely unburned land — a well-behaved model should
score it near-zero almost everywhere.

This script REUSES the production pipeline functions, it does not reimplement
them (the whole point is to measure production behaviour):

  * burn_scar.resolve_prithvi_burn_scar_model  — pinned checkpoint, means/stds, device
  * burn_scar.query_recent_s2                   — STAC search + deterministic ordering + logging
  * burn_scar._item_epsg / _boa_offset          — grid + radiometry helpers
  * burn_scar._scene_probability                — the real load+preprocess+infer (SINGLE scene,
                                                  class-1 softmax; NaN where SCL/no-data masked)

It deliberately calls `_scene_probability` directly (the single-scene unit)
rather than `infer_burn_probability` (the MAX-composite wrapper) — the grid
setup below mirrors `infer_burn_probability` exactly (majority EPSG,
transform_bounds to UTM, the scene retry policy), so the only difference from
production is "one scene, no max-reduce" — which is exactly the variable under
test.

Terminology guard (CLAUDE.md non-negotiable #6): the value measured is a
burn-scar inference probability (relative model score), not a calibrated
probability and not any fire forecast.

DECISION RULE (printed at the end):
  frac>=0.5 > 40%   -> DOMAIN SHIFT / per-scene confusion dominates
  frac>=0.5 < 10%   -> MAX-COMPOSITE inflation dominates
  10%..40%          -> mixed; the script states the lean.

Usage (defaults to the chosen June-2025 scene + the frozen pilot AOI):

    uv run python scripts/16_burn_scar_prefire_diag.py \\
        --scene-id S2A_MSIL2A_..._T29TNF_... [more scene ids] \\
        --aoi data/aoi/pilot.geojson

All scene ids passed are treated as a SINGLE temporal observation (same date)
and gridded together with NO compositing — they are mosaicked onto the shared
grid only so adjacent tiles of one date cover the AOI; the per-scene retry and
SCL masking are identical to production.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from wildfire_exposure_eo import burn_scar
from wildfire_exposure_eo.stac import (
    PC_STAC_URL,
    _default_client_factory,
    _item_datetime,
    code_commit_sha,
    load_aoi_geometry,
)

if TYPE_CHECKING:
    import pystac

SEED = 42
HIST_EDGES = [round(0.1 * i, 1) for i in range(11)]  # 0.0,0.1,...,1.0
DEFAULT_AOI = Path("data/aoi/pilot.geojson")
DEFAULT_DIAG_DIR = Path("outputs/diagnostics")


def _configure_logging() -> None:
    """Route the burn_scar module's INFO logs (candidate ids, scene progress) to stderr."""
    for module in ("wildfire_exposure_eo.burn_scar", "wildfire_exposure_eo.stac"):
        log = logging.getLogger(module)
        if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


def _query_window_items(
    aoi: Any,
    start: str,
    end: str,
    *,
    max_cloud_cover: int,
) -> list[pystac.Item]:
    """Verify-then-act step 1+2: list S2 candidates, log every id/datetime/cloud, sort.

    Uses the SAME STAC client factory and deterministic ordering as the
    production `query_recent_s2`, but over an explicit datetime range so the
    diagnostic can pin the pre-fire window. Every candidate id is logged with
    its datetime and cloud cover before any raster is read.
    """
    from shapely.geometry import mapping

    cli: Any = _default_client_factory(PC_STAC_URL)
    search = cli.search(
        collections=[burn_scar.S2_COLLECTION],
        intersects=mapping(aoi),
        datetime=f"{start}/{end}",
        query={"eo:cloud_cover": {"lte": max_cloud_cover}},
    )
    items = sorted(search.items(), key=lambda it: (_item_datetime(it), it.id))
    print(
        f"[diag] {burn_scar.S2_COLLECTION} {start}..{end} cloud<={max_cloud_cover}%: "
        f"{len(items)} candidate item(s)",
        file=sys.stderr,
    )
    for it in items:
        cc = (it.properties or {}).get("eo:cloud_cover")
        cc_s = f"{float(cc):.1f}%" if isinstance(cc, int | float) else "?"
        print(
            f"[diag]   {_item_datetime(it).isoformat()}  cloud={cc_s:>7}  {it.id}",
            file=sys.stderr,
        )
    return items


def _scene_prob_with_retry(
    item: pystac.Item,
    handle: burn_scar.ModelHandle,
    cfg: Any,
    *,
    bounds: tuple[float, float, float, float],
    epsg: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Call the production `_scene_probability` with the production scene-retry policy.

    Mirrors the retry/re-sign behaviour of `infer_burn_probability` for one
    scene so transient blob/SAS failures don't abort the diagnostic.
    """
    last_exc: Exception | None = None
    for attempt in range(1, burn_scar._SCENE_ATTEMPTS + 1):
        try:
            return burn_scar._scene_probability(
                item,
                handle,
                s2_assets=cfg.inference.s2_assets,
                bounds=bounds,
                epsg=epsg,
                scl_mask_classes=cfg.inference.scl_mask_classes,
                tile_size=cfg.inference.tile_size,
                tile_stride=cfg.inference.tile_stride,
            )
        except Exception as exc:  # transient blob/network/SAS-expiry
            last_exc = exc
            burn_scar._SAS_CACHE.clear()
            if attempt == burn_scar._SCENE_ATTEMPTS:
                raise RuntimeError(f"scene {item.id} failed after {attempt} attempt(s)") from exc
            delay = burn_scar._SCENE_RETRY_DELAYS_S[attempt - 1]
            print(
                f"[diag]   {item.id} attempt {attempt}/{burn_scar._SCENE_ATTEMPTS} "
                f"failed ({exc}); re-signing, retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    if last_exc is not None:  # pragma: no cover - loop always returns/raises
        raise last_exc
    return None


def _stats(prob: np.ndarray) -> dict[str, Any]:
    """Histogram + summary stats over the valid (finite) per-pixel probabilities."""
    valid = prob[np.isfinite(prob)]
    n = int(valid.size)
    if n == 0:
        raise ValueError("no valid (unmasked) pixels survived the SCL/no-data mask")
    counts, _ = np.histogram(valid, bins=HIST_EDGES)
    hist = [
        {
            "bin": f"{HIST_EDGES[i]:.1f}-{HIST_EDGES[i + 1]:.1f}",
            "count": int(counts[i]),
            "pct": round(100.0 * int(counts[i]) / n, 4),
        }
        for i in range(len(counts))
    ]
    return {
        "valid_pixels": n,
        "mean": float(valid.mean()),
        "median": float(np.median(valid)),
        "frac_ge_0.5": float((valid >= 0.5).mean()),
        "frac_ge_0.7": float((valid >= 0.7).mean()),
        "histogram": hist,
    }


def _verdict(frac_ge_05: float) -> tuple[str, str]:
    """Apply the decision rule; return (verdict, one-sentence interpretation)."""
    pct = 100.0 * frac_ge_05
    if frac_ge_05 > 0.40:
        return (
            "DOMAIN_SHIFT_DOMINANT",
            f"frac>=0.5 = {pct:.1f}% (>40%): the model scores essentially-unburned "
            "pre-fire land as burn-scar on a SINGLE observation, so per-scene domain "
            "shift / spectral confusion is the dominant cause — swapping the MAX "
            "reducer alone will not fix it; masking + a higher threshold + a "
            "land-cover gate are needed.",
        )
    if frac_ge_05 < 0.10:
        return (
            "MAX_COMPOSITE_DOMINANT",
            f"frac>=0.5 = {pct:.1f}% (<10%): a single scene is well-behaved on "
            "unburned land, so the over-prediction is driven by the 179-scene "
            "per-pixel MAX composite inflating the score — the reducer swap "
            "(e.g. median/percentile) is the high-leverage fix.",
        )
    return (
        "MIXED",
        f"frac>=0.5 = {pct:.1f}% (10-40%): both effects contribute. Lean: "
        + (
            "toward domain shift (closer to the 40% bound)."
            if frac_ge_05 >= 0.25
            else "toward MAX-composite inflation (closer to the 10% bound)."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scene-id",
        action="append",
        default=None,
        help=(
            "Explicit S2 L2A item id(s) to run (a SINGLE date — repeat for adjacent "
            "tiles of one date). If omitted, the script lists candidates and picks "
            "the earliest low-cloud scene(s) of the earliest qualifying date."
        ),
    )
    parser.add_argument("--aoi", type=Path, default=DEFAULT_AOI)
    parser.add_argument("--start", default="2025-06-01", help="STAC window start (ISO date).")
    parser.add_argument("--end", default="2025-06-30", help="STAC window end (ISO date).")
    parser.add_argument(
        "--max-cloud-cover",
        type=int,
        default=30,
        help="eo:cloud_cover upper bound for candidate listing (default 30).",
    )
    parser.add_argument("--device", default=None, help="torch device (default: auto cuda/cpu).")
    parser.add_argument("--diag-dir", type=Path, default=DEFAULT_DIAG_DIR)
    args = parser.parse_args()

    # Determinism (CLAUDE.md non-negotiable #4): seed every RNG we can reach.
    # Inference is frozen-weights / no dropout, so this is belt-and-braces.
    random.seed(SEED)
    np.random.seed(SEED)
    import torch

    torch.manual_seed(SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(SEED)

    _configure_logging()

    cfg = burn_scar.load_burn_scar_config()
    aoi, aoi_sha = load_aoi_geometry(args.aoi)
    print(f"[diag] AOI: {args.aoi}  bbox={aoi.bounds}  sha={aoi_sha[:12]}", file=sys.stderr)

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    print(
        f"[diag] device={device}"
        + (f" ({torch.cuda.get_device_name(0)})" if device == "cuda" else ""),
        file=sys.stderr,
    )

    # --- Verify-then-act: list candidates, then choose the scene(s) -------------
    candidates = _query_window_items(
        aoi, args.start, args.end, max_cloud_cover=args.max_cloud_cover
    )
    n_candidates = len(candidates)
    by_id = {it.id: it for it in candidates}

    if args.scene_id:
        missing = [sid for sid in args.scene_id if sid not in by_id]
        if missing:
            raise SystemExit(f"--scene-id not in the {n_candidates} listed candidates: {missing}")
        chosen = [by_id[sid] for sid in args.scene_id]
    else:
        if not candidates:
            raise SystemExit(
                f"no S2 candidates in {args.start}..{args.end} cloud<={args.max_cloud_cover}% "
                "over the AOI — widen the window or relax cloud cover"
            )
        # Earliest qualifying DATE (calendar day), then take every tile of that
        # date so adjacent tiles can together cover the AOI — a SINGLE temporal
        # observation, no compositing across dates.
        first_day = _item_datetime(candidates[0]).date()
        chosen = [it for it in candidates if _item_datetime(it).date() == first_day]

    chosen_ids = [it.id for it in chosen]
    print(
        f"[diag] chosen {len(chosen)} scene(s) (single observation, NO compositing): {chosen_ids}",
        file=sys.stderr,
    )

    # --- Grid setup: identical to infer_burn_probability ------------------------
    from rasterio.warp import transform_bounds

    burn_scar._apply_gdal_http_defaults()
    handle = burn_scar.resolve_prithvi_burn_scar_model(cfg, device=device)

    epsg_counts = Counter(burn_scar._item_epsg(it) for it in chosen)
    epsg = epsg_counts.most_common(1)[0][0]
    if len(epsg_counts) > 1:
        print(
            f"[diag] scenes span {len(epsg_counts)} UTM zones {dict(epsg_counts)}; "
            f"gridding on majority EPSG:{epsg}",
            file=sys.stderr,
        )
    bounds = transform_bounds("EPSG:4326", f"EPSG:{epsg}", *aoi.bounds)

    # --- Per-scene inference (real pipeline), NO max-composite ------------------
    # When multiple tiles of the SAME date are passed, place each scene's valid
    # pixels onto the shared grid (mosaic by coordinate). This is spatial
    # in-fill of one observation, not temporal compositing: grids are disjoint
    # in their valid footprints, so no reducer choice is exercised.
    t0 = time.perf_counter()
    grid_prob: np.ndarray | None = None
    per_scene_valid: dict[str, int] = {}
    for i, item in enumerate(chosen, start=1):
        print(f"[diag] scene {i}/{len(chosen)} {item.id}", file=sys.stderr)
        result = _scene_prob_with_retry(item, handle, cfg, bounds=bounds, epsg=epsg)
        if result is None:
            print(f"[diag]   {item.id}: fully masked on AOI grid, skipped", file=sys.stderr)
            per_scene_valid[item.id] = 0
            continue
        prob, _xs, _ys = result
        per_scene_valid[item.id] = int(np.isfinite(prob).sum())
        if grid_prob is None:
            grid_prob = prob
        else:
            # All scenes share bounds+epsg+resolution -> identical grid shape.
            if prob.shape != grid_prob.shape:
                raise ValueError(
                    f"scene {item.id} grid {prob.shape} != reference {grid_prob.shape}; "
                    "cannot mosaic"
                )
            # Spatial in-fill: fill reference NaNs from this scene's valid pixels.
            fill = np.isnan(grid_prob) & np.isfinite(prob)
            grid_prob = np.where(fill, prob, grid_prob)
    runtime_s = time.perf_counter() - t0

    if grid_prob is None:
        raise SystemExit(
            f"all {len(chosen)} chosen scene(s) were fully masked over the AOI "
            "(clouds/no-data) — pick a clearer scene"
        )

    stats = _stats(grid_prob)
    verdict, interpretation = _verdict(stats["frac_ge_0.5"])

    # --- Report (human-readable to stderr; machine sidecar to JSON) ------------
    print("", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print("PHASE-0 PRE-FIRE PER-SCENE DIAGNOSTIC", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"scenes (single observation): {chosen_ids}", file=sys.stderr)
    print(
        f"window listed: {args.start}..{args.end} cloud<={args.max_cloud_cover}%  "
        f"({n_candidates} candidate(s))",
        file=sys.stderr,
    )
    print(f"device: {device}   runtime: {runtime_s:.1f}s", file=sys.stderr)
    print(f"valid (unmasked) pixels: {stats['valid_pixels']:,}", file=sys.stderr)
    print(f"mean prob:   {stats['mean']:.4f}", file=sys.stderr)
    print(f"median prob: {stats['median']:.4f}", file=sys.stderr)
    print(f"frac >= 0.5: {stats['frac_ge_0.5'] * 100:.2f}%", file=sys.stderr)
    print(f"frac >= 0.7: {stats['frac_ge_0.7'] * 100:.2f}%", file=sys.stderr)
    print("histogram (% of valid pixels):", file=sys.stderr)
    for h in stats["histogram"]:
        bar = "#" * round(h["pct"] / 2)
        print(f"  {h['bin']}  {h['pct']:6.2f}%  {bar}", file=sys.stderr)
    print("-" * 72, file=sys.stderr)
    print(f"VERDICT: {verdict}", file=sys.stderr)
    print(f"  {interpretation}", file=sys.stderr)
    print("=" * 72, file=sys.stderr)

    # --- JSON sidecar -----------------------------------------------------------
    run_at = datetime.now(UTC)
    run_id = run_at.strftime("%Y%m%dT%H%M%SZ")
    args.diag_dir.mkdir(parents=True, exist_ok=True)
    sidecar = args.diag_dir / f"burn_scar_prefire_diag_{run_id}.json"
    payload = {
        "run_id": run_id,
        "generated_by": "scripts/16_burn_scar_prefire_diag.py",
        "code_commit_sha": code_commit_sha(cwd=Path.cwd()),
        "created_at_utc": run_at.isoformat(),
        "seed": SEED,
        "purpose": (
            "Phase-0: is WU-1 burn-scar over-prediction driven by the MAX "
            "composite or by per-scene domain shift? Single pre-fire S2 "
            "observation over the pilot AOI, no compositing."
        ),
        "value_semantics": (
            "burn-scar inference probability (Prithvi-Burn-Scar class-1 softmax); "
            "relative model score, NOT a calibrated probability and NOT a fire forecast"
        ),
        "model": {
            "hf_model_id": handle.hf_model_id,
            "hf_revision_sha": handle.hf_revision_sha,
            "model_version": handle.model_version,
            "device": device,
            "torch_version": torch.__version__,
        },
        "aoi": {
            "path": str(args.aoi),
            "geometry_sha": aoi_sha,
            "bbox_wgs84": list(aoi.bounds),
        },
        "stac": {
            "catalog_url": PC_STAC_URL,
            "collection": burn_scar.S2_COLLECTION,
            "window_start": args.start,
            "window_end": args.end,
            "max_cloud_cover": args.max_cloud_cover,
            "n_candidates_listed": n_candidates,
            "chosen_scene_ids": chosen_ids,
            "single_observation_no_compositing": True,
            "grid_epsg": int(epsg),
            "per_scene_valid_pixels": per_scene_valid,
        },
        "preprocessing": {
            "s2_assets": list(cfg.inference.s2_assets),
            "scl_mask_classes": list(cfg.inference.scl_mask_classes),
            "tile_size": cfg.inference.tile_size,
            "tile_stride": cfg.inference.tile_stride,
            "boa_offset_and_scale": "(DN - boa_offset)/10000, clip>=0, then (x-mean)/std",
        },
        "runtime_seconds": round(runtime_s, 2),
        "stats": stats,
        "decision_rule": {
            "frac_ge_0.5_gt_0.40": "DOMAIN_SHIFT_DOMINANT",
            "frac_ge_0.5_lt_0.10": "MAX_COMPOSITE_DOMINANT",
            "otherwise": "MIXED",
        },
        "verdict": verdict,
        "interpretation": interpretation,
    }
    sidecar.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"[diag] sidecar: {sidecar}", file=sys.stderr)
    print(json.dumps({"verdict": verdict, "sidecar": str(sidecar), **stats}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
