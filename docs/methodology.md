# Methodology — end-to-end dry run

> Pre-development walk-through of the planned dev sequence. Each phase lists its **deliverable**, **prerequisites**, **anticipated stuck-points**, and **remediation**. Closes [`PRE_DEV_CHECKLIST.md`](../PRE_DEV_CHECKLIST.md) §I.1.

The point of this document is to *think the project through end-to-end before writing code*, identify where a future Claude Code session is likely to stall, and pre-position the prerequisite for each stall. A stuck CC session that interrupts a dev flow is the failure mode this avoids.

The sequence below assumes [`PRE_DEV_CHECKLIST.md`](../PRE_DEV_CHECKLIST.md) §A through §H complete (currently 29 of 38 items checked).

## Phase sequence

| # | Phase | Output artefact | Prompt file | Notes |
|---|---|---|---|---|
| 1 | Audit shipping | `audit` CLI + JSON report | `prompts/01_data_audit.md` | already drafted |
| 2 | OSM extraction | `outputs/parquet/osm_assets_<run_id>.parquet` | — | uses `data/taxonomy/critical_infrastructure.yaml` |
| 3 | STAC item resolution | `outputs/manifests/stac_<run_id>.json` | — | S2, S1, Cop-DEM, WorldCover; deterministic ordering |
| 4 | Static raster fetch | `data/cache/` (gitignored) | — | ETH GCH tiles, EFFIS fuel map |
| 5 | ICNF burns ingestion | `outputs/parquet/icnf_burns_<vintage>.parquet` | — | scripts/00_icnf_fetch.sh already shipped |
| 6 | Weak-label preparation | `outputs/cogs/weak_labels_<run_id>.tif` | — | COSc + COS join — see stuck-point §6 |
| 7 | Stage 1 — SegFormer baseline training | `checkpoints/fuel_segformer_b0_<run_id>.safetensors` | `prompts/02_train_fuel_baseline.md` | atlas, RTX 3090, ~4–6 hours |
| 8 | Stage 1 — foundation-model variant | side-by-side comparison report | `prompts/03_train_fuel_foundation.md` | optional, atlas, ~8–12 hours |
| 9 | Stage 1b — burn-scar inference | `outputs/cogs/burn_scar_<run_id>.tif` | `prompts/03_burn_scar_inference.md` | already drafted |
| 10 | Stage 2 — per-asset feature extraction | `outputs/parquet/features_<run_id>.parquet` | `prompts/04_score_assets.md` | DuckDB-Spatial + rasterio |
| 11 | Stage 2 — exposure score composition | `outputs/parquet/exposure_<run_id>.parquet` | included in `04_score_assets.md` | reads `config/exposure_score.yaml` |
| 12 | Validation | `docs/validation_report.md` + plots | `prompts/05_validate.md` | lift / Spearman / Brier vs ICNF |
| 13 | STAC catalog assembly | `stac/catalog.json` + per-collection items | `prompts/06_build_catalog.md` | mirrors `cheias-pt-stac` |
| 14 | Demo command | `uv run wildfire-exposure-eo demo` | `prompts/07_demo.md` | pretrained checkpoints, smoke + pilot |
| 15 | Documentation pass | `docs/limitations.md`, `docs/scaling.md`, training logs | — | last-mile polish |

## Anticipated stuck-points, by phase

### §3 — STAC item resolution

**Stuck-point.** Sentinel-2 cloud-cover filter wipes out the AOI's late-summer scenes in dry years; the model needs both wet- and dry-season composites. Naive `eo:cloud_cover < 30` may return fewer than 5 items for July–September.

**Remediation.** Two-pass query — strict 30 % cloud-cover for spring composite, relaxed 60 % cloud-cover for summer composite with explicit cloud-masking via S2 SCL band. Document the asymmetry in the run's provenance dict (per-composite cloud-cover thresholds).

### §6 — Weak-label preparation (COSc + COS join)

**Stuck-point.** This is the most complex piece of label engineering in the pipeline. COSc gives a 10 m raster with 4 fuel-cover classes. COS gives a vector layer with ~80 species-level codes. The 9-class internal taxonomy needs both: COSc resolves shrub-low vs shrub-tall and grass vs non-fuel; COS resolves broadleaf-open vs broadleaf-closed and conifer-open vs conifer-closed.

The join is **not trivial** because:

- COS is vector (1:25 000 polygons), COSc is raster (10 m); rasterising COS onto the COSc grid loses sub-polygon detail.
- COS species codes don't map 1:1 onto our 9 internal classes — some COS codes are pure (e.g. `5.1.2.1` Pinus pinaster → `conifer-*`), others are mixed (e.g. `5.1.4` mixed forest → `mixed-forest`).
- COSc + COS may disagree at boundaries — COS 2023 v1 says "eucalyptus" but COSc 2024 says "Shrubland" because the stand burned in 2023. We trust COSc for state, COS for species.

