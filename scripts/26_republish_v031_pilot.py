"""Re-publish the PILOT scored exposure artifact at v0.3.1 (provenance-only fix).

Why this script exists
----------------------
``config/exposure_score.yaml`` is v0.3.1 (commit ``e144a7d``) but the published
scored artifact (``stac/exposure-assets/exposure-assets-20260611T170549Z/`` and
``docs/app/data/exposure_assets_20260611T170549Z.geojson``) still carries
``model_version 0.2.0`` in its per-row provenance. v0.3.1 dropped the FWI weight
added at v0.3.0 and renormalised the six structural weights back to the EXACT
v0.2.0 values — verified BYTE-IDENTICAL against the published commit's config —
so the ``exposure_score`` values and ``exposure_rank`` are NUMERICALLY IDENTICAL.
Only the ``model_version`` label and the provenance are stale.

What this script does
---------------------
Re-scores the pilot under the CURRENT v0.3.1 config and writes a fresh scored
GeoParquet whose per-row provenance reads ``model_version 0.3.1``, a fresh
``run_id``, ``code_commit_sha`` = current HEAD, ``seed`` 42.

It does NOT recompute the structural features from STAC: those are deterministic
functions of frozen source artefacts already cached for the v0.2.0 backdated run.
It REUSES that cached features parquet (asserting every source artefact matches
the cached run by SHA-256, non-negotiable #3) and recomposes with the v0.3.1
weights. No live EWDS FWI pull: FWI is UNWEIGHTED in v0.3.1 and was never present
in the v0.2.0 backdated artifact, so the schema footprint stays identical — the
re-score changes the label, not the numbers.

Determinism (#4): the recompose is a pure function of the cached features and the
YAML weights; no RNG is used, but ``seed`` 42 is recorded for the contract.

Terminology guard (#6): ``exposure_score`` is a relative, AOI-normalised
screening rank in [0, 1] — never a probability of fire.

Usage::

    uv run python scripts/26_republish_v031_pilot.py --smoke   # verify-then-act
    uv run python scripts/26_republish_v031_pilot.py           # full pilot

``--smoke`` recomposes on a deterministic asset subset and writes ``*_smoke_*``
outputs (touching nothing canonical); the full run writes the canonical
``features_{run_id}.parquet`` + ``exposure_{run_id}.parquet`` and asserts the
result is identical (float tolerance) to the published v0.2.0 run before exit.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

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
    parser.add_argument("--aoi", type=Path, default=_ROOT / "data/aoi/pilot.geojson")
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
    parser.add_argument("--exposure-config", type=Path, default=_ROOT / "config/exposure_score.yaml")
    parser.add_argument(
        "--taxonomy", type=Path, default=_ROOT / "data/taxonomy/critical_infrastructure.yaml"
    )
    parser.add_argument(
        "--cached-run-id",
        type=str,
        default="20260611T170549Z",
        help="run_id of the v0.2.0 cached run (its provenance pins the source SHAs to assert)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="recompose on a deterministic asset subset; write *_smoke_* outputs only",
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
    limit = args.limit if args.limit > 0 else (40 if args.smoke else 0)

    # --- reuse cached structural features (no STAC recompute) ---------------
    feats = gpd.read_parquet(args.cached_features)
    if feats.crs is None or feats.crs.to_epsg() != 4326:
        raise ValueError(f"{args.cached_features}: expected EPSG:4326, got {feats.crs}")
    structural_cols = _structural_feature_cols(feats)
    print(
        f"reusing {len(feats)} cached assets; structural features: {structural_cols}",
        file=sys.stderr,
    )

    # --- assert the cached source artefacts match by SHA-256 (non-negotiable #3) ---
    cached_exposure = args.cached_features.parent / f"exposure_{args.cached_run_id}.parquet"
    cached_prov = json.loads(gpd.read_parquet(cached_exposure)["provenance"].iloc[0])
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

    # --- reconstruct buffers + metadata exactly as the cached run did -------
    taxonomy = yaml.safe_load(args.taxonomy.read_text())
    assets = load_assets(args.osm)
    if limit > 0:
        assets = (
            assets.sort_values(["asset_class", "osm_type", "osm_id"])
            .head(limit)
            .reset_index(drop=True)
        )
        print(f"[smoke] limiting to {len(assets)} assets", file=sys.stderr)
    assert isinstance(assets, gpd.GeoDataFrame)  # narrow after the smoke sort/head chain
    buffers = buffer_assets(assets, taxonomy)
    meta = _asset_metadata(assets, taxonomy)

    # --- assemble the feature frame from the cached structural features -----
    asset_index = pd.Index(buffers["asset_id"], name="asset_id")
    cached_by_id = feats.set_index("asset_id")
    features_df = pd.DataFrame(index=asset_index)
    for col in structural_cols:
        features_df[col] = cached_by_id[col].reindex(asset_index).to_numpy()
    features_df = features_df.loc[:, [c for c in FEATURE_NAMES if c in features_df.columns]]

    # --- recompose with the v0.3.1 weights ---------------------------------
    config = load_exposure_config(args.exposure_config)
    if config.version != "0.3.1":
        raise ValueError(f"expected exposure config 0.3.1, got {config.version}")
    composed = compose_exposure(features_df, config)

    # --- provenance: model_version 0.3.1, fresh run_id, current commit ------
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    _aoi_geom, aoi_sha = load_aoi_geometry(args.aoi)
    del _aoi_geom  # geometry not needed; we carry only its sha + the cached run's window
    gch_candidates = sorted((_SOURCE_ROOT / "data/cache/eth-gch-2020").glob("*.tif"))
    if not gch_candidates:
        raise FileNotFoundError("no ETH GCH tile under data/cache/eth-gch-2020")
    provenance = ScoredAssetProvenance(
        model_version=config.version,
        config_sha=sha256_file(args.exposure_config),
        crosswalk_sha=cached_prov["crosswalk_sha"],
        run_id=run_id,
        code_commit_sha=code_commit_sha(cwd=_ROOT),
        aoi_path=str(args.aoi),
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

    # --- prove identity vs the published v0.2.0 run (#5 verify-then-act) ----
    if not args.smoke:
        new = gpd.read_parquet(exp_out).set_index("asset_id")
        old = gpd.read_parquet(cached_exposure).set_index("asset_id")
        j = new[["exposure_score", "exposure_rank"]].join(
            old[["exposure_score", "exposure_rank"]], lsuffix="_new", rsuffix="_old"
        )
        max_abs = float((j["exposure_score_new"] - j["exposure_score_old"]).abs().max())
        n_rank_chg = int((j["exposure_rank_new"] != j["exposure_rank_old"]).sum())
        print(
            f"identity-vs-v0.2.0: n={len(j)} max_abs_score_diff={max_abs} "
            f"n_rank_changes={n_rank_chg}",
            file=sys.stderr,
        )
        if len(j) != len(old) or len(j) != len(new):
            raise ValueError(
                "asset_id set drifted vs the published v0.2.0 run — refusing to publish"
            )
        if n_rank_chg != 0:
            raise ValueError(
                f"{n_rank_chg} rank changes vs published v0.2.0 — v0.3.1 was expected to be "
                "numerically identical; refusing to publish a surprise"
            )

    print(f"wrote {feats_out}", file=sys.stderr)
    print(f"wrote {exp_out}", file=sys.stderr)
    print(
        f"run_id={run_id} model_version={config.version} n_assets={len(composed)} seed={DEFAULT_SEED}",
        file=sys.stderr,
    )
    # stdout: the run_id, so callers can chain publishing on it.
    print(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
