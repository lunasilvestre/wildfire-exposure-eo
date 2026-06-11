# Prompt 10 — Per-asset features + exposure rank (WU-6)

> **Drafted 2026-06-11 at the attended close-out checkpoint** per
> [`00_CLOSEOUT_PLAN.md`](00_CLOSEOUT_PLAN.md) ("write it first"). Executed as
> **WU-6** on the session-default (top) model — this WU owns the rank
> semantics. CLAUDE.md non-negotiable #6 applies to every docstring, log
> line, column name, and comment: *exposure*, *rank*, *relative*,
> *normalized* — never probability.

## Purpose

Implement `wildfire-exposure-eo score`: per-asset zonal features over the
WU-2 asset universe (per `docs/methodology.md` §10) composed into the
transparent linear exposure **rank** defined by `config/exposure_score.yaml`.
Two GeoParquet outputs: `features_{run_id}.parquet` and
`exposure_{run_id}.parquet`. This is the artefact the whole project exists to
produce; WU-7 validates it.

## Prerequisites (do not start without these)

- [ ] WU-2 osm assets parquet, WU-3 raster cache + manifest, WU-4 burns
      parquet, WU-5 fuel COG, WU-1 burn-scar COG all present. List each
      concrete path + sha you consume in the session log before computing.
- [ ] `config/exposure_score.yaml` v0.1.0 read end-to-end, including the
      normalisation and calibration comments.
- [ ] `data/taxonomy/critical_infrastructure.yaml` provides
      `buffer_radius_m` and `criticality_weight` per class.
- [ ] Read `CLAUDE.md`; #6 (rank, never probability) and #3 (provenance)
      dominate this WU.

## Pre-approved decisions (Nelson, 2026-06-11 checkpoint)

- **Dependencies**: adding `duckdb` + `exactextract` (pinned) is approved —
  justification: methodology §10 prescribes DuckDB-Spatial + exactextract
  for the assets × features zonal cross-product with a ≤10 ms/asset target;
  pure-Python loops are minutes/asset. If `exactextract` wheels fail on
  atlas, `rasterstats` (pinned) is the approved fallback; note the swap in
  the session log.
- **FWI feature**: check the audit's IPMA row first. If there is no GREEN,
  documented, public programmatic source for daily FWI verifiable in
  session, take the approved fallback: bump `config/exposure_score.yaml` to
  `version: "0.2.0"` **dropping `fwi_p95_recent_season` and renormalising
  the remaining six weights proportionally to sum exactly 1.0**, with a
  dated changelog comment in the YAML. Do not invent an endpoint; do not
  silently re-weight outside this rule.
- **`recent_burn_share_12mo` threshold**: binarise the WU-1 max-composite
  COG at `prob ≥ 0.5`, computing the share of buffer pixels above
  threshold. Per the WU-1 caveat (179-scene max-composite retains
  single-scene false positives; pilot median ≈ 0.47) this share is an
  upward-biased *relative rank input*, not a burned-area estimate — say so
  in the feature docstring and carry the threshold in provenance.

## Deliverables

1. **`src/wildfire_exposure_eo/features.py`** — per-feature pure functions,
   each `(assets_gdf, source, window: DateRange, …) -> pd.Series` indexed by
   `asset_id`, computed over the class-specific buffer
   (`ST_Buffer` in EPSG:32629, radius from taxonomy):
   - `fuel_class_severity_weight` — zonal **mean** of WU-5 severity band.
   - `canopy_height_p90_m` — zonal **p90** of ETH GCH.
   - `slope_max_deg` — zonal **max** of slope derived from Cop-DEM GLO-30
     (resolve via the WU-3 cache or the committed STAC manifest machinery;
     slope computed once on the pilot grid, documented method, e.g. Horn).
   - `historical_burn_share` — area share of buffer intersecting ICNF
     polygons with `vintage ≤ window.end` (WU-4 parquet; geometric overlay
     in EPSG:32629).
   - `recent_burn_share_12mo` — thresholded share per the pre-approved rule.
   - `nbr_delta_recent` — spring vs. late-summer median-NBR delta from S2
     L2A within `window`, reusing the existing STAC resolver + stackstac
     machinery (deterministic item ordering; item IDs logged and carried in
     provenance).
   - `fwi_p95_recent_season` — only if the IPMA source verifies (see above).
   Every function takes the window explicitly; nothing reads "now".

