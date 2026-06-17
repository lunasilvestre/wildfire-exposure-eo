"""Operational refresh spine — the every-2-days "assets to watch" decision product (WU-26).

A single orchestrator that crosses the repo's two axes and republishes the fast
one:

1. **Refresh FWI (fast axis).** Pull the latest published EWDS FWI day, regenerate
   the six display COGs (reusing ``scripts/25_make_fwi_cogs.py`` — imported, not
   duplicated), upload them to Cloudflare R2, and patch the ``fwi_overlay`` block
   of ``docs/app/data/style_data.json`` with the new ``valid_date`` + component
   hrefs/ranges. The validated structural axis is untouched between refreshes.
2. **Watch-list two-axis join (the decision product).** For each pilot scored
   asset, sample the CURRENT FWI at the asset's location and compute a transparent
   triage priority ``watch_priority = exposure_score * clip(fwi/50, 0, 1)`` (see
   :mod:`wildfire_exposure_eo.operational`). Emit the top-N + the full join as
   GeoParquet + JSON + a human-readable Markdown table.
3. **Provenance per run.** Every artefact carries the run_id, code_commit_sha,
   fwi_valid_date, FWI DOI, model_version, seed (non-negotiable #3 / #4).
4. **Graceful failure.** If EWDS is down / returns no data, the last-good
   artefacts are KEPT and the script exits non-zero WITHOUT publishing empty or
   garbage output (logged clearly). FWI is never imputed.

Honest framing (CLAUDE.md non-negotiable #6 + #9): the watch list is OPERATIONAL
TRIAGE — "validated high-exposure assets currently under elevated OBSERVED fire
weather, prioritise monitoring". It is NOT a forecast, NOT a probability, NOT a
prediction of ignition; FWI is observed reanalysis (~2-day lag, 0.25° regional).

Credentials (security): the EWDS key is read from ``CDSAPI_KEY`` or
``~/.cdsapirc`` and is NEVER printed, logged, or written to any artefact.

Usage::

    uv run python scripts/26_operational_refresh.py                 # pilot, live
    uv run python scripts/26_operational_refresh.py --aoi monchique # another AOI
    uv run python scripts/26_operational_refresh.py --no-upload     # skip R2 push
    uv run python scripts/26_operational_refresh.py --no-style      # skip style_data patch
    uv run python scripts/26_operational_refresh.py --top-n 30
    uv run python scripts/26_operational_refresh.py --smoke         # offline config check, exit 0
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Repo-root import shim so the script runs from anywhere.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from wildfire_exposure_eo.fire_weather import (
    EwdsFwiSurface,
    ewds_fwi_provenance,
    load_ewds_fwi_config,
    load_ewds_key,
)
from wildfire_exposure_eo.operational import (
    FWI_REF,
    FWI_REF_RATIONALE,
    WATCH_PRIORITY_FORMULA,
    build_watch_list,
    sample_fwi_at_points,
    watch_list_markdown,
)
from wildfire_exposure_eo.schemas import GeobrowserStyleData, WatchListItem, WatchListRun
from wildfire_exposure_eo.stac import code_commit_sha, load_aoi_geometry

if TYPE_CHECKING:
    import geopandas as gpd

_AOI_DIR = _ROOT / "data" / "aoi"
_CONFIG_PATH = _ROOT / "config" / "fire_weather.yaml"
_GEOBROWSER_DIR = _ROOT / "outputs" / "geobrowser"
_STAC_EXPOSURE_DIR = _ROOT / "stac" / "exposure-assets"
_DOCS_DATA = _ROOT / "docs" / "app" / "data"
_STYLE_DATA = _DOCS_DATA / "style_data.json"
_WATCH_DIR = _ROOT / "outputs" / "watch_list"

#: Default seed (non-negotiable #4); no RNG is used, threaded for contract uniformity.
DEFAULT_SEED = 42

#: Cloudflare R2 remote + bucket the FWI COGs are uploaded to (rclone ``r2:``
#: remote; custom domain ``wildfire.cheias.pt``). Matches scripts/15's asset base.
_R2_BUCKET = "r2:wildfire-exposure-eo"


def _load_script_25() -> Any:
    """Import ``scripts/25_make_fwi_cogs.py`` as a module (its name starts with a digit).

    Reuses scripts/25's COG-generation logic without duplicating it (the prompt's
    explicit requirement). The module has no network side effects at import.
    """
    path = _ROOT / "scripts" / "25_make_fwi_cogs.py"
    spec = importlib.util.spec_from_file_location("_fwi_cogs_25", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Step 1 — refresh the FWI COGs (reuse scripts/25), upload to R2, patch style.
# ---------------------------------------------------------------------------
def refresh_fwi_cogs(
    *,
    start: date,
    out_dir: Path,
    manifest_path: Path,
    key: str,
) -> tuple[EwdsFwiSurface, list[dict[str, object]], dict[str, Any]]:
    """Pull the latest EWDS FWI day and (re)write the six display COGs + manifest.

    Delegates to scripts/25 for the AOI-union bbox, the latest-available-day walk,
    and the per-component COG writer (no duplication). Raises on EWDS-down / no
    data so the caller can fail gracefully WITHOUT having touched live artefacts.
    Returns ``(surface, written_components, manifest_dict)``.
    """
    s25 = _load_script_25()
    config = load_ewds_fwi_config(_CONFIG_PATH)
    available = {v.feature_name for v in config.variables}
    missing = [feat for feat, _ in s25._OVERLAY_COMPONENTS if feat not in available]
    if missing:
        raise ValueError(f"overlay components missing from ewds config: {missing}")

    bbox = s25.union_bbox_with_margin()
    print(
        f"[refresh] EWDS union+{s25._MARGIN_DEG}deg bbox = {tuple(round(c, 4) for c in bbox)}",
        file=sys.stderr,
    )
    # latest_available_surface raises (EWDS down / unpublished) or returns a
    # non-null surface; an all-null published day is skipped inside it.
    surface = s25.latest_available_surface(bbox, start, _CONFIG_PATH, key=key)
    valid = surface.valid_date.isoformat()
    print(f"[refresh] FWI valid date: {valid}", file=sys.stderr)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, object]] = []
    for feature_name, token in s25._OVERLAY_COMPONENTS:
        vmin, vmax = s25.fwi_component_value_range(surface, feature_name)
        dst = out_dir / f"fwi_{token}_3857_{valid}.tif"
        s25.write_fwi_component_cog(surface, feature_name, dst)
        size_mb = dst.stat().st_size / 1e6
        print(
            f"[refresh]   {token:5s} -> {dst.name} ({size_mb:.3f} MB; "
            f"range {vmin:.2f}..{vmax:.2f})",
            file=sys.stderr,
        )
        written.append(
            {
                "component": token,
                "feature_name": feature_name,
                "filename": dst.name,
                "value_min": round(vmin, 4),
                "value_max": round(vmax, 4),
            }
        )

    provenance = ewds_fwi_provenance(config, surface)  # no key in here
    manifest = {
        "fwi_valid_date": valid,
        "requested_date": surface.requested_date.isoformat(),
        "extent_bbox_4326": [round(c, 6) for c in bbox],
        "extent_source_aois": list(s25._CANONICAL_AOIS),
        "extent_margin_deg": s25._MARGIN_DEG,
        "display_crs": "EPSG:3857",
        "components": written,
        "provenance": provenance,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"[refresh] wrote {len(written)} COGs + manifest {manifest_path.name}",
        file=sys.stderr,
    )
    return surface, written, manifest


def upload_cogs_to_r2(components: list[dict[str, object]], out_dir: Path, bucket: str) -> None:
    """Upload the freshly written FWI COGs to Cloudflare R2 via the ``r2:`` rclone remote.

    Each COG is copied to ``<bucket>/<filename>`` (the geobrowser reads them at
    ``https://wildfire.cheias.pt/<filename>``). Raises ``RuntimeError`` if rclone
    is unavailable or any copy fails — the caller treats that as a refresh failure
    and keeps the last-good style_data (does not point it at unuploaded COGs).
    """
    if shutil.which("rclone") is None:
        raise RuntimeError(
            "rclone not found on PATH — cannot upload FWI COGs to R2. Install rclone "
            "and configure the 'r2:' remote, or pass --no-upload."
        )
    for comp in components:
        name = str(comp["filename"])
        src = out_dir / name
        dst = f"{bucket}/{name}"
        print(f"[refresh] rclone copyto {src.name} -> {dst}", file=sys.stderr)
        result = subprocess.run(
            ["rclone", "copyto", str(src), dst],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"rclone upload failed for {name} (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
    print(f"[refresh] uploaded {len(components)} COGs to {bucket}", file=sys.stderr)


def patch_style_fwi_overlay(style_path: Path, manifest_path: Path, asset_base: str) -> str:
    """Patch ONLY the ``fwi_overlay`` block of an existing style_data.json.

    Reuses scripts/15's :func:`build_fwi_overlay` to construct the overlay from
    the fresh manifest, swaps it into the committed style_data, and re-validates
    the whole document against :class:`GeobrowserStyleData` before writing — so
    the refresh updates the fast axis (new ``valid_date`` + component hrefs)
    without re-deriving the validated structural blocks. Returns the new
    ``valid_date``. Raises if the style file or manifest is missing.
    """
    s15 = _load_script_15()
    if not style_path.exists():
        raise FileNotFoundError(
            f"style_data.json not found at {style_path} — run scripts/15 once first "
            "to build the full bundle, then refresh the FWI overlay."
        )
    overlay = s15.build_fwi_overlay(manifest_path, asset_base)
    if overlay is None:
        raise RuntimeError(f"no FWI manifest at {manifest_path} — cannot patch overlay")
    doc = json.loads(style_path.read_text())
    doc["fwi_overlay"] = overlay.model_dump()
    # Re-validate the whole document so a malformed patch fails loudly (#schema).
    style = GeobrowserStyleData.model_validate(doc)
    style_path.write_text(style.model_dump_json(indent=1) + "\n")
    print(
        f"[refresh] patched style_data.json fwi_overlay -> valid {overlay.valid_date}",
        file=sys.stderr,
    )
    return overlay.valid_date


def _load_script_15() -> Any:
    """Import ``scripts/15_make_geobrowser_data.py`` for its ``build_fwi_overlay`` helper."""
    path = _ROOT / "scripts" / "15_make_geobrowser_data.py"
    spec = importlib.util.spec_from_file_location("_geobrowser_15", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Step 2 — load scored assets, sample current FWI, build the watch list.
# ---------------------------------------------------------------------------
def _latest_exposure_parquet() -> Path:
    """Newest committed scored-asset GeoParquet under ``stac/exposure-assets/``.

    The validated structural ranks the watch list crosses live in the published
    STAC item's GeoParquet (authoritative, full per-row provenance). Run-ids sort
    lexically; the newest wins.
    """
    cands = sorted(_STAC_EXPOSURE_DIR.glob("exposure-assets-*/exposure_*.parquet"))
    if not cands:
        raise FileNotFoundError(
            f"no scored-asset GeoParquet under {_STAC_EXPOSURE_DIR} — run the scoring "
            "pipeline + publish-stac first."
        )
    return cands[-1]


def load_scored_assets(parquet_path: Path) -> gpd.GeoDataFrame:
    """Load the scored-asset GeoParquet (EPSG:4326 asserted) for the join."""
    import geopandas as gpd

    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{parquet_path.name}: expected EPSG:4326, got {gdf.crs} (#2)")
    return gdf


def _model_version_from_provenance(gdf: gpd.GeoDataFrame, default: str) -> str:
    """Read ``model_version`` from the scored-asset per-row provenance (no fabrication).

    The watch-list provenance must reflect the structural rank actually consumed.
    Falls back to ``default`` only if the column/key is absent.
    """
    if "provenance" not in gdf.columns or len(gdf) == 0:
        return default
    raw = gdf["provenance"].iloc[0]
    try:
        prov = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, ValueError):
        return default
    return str(prov.get("model_version", default))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aoi",
        default="pilot",
        help="AOI slug under data/aoi/ to sample FWI for (default: pilot)",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="latest date to try for the EWDS pull (ISO); walks back to the newest published day",
    )
    parser.add_argument("--top-n", type=int, default=25, help="rows in the Markdown brief")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="deterministic seed (#4)")
    parser.add_argument(
        "--out-dir", type=Path, default=_WATCH_DIR, help="directory for the watch-list artefacts"
    )
    parser.add_argument(
        "--no-upload", action="store_true", help="skip the R2 upload of the FWI COGs"
    )
    parser.add_argument(
        "--no-style", action="store_true", help="skip patching docs/app/data/style_data.json"
    )
    parser.add_argument(
        "--asset-base-url",
        default="https://wildfire.cheias.pt",
        help="public base URL (Cloudflare R2) the geobrowser reads the FWI COGs from",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="offline: validate config + AOI + scored assets exist; no network/key; exit 0",
    )
    args = parser.parse_args()

    aoi_path = _AOI_DIR / f"{args.aoi}.geojson"
    if not aoi_path.exists():
        print(f"[refresh] AOI not found: {aoi_path}", file=sys.stderr)
        return 2

    if args.smoke:
        config = load_ewds_fwi_config(_CONFIG_PATH)
        geom, _ = load_aoi_geometry(aoi_path)
        exposure_pq = _latest_exposure_parquet()
        print(
            f"[smoke] config OK ({len(config.variables)} FWI vars), AOI {args.aoi} loads "
            f"(bounds {tuple(round(c, 3) for c in geom.bounds)}), scored assets at "
            f"{exposure_pq.name}; formula: {WATCH_PRIORITY_FORMULA}",
            file=sys.stderr,
        )
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    commit_sha = code_commit_sha(cwd=_ROOT)

    # --- Step 1: refresh FWI COGs (graceful failure if EWDS is down / no data) ---
    try:
        key = load_ewds_key()  # CDSAPI_KEY env or ~/.cdsapirc; never printed
        surface, components, manifest = refresh_fwi_cogs(
            start=date.fromisoformat(args.date),
            out_dir=_GEOBROWSER_DIR,
            manifest_path=_GEOBROWSER_DIR / "fwi_overlay_manifest.json",
            key=key,
        )
    except Exception as exc:
        print(
            f"[refresh] FWI refresh FAILED ({type(exc).__name__}: {exc}). "
            "Keeping last-good artefacts; nothing published. (EWDS may be down or the "
            "latest day is not yet available — never imputed.)",
            file=sys.stderr,
        )
        return 1

    fwi_prov = manifest["provenance"]

    # --- Step 1b: upload COGs + patch the style_data fwi_overlay block ---
    if not args.no_upload:
        try:
            upload_cogs_to_r2(components, _GEOBROWSER_DIR, _R2_BUCKET)
        except Exception as exc:
            print(
                f"[refresh] R2 upload FAILED ({type(exc).__name__}: {exc}). "
                "NOT patching style_data (would reference unuploaded COGs); last-good kept.",
                file=sys.stderr,
            )
            return 1
    else:
        print("[refresh] --no-upload: skipping R2 push", file=sys.stderr)

    if not args.no_style:
        try:
            patch_style_fwi_overlay(
                _STYLE_DATA,
                _GEOBROWSER_DIR / "fwi_overlay_manifest.json",
                args.asset_base_url,
            )
        except Exception as exc:
            print(
                f"[refresh] style_data patch FAILED ({type(exc).__name__}: {exc}). "
                "Last-good style_data kept.",
                file=sys.stderr,
            )
            return 1
    else:
        print("[refresh] --no-style: skipping style_data patch", file=sys.stderr)

    # --- Step 2: build the watch list (two-axis join) ---
    exposure_pq = _latest_exposure_parquet()
    assets = load_scored_assets(exposure_pq)
    exposure_run_id = exposure_pq.stem.replace("exposure_", "")
    model_version = _model_version_from_provenance(assets, default="unknown")

    fwi_surface = surface.components["fwi_fwi_current"]  # the headline FWI
    fwi_series = sample_fwi_at_points(fwi_surface, assets)
    df = build_watch_list(assets, fwi_series, ref=FWI_REF)

    # Validate every row against the schema before writing (non-negotiable #schema).
    for record in df.to_dict(orient="records"):
        WatchListItem.model_validate(record)
    n_with_fwi = int(df["watch_priority"].notna().sum())

    run_id = surface.valid_date.strftime("%Y%m%dT000000Z")  # anchored on the FWI valid day
    run = WatchListRun(
        run_id=run_id,
        code_commit_sha=commit_sha,
        model_version=model_version,
        seed=args.seed,
        aoi_name=args.aoi,
        aoi_path=str(aoi_path.relative_to(_ROOT)),
        exposure_run_id=exposure_run_id,
        formula=WATCH_PRIORITY_FORMULA,
        fwi_ref=FWI_REF,
        fwi_ref_rationale=FWI_REF_RATIONALE,
        fwi_valid_date=str(fwi_prov["fwi_valid_date"]),
        fwi_requested_date=str(fwi_prov["fwi_requested_date"]),
        fwi_product_id=str(fwi_prov["fwi_product_id"]),
        fwi_doi=str(fwi_prov["fwi_doi"]),
        fwi_dataset_type=str(fwi_prov["fwi_dataset_type"]),
        fwi_system_version=str(fwi_prov["fwi_system_version"]),
        fwi_attribution=str(fwi_prov["fwi_attribution"]),
        fwi_lag_note="~2-day lag",
        n_assets=len(df),
        n_with_fwi=n_with_fwi,
        top_n=args.top_n,
    )

    _write_watch_artifacts(df, run, assets, args.out_dir)

    top = df.iloc[0] if len(df) else None
    if top is not None and top.get("watch_priority") is not None:
        print(
            f"[refresh] watch list: {len(df)} assets, FWI valid {run.fwi_valid_date}; "
            f"top = {top['asset_class']} (rank #{int(top['exposure_rank'])}, "
            f"FWI {float(top['fwi_current']):.1f}, "
            f"watch_priority {float(top['watch_priority']):.4f})",
            file=sys.stderr,
        )
    else:
        print(
            f"[refresh] watch list: {len(df)} assets, FWI valid {run.fwi_valid_date}; "
            "no asset had covered FWI (all uncovered — not imputed)",
            file=sys.stderr,
        )
    return 0


def _write_watch_artifacts(
    df: Any, run: WatchListRun, assets: gpd.GeoDataFrame, out_dir: Path
) -> None:
    """Write the watch list as GeoParquet + JSON + Markdown (all with run provenance)."""
    import geopandas as gpd

    run_id = run.run_id
    # GeoParquet: re-attach the asset representative-point geometry (EPSG:4326).
    geom_by_id = assets.set_index("asset_id").geometry.representative_point()
    gdf = gpd.GeoDataFrame(
        df.copy(),
        geometry=[geom_by_id.loc[aid] for aid in df["asset_id"]],
        crs="EPSG:4326",
    )
    pq_path = out_dir / f"watch_list_{run.aoi_name}_{run_id}.parquet"
    gdf.to_parquet(pq_path)

    json_path = out_dir / f"watch_list_{run.aoi_name}_{run_id}.json"
    payload = {
        "run": run.model_dump(),
        "items": df.to_dict(orient="records"),
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")

    md_path = out_dir / f"watch_list_{run.aoi_name}_{run_id}.md"
    md = watch_list_markdown(
        df,
        top_n=run.top_n,
        run_id=run_id,
        fwi_valid_date=run.fwi_valid_date,
        formula=run.formula,
        ref=run.fwi_ref,
    )
    md_path.write_text(md)
    print(
        f"[refresh] wrote watch list: {pq_path.name}, {json_path.name}, {md_path.name}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
