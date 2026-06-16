"""Operational v0.3.0 pilot re-score with current-season EWDS FWI (prompt: WU v0.3.0).

The v0.3.0 calibration (config/exposure_score.yaml, Nelson 2026-06-16) RESTORES
the fire-weather dimension dropped at 0.2.0, now sourced from the verified CEMS
EWDS ``cems-fire-historical-v1`` current-season FWI (DOI 10.24381/cds.0e89c522).
``fwi_fwi_current`` takes a 0.10 weight; the six FWI components and three topology
features stay AVAILABLE-but-UNWEIGHTED.

This script re-scores the PILOT operationally, with CURRENT-season FWI. It does
NOT recompute the structural features (fuel, canopy, slope, historical-burn,
NBR-delta) from STAC — those are deterministic functions of frozen source
artefacts already cached for the v0.2.0 backdated run. It REUSES that cached
features parquet (asserting the source artefacts match by SHA-256, non-negotiable
#3) and adds ONE new dimension: the per-asset current-season FWI system pulled
live from EWDS for the latest available date (~2-day lag; today the data store
reported 2026-06-11 as the latest). The seven FWI components are carried per
asset (AVAILABLE); only ``fwi_fwi_current`` is weighted by the v0.3.0 config.

The result is the v0.3.0 pilot scored GeoParquet (features + exposure) under
``outputs/parquet/`` with the full ScoredAsset provenance contract:
``model_version 0.3.0``, a fresh ``run_id``, the current ``code_commit_sha``, the
EWDS ``fwi_valid_date`` + DOI, and ``seed 42``.

METHODOLOGY GUARD (CLAUDE.md): current FWI is NOT validated against historical
ICNF burns (temporal mismatch — today's fire weather cannot explain 2017-2024
burns). Backdated FWI validation is a separate, pending step. This script only
produces the operational re-score; it computes NO validation metrics.

Usage::

    uv run python scripts/23_rescore_v030_pilot.py --smoke   # 1km tile, proves path
    uv run python scripts/23_rescore_v030_pilot.py           # full pilot
    uv run python scripts/23_rescore_v030_pilot.py --fwi-date 2026-06-11
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

# Repo-root import shim so the script runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wildfire_exposure_eo.features import (
    _asset_metadata,
    _write_outputs,
    buffer_assets,
    load_assets,
    sha256_file,
)
from wildfire_exposure_eo.fire_weather import (
    build_ewds_fwi_surface,
    ewds_fwi_components,
    ewds_fwi_provenance,
    load_ewds_fwi_config,
    load_ewds_key,
)
from wildfire_exposure_eo.schemas.scored_asset import (
    FEATURE_NAMES,
    ScoredAsset,
    ScoredAssetProvenance,
)
from wildfire_exposure_eo.scoring import compose_exposure, load_exposure_config
from wildfire_exposure_eo.stac import code_commit_sha, load_aoi_geometry

_ROOT = Path(__file__).resolve().parents[1]
#: Cached input artefacts (OSM/burns/fuel/GCH/features parquet) live under the
#: MAIN repo's gitignored ``outputs/`` + ``data/cache/``; an isolated worktree has
#: no ``outputs/`` of its own. Inputs default to this source root; OUTPUTS are
#: always written under the worktree ``_ROOT`` so the branch carries the artefact.
_SOURCE_ROOT = Path("/home/nls/Documents/dev/wildfire-exposure-eo")
DEFAULT_SEED = 42


def _structural_feature_cols(features_gdf: gpd.GeoDataFrame) -> list[str]:
    """The cached structural feature columns present in the v0.2.0 features parquet."""
    return [c for c in FEATURE_NAMES if c in features_gdf.columns]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aoi", type=Path, default=_ROOT / "data/aoi/pilot.geojson", help="AOI GeoJSON"
    )
    parser.add_argument(
        "--cached-features",
        type=Path,
        default=_SOURCE_ROOT / "outputs/parquet/features_20260611T170549Z.parquet",
        help="v0.2.0 cached features parquet to reuse (structural features + geometry)",
    )
    parser.add_argument(
        "--osm",
        type=Path,
        default=_SOURCE_ROOT / "outputs/parquet/osm_assets_20260610T113143Z.parquet",
        help="WU-2 OSM asset GeoParquet (must match the cached run by SHA-256)",
    )
    parser.add_argument(
        "--burns",
        type=Path,
        default=_SOURCE_ROOT / "outputs/parquet/icnf_burns_20260610T164453Z.parquet",
    )
    parser.add_argument(
        "--fuel-cog",
        type=Path,
        default=_SOURCE_ROOT / "outputs/cogs/fuel_class_20260611T090120Z.tif",
    )
    parser.add_argument(
        "--exposure-config", type=Path, default=_ROOT / "config/exposure_score.yaml"
    )
    parser.add_argument(
        "--fire-weather-config", type=Path, default=_ROOT / "config/fire_weather.yaml"
    )
    parser.add_argument("--taxonomy", type=Path, default=_ROOT / "data/taxonomy/critical_infrastructure.yaml")
    parser.add_argument(
        "--cached-run-id",
        type=str,
        default="20260611T170549Z",
        help="run_id of the v0.2.0 cached run (its provenance pins the source SHAs to assert)",
    )
    parser.add_argument(
        "--fwi-date",
        type=str,
        default=None,
        help="requested EWDS date (ISO); default = latest available (probed via the data store)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "verify-then-act smoke: run the FULL path (cached reuse -> live EWDS pull -> "
            "zonal -> recompose -> write -> schema-validate) on a small asset subset, "
            "writing *_smoke_* outputs. Uses the pilot AOI for the EWDS grid because the "
            "0.25-degree EWDS grid is single-cell over the 1km tile (a 1x1 raster has no "
            "resolution for exactextract); the pilot is the smallest multi-cell footprint."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="cap assets scored (0 = all). --smoke implies --limit 40 if unset.",
    )
    args = parser.parse_args()

    import yaml

    tag = "_smoke" if args.smoke else ""
    # The EWDS 0.25-degree grid is single-cell (1x1, no resolution) over the 1km
    # smoke tile, so the smoke uses the pilot AOI for the FWI grid + a small asset
    # subset to still exercise the full path end-to-end.
    aoi_path = args.aoi
    limit = args.limit if args.limit > 0 else (40 if args.smoke else 0)

    # --- reuse cached structural features (no STAC recompute) ---------------
    feats = gpd.read_parquet(args.cached_features)
    if feats.crs is None or feats.crs.to_epsg() != 4326:
        raise ValueError(f"{args.cached_features}: expected EPSG:4326, got {feats.crs}")
    structural_cols = _structural_feature_cols(feats)
    print(f"reusing {len(feats)} cached assets; structural features: {structural_cols}", file=sys.stderr)

    # --- assert the cached source artefacts match by SHA-256 (non-negotiable #3) ---
    cached_prov = json.loads(
        gpd.read_parquet(
            args.cached_features.parent / f"exposure_{args.cached_run_id}.parquet"
        )["provenance"].iloc[0]
    )
    for label, path, key in [
        ("osm", args.osm, "osm_parquet_sha"),
        ("burns", args.burns, "burns_parquet_sha"),
        ("fuel", args.fuel_cog, "fuel_cog_sha"),
    ]:
        got = sha256_file(path)
        if got != cached_prov[key]:
            raise ValueError(
                f"{label} artefact SHA mismatch: {path.name} is {got} but the cached "
                f"v0.2.0 run used {cached_prov[key]} — refusing to re-score on drifted inputs"
            )
    print("cached source artefacts verified by SHA-256 (osm/burns/fuel)", file=sys.stderr)

    # --- reconstruct buffers exactly as the cached run did ------------------
    taxonomy = yaml.safe_load(args.taxonomy.read_text())
    assets = load_assets(args.osm)
    if limit > 0:
        # Deterministic subset (sorted by the same key buffer_assets/_asset_metadata
        # use) so the smoke is reproducible and exercises the full path.
        assets = (
            assets.sort_values(["asset_class", "osm_type", "osm_id"])
            .head(limit)
            .reset_index(drop=True)
        )
        print(f"[smoke] limiting to {len(assets)} assets", file=sys.stderr)
    buffers = buffer_assets(assets, taxonomy)
    meta = _asset_metadata(assets, taxonomy)

    # --- pull current EWDS FWI (latest available date) ---------------------
    ewds_config = load_ewds_fwi_config(args.fire_weather_config)
    aoi_geom, aoi_sha = load_aoi_geometry(aoi_path)
    key = load_ewds_key()  # CDSAPI_KEY env or ~/.cdsapirc; never printed/committed
    when = (
        date.fromisoformat(args.fwi_date)
        if args.fwi_date is not None
        else _latest_available_date(aoi_geom, ewds_config, key)
    )
    print(f"pulling EWDS FWI for {when.isoformat()} over {aoi_path.name}", file=sys.stderr)
    surface = build_ewds_fwi_surface(aoi_geom, when, ewds_config, key=key, seed=DEFAULT_SEED)
    ewds_series = ewds_fwi_components(buffers, surface, ewds_config)
    if ewds_series is None:
        raise RuntimeError(f"EWDS FWI surface is null for {when} — cannot re-score with current FWI")
    ewds_prov = ewds_fwi_provenance(ewds_config, surface)
    print(f"fwi_valid_date: {surface.valid_date.isoformat()}", file=sys.stderr)

    # --- assemble the feature frame (cached structural + current FWI) -------
    asset_index = pd.Index(buffers["asset_id"], name="asset_id")
    features_df = pd.DataFrame(index=asset_index)
    cached_by_id = feats.set_index("asset_id")
    for col in structural_cols:
        features_df[col] = cached_by_id[col].reindex(asset_index).to_numpy()
    for feat_name, series in ewds_series.items():
        features_df[feat_name] = series.reindex(asset_index).to_numpy()
    # Canonical column order.
    features_df = features_df[[c for c in FEATURE_NAMES if c in features_df.columns]]

    # --- recompose with the v0.3.0 weights ---------------------------------
    config = load_exposure_config(args.exposure_config)
    if config.version != "0.3.0":
        raise ValueError(f"expected exposure config 0.3.0, got {config.version}")
    composed = compose_exposure(features_df, config)

    # --- provenance: model_version 0.3.0, fresh run_id, current commit, FWI date ---
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    gch_candidates = sorted((_SOURCE_ROOT / "data/cache/eth-gch-2020").glob("*.tif"))
    if not gch_candidates:
        raise FileNotFoundError("no ETH GCH tile under data/cache/eth-gch-2020")
    provenance = ScoredAssetProvenance(
        model_version=config.version,
        config_sha=sha256_file(args.exposure_config),
        crosswalk_sha=cached_prov["crosswalk_sha"],
        run_id=run_id,
        code_commit_sha=code_commit_sha(cwd=_ROOT),
        aoi_path=str(args.aoi if not args.smoke else aoi_path),
        aoi_geometry_sha=aoi_sha,
        window_start=date.fromisoformat(cached_prov["window_start"]),
        window_end=date.fromisoformat(cached_prov["window_end"]),
        osm_parquet_sha=sha256_file(args.osm),
        burns_parquet_sha=sha256_file(args.burns),
        fuel_cog_sha=sha256_file(args.fuel_cog),
        gch_cache_sha=sha256_file(gch_candidates[0]),
        burn_scar_cog_sha=cached_prov.get("burn_scar_cog_sha"),
        dem_item_ids=tuple(cached_prov.get("dem_item_ids", ())),
        s2_item_ids=tuple(cached_prov.get("s2_item_ids", ())),
        burn_share_threshold=cached_prov["burn_share_threshold"],
        fwi_product_id=ewds_prov.get("fwi_product_id"),
        fwi_doi=ewds_prov.get("fwi_doi"),
        fwi_config_version=ewds_prov.get("fwi_config_version"),
        fwi_dataset_type=ewds_prov.get("fwi_dataset_type"),
        fwi_system_version=ewds_prov.get("fwi_system_version"),
        fwi_requested_date=ewds_prov.get("fwi_requested_date"),
        fwi_valid_date=ewds_prov.get("fwi_valid_date"),
        fwi_variable_map=dict(ewds_prov.get("fwi_variable_map", {})),
    )

    out_dir = _ROOT / "outputs/parquet"
    feats_out = out_dir / f"features{tag}_{run_id}.parquet"
    exp_out = out_dir / f"exposure{tag}_{run_id}.parquet"
    sample_row = _write_outputs(
        meta=meta,
        composed=composed,
        provenance=provenance,
        features_out=feats_out,
        exposure_out=exp_out,
    )
    ScoredAsset.model_validate(sample_row)  # fail loudly if the contract drifts

    print(f"wrote {feats_out}", file=sys.stderr)
    print(f"wrote {exp_out}", file=sys.stderr)
    print(
        f"run_id={run_id} model_version={config.version} n_assets={len(composed)} "
        f"fwi_valid_date={surface.valid_date.isoformat()}",
        file=sys.stderr,
    )
    return 0


def _latest_available_date(aoi_geom: Any, config: Any, key: str) -> date:
    """Probe the EWDS data store for its latest available date, parsed from its 400 error.

    The ``cems-fire-historical-v1`` process rejects an out-of-range request with a
    message that names the latest available date. We request today's date, read
    that message, and return the named date — no invented identifiers (#1).
    """
    import re

    today = datetime.now(UTC).date()
    try:
        build_ewds_fwi_surface(aoi_geom, today, config, key=key, seed=DEFAULT_SEED)
    except Exception as exc:  # noqa: BLE001 — parse the data-store message
        m = re.search(r"latest date available for this dataset is:\s*(\d{4}-\d{2}-\d{2})", str(exc))
        if m:
            return date.fromisoformat(m.group(1))
        raise
    return today  # today's data is already available


if __name__ == "__main__":
    raise SystemExit(main())