**Remediation.** A documented decision table in `src/wildfire_exposure_eo/labels.py`:

```python
def fuse_labels(cosc: xarray.DataArray, cos: xarray.DataArray) -> xarray.DataArray:
    """
    Priority rules:
      1. If COSc == non-fuel → internal class 0 (non-fuel), regardless of COS.
      2. If COSc == Spontaneous herbaceous → internal class 1 (grass).
      3. If COSc == Shrubland AND COS implies tall shrub → 3 (shrub-tall),
         else 2 (shrub-low).
      4. If COSc == Open Forest AND COS species is conifer → 6 (conifer-open).
      5. If COSc == Open Forest AND COS species is broadleaf → 4 (broadleaf-open).
      6. If COSc == Dense Forest AND COS species is conifer → 7 (conifer-closed).
      7. If COSc == Dense Forest AND COS species is broadleaf → 5 (broadleaf-closed).
      8. If COSc disagrees with COS by >2 years (burn between vintages),
         trust COSc; flag in `label_confidence` metadata band.
      9. If COS is mixed-forest code, internal class 8 regardless.
    """
```

Output as a 2-band COG: band 1 the fused label (uint8 0–8), band 2 a label-confidence score (uint8 0–100). Confidence < 50 pixels are excluded from training but kept for validation viz.

### §7 — SegFormer-B0 baseline training

**Stuck-point.** SegFormer-B0 from `transformers` expects 3-channel input; Sentinel-2 has 13 bands (or 10 useful at 10 m). Naive solutions either drop information (RGB-only) or break ImageNet pretraining (random init of conv1 for 12 bands).

**Remediation.** Two options, documented in `src/wildfire_exposure_eo/fuel/baseline_segformer.py`:

1. **3-channel via informative composites.** Train on 3 channels: `(NIR, SWIR1, Red)` — false-colour for vegetation work; preserves ImageNet pretraining. Recommended for the *baseline*.
2. **12-channel via custom conv1.** Re-initialise conv1 to accept 12 bands, copy the original RGB weights into the corresponding channels, He-init the rest. Documented as a *Stage 1 advanced* path.

The baseline ships with option 1. Option 2 is a stretch goal documented in the validation report if pursued.

### §8 — Foundation-model variant (Prithvi / Clay)

**Stuck-point.** TerraTorch's API surface has churned across 0.x → 1.x; published tutorials drift. Hugging Face model IDs for Prithvi-EO 2.0 may differ from what blog posts cite.

**Remediation.** `prompts/03_train_fuel_foundation.md` (not yet drafted) opens with a "verify-then-load" block: print `terratorch.__version__`, query the HF API for `ibm-nasa-geospatial/Prithvi-EO-2.0-*` to find the actual current model IDs, then load. Per [`CLAUDE.md`](../CLAUDE.md) non-negotiable #1, model IDs land in `config/foundation_model.yaml` and the run's provenance, never in code.

### §10 — Per-asset feature extraction

**Stuck-point.** Computing 11 features per asset across an AOI of ~10–100 k assets is the cross-product step. Naive Python loops over assets × features × source rasters are minutes-per-asset. Operationally unacceptable.

**Remediation.** DuckDB-Spatial + `exactextract` for raster-vector aggregation. Pseudocode:

```python
con = duckdb.connect()
con.execute("INSTALL spatial; LOAD spatial")
con.execute("CREATE TABLE assets AS SELECT * FROM 'osm_assets.parquet'")
con.execute("CREATE TABLE assets_buffered AS SELECT *, ST_Buffer(geometry, buffer_radius_m) FROM assets")
# rasterio per-asset zonal-stats via exactextract, parallelised over assets via joblib
```

Performance target: ≤ 10 ms per asset for the full feature row on a 16-core CPU. If the smoke run busts that, switch to GPU rasterisation via `cupy-xarray`.

### §11 — Exposure-score composition

**Stuck-point.** Normalisation method affects calibration. `percentile_rank_within_aoi` (current default) makes scores AOI-relative — directly comparable within the AOI but not across AOIs. A national rollout would need a national reference distribution.

**Remediation.** Document explicitly in `config/exposure_score.yaml` (already done) and `docs/limitations.md` (to write in §15). Treat as a known scope boundary, not a flaw.

### §12 — Validation, temporal leakage

**Stuck-point.** Easiest possible bug: train / score on data that includes the burn year being predicted. ICNF Áreas Ardidas 2017–24 spans years that S2 has imagery for; using post-2017 S2 to score "2017 risk" is leakage.

**Remediation.** Hard rule in `src/wildfire_exposure_eo/validation.py`:

