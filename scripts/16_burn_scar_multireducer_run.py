"""WU-10: run burn-scar inference ONCE and emit several candidate composites.

To pick the composite reducer data-drivenly we need every candidate
(`max`, `median`, `p85`, `consensus_3`, ...) computed from the SAME scene stack
and the SAME inference pass — running inference once per reducer would be
~5x the GPU time and would not isolate the reducer as the only variable.

This driver reuses the production pipeline:

  * burn_scar.query_recent_s2          — STAC search + deterministic ordering + logging
  * burn_scar.filter_to_season         — fire-season window restriction (WU-10 1b)
  * burn_scar.infer_burn_probability_multi — one tiled, memory-bounded pass that
    streams each scene to an on-disk memmap and reduces it block-wise under
    EVERY requested reducer (WU-10 1a)
  * burn_scar.write_burn_scar_cog      — provenance-tagged COG per candidate

It does NOT touch the live STAC item, R2, or the exposure parquet — every
candidate COG is written under outputs/ only (HIL boundary: FLAG A).

Determinism: seed 42 (CLAUDE.md non-negotiable #4). Verify-then-act: candidate
S2 item ids are listed+logged by query_recent_s2 before any raster read; run
--smoke first. Terminology guard (non-negotiable #6): the COG value is a
burn-scar inference score (relative model score), never a probability/forecast.

Usage:

    uv run python scripts/16_burn_scar_multireducer_run.py \\
        --aoi data/aoi/pilot.geojson \\
        --window-end 2026-06-09 --window-months 12 \\
        --reducers max,median,p85,consensus_3 \\
        --out-dir /abs/path/outputs/cogs --device cuda
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from importlib.metadata import version as pkg_version
from pathlib import Path

import numpy as np

from wildfire_exposure_eo import burn_scar
from wildfire_exposure_eo.stac import (
    PC_STAC_URL,
    code_commit_sha,
    load_aoi_geometry,
)

SEED = 42
DEFAULT_REDUCERS = ("max", "median", "p85", "consensus_3")


def _configure_logging() -> None:
    import logging

    for module in ("wildfire_exposure_eo.burn_scar", "wildfire_exposure_eo.stac"):
        log = logging.getLogger(module)
        if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


def _stats(da: object) -> dict[str, float]:
    """median / mean / frac>=0.5 over the valid (finite) pixels of one composite."""
    import xarray as xr

    assert isinstance(da, xr.DataArray)
    arr = np.asarray(da.values, dtype=np.float64)
    valid = arr[np.isfinite(arr)]
    if valid.size == 0:
        return {"valid_pixels": 0, "median": float("nan"), "mean": float("nan"), "frac_ge_05": 0.0}
    return {
        "valid_pixels": int(valid.size),
        "median": round(float(np.median(valid)), 6),
        "mean": round(float(valid.mean()), 6),
        "frac_ge_05": round(float((valid >= 0.5).mean()), 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aoi", type=Path, default=Path("data/aoi/pilot.geojson"))
    parser.add_argument("--config", type=Path, default=burn_scar.DEFAULT_CONFIG_PATH)
    parser.add_argument("--window-end", default=None, help="ISO date; default today UTC")
    parser.add_argument("--window-months", type=int, default=None)
    parser.add_argument(
        "--reducers",
        default=",".join(DEFAULT_REDUCERS),
        help="comma-separated reducer list (max,median,p85,consensus_3,...)",
    )
    parser.add_argument("--out-dir", type=Path, required=True, help="absolute output dir for COGs")
    parser.add_argument("--device", default=None)
    parser.add_argument(
        "--tile-origin-jitter",
        dest="tile_origin_jitter",
        action="store_true",
        default=None,
        help="force the WU-10 de-grid crop-origin jitter on (default: config value)",
    )
    parser.add_argument(
        "--no-tile-origin-jitter",
        dest="tile_origin_jitter",
        action="store_false",
        help="force the de-grid crop-origin jitter off (A/B against jitter on)",
    )
    parser.add_argument(
        "--tile-stride",
        type=int,
        default=None,
        help="override config tile_stride (smaller = more overlap, flatter tent, more GPU)",
    )
    parser.add_argument(
        "--out-prefix",
        default=None,
        help="override the COG filename prefix (e.g. burn_scar_wu10degrid)",
    )
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()

    random.seed(SEED)
    np.random.seed(SEED)
    import torch

    torch.manual_seed(SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(SEED)

    _configure_logging()

    aoi_path = Path("data/aoi/smoke.geojson") if args.smoke else args.aoi
    reducers = tuple(r.strip() for r in args.reducers.split(",") if r.strip())
    cfg = burn_scar.load_burn_scar_config(args.config)
    tile_origin_jitter = (
        cfg.inference.tile_origin_jitter
        if args.tile_origin_jitter is None
        else args.tile_origin_jitter
    )
    tile_stride = args.tile_stride if args.tile_stride is not None else cfg.inference.tile_stride
    months = args.window_months if args.window_months is not None else cfg.inference.window_months
    end = (
        datetime.fromisoformat(args.window_end).date()
        if args.window_end is not None
        else datetime.now(UTC).date()
    )
    start = burn_scar.months_back(end, months)

    geometry, aoi_sha = load_aoi_geometry(aoi_path)
    print(f"[multi] AOI {aoi_path} sha={aoi_sha[:12]} window {start}..{end}", file=sys.stderr)
    print(f"[multi] reducers: {list(reducers)}", file=sys.stderr)

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    handle = burn_scar.resolve_prithvi_burn_scar_model(cfg, device=device)

    items = burn_scar.query_recent_s2(
        geometry,
        months,
        max_cloud_cover=cfg.inference.s2_max_cloud_cover,
        window_end=end,
    )
    items = burn_scar.filter_to_season(
        items,
        season_start_month=cfg.inference.season_start_month,
        season_end_month=cfg.inference.season_end_month,
    )
    if not items:
        print("[multi] no S2 items after season filter — nothing to infer", file=sys.stderr)
        return 1

    print(
        f"[multi] de-grid: tile_origin_jitter={tile_origin_jitter} "
        f"tile_size={cfg.inference.tile_size} tile_stride={tile_stride}",
        file=sys.stderr,
    )
    composites = burn_scar.infer_burn_probability_multi(
        items,
        handle,
        geometry,
        s2_assets=cfg.inference.s2_assets,
        scl_mask_classes=cfg.inference.scl_mask_classes,
        reducers=reducers,
        tile_size=cfg.inference.tile_size,
        tile_stride=tile_stride,
        tile_origin_jitter=tile_origin_jitter,
        seed=SEED,
    )

    created_at = datetime.now(UTC)
    run_id = created_at.strftime("%Y%m%dT%H%M%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {
        "run_id": run_id,
        "code_commit_sha": code_commit_sha(cwd=Path.cwd()),
        "created_at_utc": created_at.isoformat(),
        "seed": SEED,
        "aoi_path": str(aoi_path),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "n_items": len(items),
        "reducers": list(reducers),
        "tile_origin_jitter": tile_origin_jitter,
        "tile_size": cfg.inference.tile_size,
        "tile_stride": tile_stride,
        "candidates": {},
    }
    if args.out_prefix is not None:
        prefix = args.out_prefix
    else:
        prefix = "burn_scar_smoke_wu10multi" if args.smoke else "burn_scar_wu10multi"
    for reducer, da in composites.items():
        cog_path = args.out_dir / f"{prefix}_{reducer}_{run_id}.tif"
        provenance = burn_scar.BurnScarRun(
            run_id=f"{run_id}_{reducer}",
            code_commit_sha=code_commit_sha(cwd=Path.cwd()),
            created_at_utc=created_at,
            model_id=handle.hf_model_id,
            model_version=handle.model_version,
            hf_revision_sha=handle.hf_revision_sha,
            terratorch_version=pkg_version("terratorch"),
            torch_version=pkg_version("torch"),
            device=handle.device,
            aoi_path=str(aoi_path),
            aoi_geometry_sha=aoi_sha,
            stac_catalog_url=PC_STAC_URL,
            window_start=start,
            window_end=end,
            s2_max_cloud_cover=cfg.inference.s2_max_cloud_cover,
            s2_item_ids=tuple(it.id for it in items),
            scl_mask_classes=cfg.inference.scl_mask_classes,
            reducer=reducer,
            season_start_month=cfg.inference.season_start_month,
            season_end_month=cfg.inference.season_end_month,
            tile_origin_jitter=tile_origin_jitter,
            tile_size=cfg.inference.tile_size,
            tile_stride=tile_stride,
            binarisation_threshold=cfg.inference.binarisation_threshold,
            output_crs=burn_scar.OUTPUT_CRS,
            resampling=burn_scar.RESAMPLING,
            nodata=burn_scar.NODATA,
            output_path=str(cog_path),
        )
        burn_scar.write_burn_scar_cog(da, cog_path, provenance)
        stats = _stats(da)
        cand: dict[str, object] = {**stats, "cog_path": str(cog_path)}
        summary["candidates"][reducer] = cand  # type: ignore[index]
        print(
            f"[multi] {reducer:>12}: median={stats['median']} mean={stats['mean']} "
            f"frac>=0.5={stats['frac_ge_05']}  -> {cog_path}",
            file=sys.stderr,
        )

    summary_path = args.out_dir / f"{prefix}_summary_{run_id}.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(f"[multi] summary: {summary_path}", file=sys.stderr)
    print(json.dumps({"summary": str(summary_path), "candidates": summary["candidates"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