2. **`src/wildfire_exposure_eo/scoring.py`** —
   `compose_exposure(features_df, config: ExposureConfig) -> pd.DataFrame`:
   percentile-rank each feature within the AOI (ties: average rank), apply
   the YAML weights (assert sum == 1.0 — also add the CI assert the YAML
   header promises if absent), output `exposure_score ∈ [0,1]`
   (percentile-ranked composite) and integer `exposure_rank` (1 = most
   exposed). Missing feature values: rank from available values; row carries
   `features_present: list[str]` — never imputed silently.

3. **Schemas** — `schemas/scored_asset.py`: `AssetFeatures`,
   `ScoredAsset` (asset_id, asset_class, criticality_weight, features
   nested, exposure_score, exposure_rank, `features_present`, provenance).
   `ScoredAssetProvenance` carries: `model_version` (= exposure config
   version), `config_sha` (exposure YAML), `crosswalk_sha`, `run_id`,
   `code_commit_sha`, `window_start/end`, source artefact shas (osm parquet,
   burns parquet, fuel COG, burn-scar COG, GCH/DEM cache entries), S2 item
   ID list for `nbr_delta_recent`, and the burn-share threshold. This is the
   README's provenance contract — every row carries it (#3).

4. **CLI** `score` with `--aoi`, `--window-end YYYY-MM-DD` (**required** —
   WU-7's leakage rule depends on it; the 12-month and seasonal windows
   derive from it), `--features-out`, `--exposure-out`, `--smoke`.

5. **Tests** — unit per feature function on synthetic fixtures (known
   geometry → hand-computable zonal value); rank composition tests
   (weights sum, rank ordering invariant under monotone feature transforms,
   missing-feature path); property test: percentile ranks always in [0,1]
   and order-preserving. Integration: smoke-AOI end-to-end with fixture
   rasters, every row `ScoredAsset.model_validate`s. Performance check on
   the pilot run: log ms/asset; flag if > 10 ms/asset (do not "fix" by
   sampling assets).

6. **CI**: extend `validate-schemas` to spot-check a committed sample
   exposure row. **`prompts/_session_log.md`** entry.

## Constraints

- **Language discipline (#6).** `exposure_score`, `exposure_rank`; the
  docstring of `compose_exposure` states verbatim: "a relative,
  AOI-normalised screening rank — not a probability of fire."
- **Backdatable by construction.** Every feature respects `--window-end`;
  if an input artefact cannot honour the window (the WU-1 burn-scar COG is
  fixed at 2025-06→2026-06), the feature returns null for out-of-window
  requests and `features_present` reflects it. WU-7 relies on this for the
  leakage-clean validation run.
- **CRS explicit**: assets buffered in EPSG:32629; each raster read in its
  native CRS and the zonal join reprojected explicitly once.
- **Determinism**: seed 42 where any RNG appears; deterministic asset
  ordering `(asset_class, osm_type, osm_id)` in outputs.

## Test gates

The four standard gates, plus `validate-schema` on the exposure parquet, plus
`uv run wildfire-exposure-eo score --smoke --window-end 2026-06-01`.

## Verification (before declaring done)

1. Smoke run, then pilot run with `--window-end` = today; log wall-clock and
   ms/asset.
2. Pick 3 assets (highest rank, median, lowest); for each, hand-recompute
   the composite from the features parquet with the YAML weights and confirm
   the score matches (paste the arithmetic into the session log).
3. Confirm a backdated pilot run (`--window-end 2024-12-31`) completes, with
   `recent_burn_share_12mo` (and FWI if present) correctly null/absent in
   `features_present`.
4. `duckdb` sanity: rank distribution ~uniform on [0,1]; no NaN scores; row
   count == WU-2 asset count.

## Out of scope

- Portfolio aggregation (`portfolio_aggregation` in the YAML) — document as
  future work; do not implement.
- Any calibration claims; any per-asset maps (WU-8).

## Done when

Gates green; smoke + current + backdated pilot runs verified; sample row
committed for CI; session log updated; pushed with CI green.