```python
def assert_no_temporal_leakage(score_inputs_window: DateRange, validation_burns: gpd.GeoDataFrame):
    assert validation_burns["year"].min() > score_inputs_window.end.year, \
        f"Validation burns include years inside the score-input window {score_inputs_window}"
```

Unit test enforces this. A property test (`hypothesis`) generates random window / burn-year combinations to confirm the assertion fires correctly.

### §14 — Demo command, 30-minute CPU budget

**Stuck-point.** The 30-minute target is achievable *only* with: pretrained checkpoints distributed externally, pilot-AOI-only (not national), foundation-model variant skipped (CPU inference of Prithvi-300M over 30×30 km would alone consume the budget).

**Remediation.** `demo` command runs:

1. OSM Overpass query for the AOI (1 min).
2. STAC item resolution (instant, network only).
3. Skip imagery download — uses pre-baked composite COGs published as release attachments (next section).
4. SegFormer baseline inference only — no foundation-model variant. CPU-friendly.
5. Prithvi burn-scar inference: **skipped on CPU by default**, runs only with `--with-burn-scar` flag (assumes GPU available or accepts ~30 min extra).
6. Feature extraction + score (3–5 min).
7. STAC catalog assembly (instant).
8. Validation against shipped ICNF subset (1 min).

Total: ~10–15 min on CPU with the burn-scar feature disabled. The `recent_burn_share_12mo` column in the demo output is populated with the most recent published Prithvi-Burn-Scar inference from a release attachment.

## Checkpoint distribution decision

§I.2 of [`PRE_DEV_CHECKLIST.md`](../PRE_DEV_CHECKLIST.md) — decided: **GitHub release attachments** for the pilot. Rationale below.

### Options considered

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **GitHub release attachments** | Free, no auth, 2 GB per file (10 GB per release), versioned by tag, well-tested, no new infra | 2 GB ceiling forces multi-file releases for larger checkpoints; egress quota-free | **Selected for the pilot** |
| Hugging Face Hub | Designed for this; canonical place for EO ML weights in 2026; built-in LFS + versioning; `huggingface_hub` API is one-call | Adds an `huggingface_hub` dependency + HF account requirement (mild) | Migrate post-launch when project is public |
| Cloudflare R2 | User already has `cheias-pt` infra; zero-egress charges | Auth surface for non-author users; adds a setup step to the demo command | Not needed — public artefacts have no R2 advantage |
| Zenodo | DOI-backed, archival-quality, free | High-friction upload; not version-friendly for iterative dev | After v0.1 ships, for the published-paper artefact |

### Distribution mechanics

- One GitHub release per `pre-dev-vN` / `vN.Y` tag.
- Attachments per release (under 2 GB each):
  - `fuel_segformer_b0.safetensors` (~50–100 MB)
  - `fuel_segformer_b0_config.json`
  - `prithvi_burn_scar_cog_<run_id>.tif` (the pre-baked burn-scar inference for the pilot AOI, used by `demo` to skip CPU inference)
  - `s2_composite_pilot_spring_<vintage>.tif` + `s2_composite_pilot_summer_<vintage>.tif` (pre-baked AOI composites, ~100–200 MB each)
- `wildfire-exposure-eo demo` reads `tools/release_index.json` to know which release tag → which attachment URLs.
- Demo command's first step is `download_checkpoints` with checksum verification against a committed manifest.

### License posture

All released artefacts under MIT (same as the repo). The published checkpoints are derived from public data + MIT-licensed code; no upstream license conflict.

## Final pre-flight notes

### Soften pass — verified

The README has been audited for over-promise language. Two absolute claims about the 30-minute demo were softened to *target wall-clock* framing — *target*, not *guaranteed*. The "Definition of done" section retains a strict 30-minute target because that's the gate we ship against.

### Stub files referenced but not yet written

These appear in the README and are *deliverables of the dev phase*, not pre-dev requirements. Listed here so a future CC session knows they exist as known TODOs:

- `docs/limitations.md` — honest scope boundaries (per Definition of done). Drafted at first dev session.
- `docs/scaling.md` — PostGIS / production path notes. Drafted when the pilot is scoped beyond a single AOI.
- `docs/validation_report.md` — generated by `prompts/05_validate.md` once that ships.
- `docs/training_runs/` — directory created during Phase 7 training, populated with run logs + metrics.

### Tag

When all `[ ]` items in [`PRE_DEV_CHECKLIST.md`](../PRE_DEV_CHECKLIST.md) are checked except possibly `Cloudflare R2 (optional)`, the user commits and tags `pre-dev-v0`. That tag is the milestone where this repo transitions from "pre-development" to "in-development" — the first CC session opens `prompts/01_data_audit.md` and the dev phase begins.
