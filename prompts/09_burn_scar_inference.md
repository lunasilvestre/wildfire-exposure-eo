# Prompt 09 — Burn-scar inference (Stage 1b)

> **Close-out amendment (2026-06-09, rev. GPU-first).** Executed as
> **WU-1** of [`00_CLOSEOUT_PLAN.md`](00_CLOSEOUT_PLAN.md) — **GPU-first on
> atlas (RTX 3090), ON the critical path**, run early right after WU-0
> since it depends only on the committed STAC resolver + AOI. Pretrained
> `Prithvi-EO-2.0-300M-BurnScars` inference only, exactly as written below;
> no fine-tuning exists anywhere in the close-out scope. Smoke-AOI inference
> before pilot; the pilot COG becomes the pre-baked artifact the CPU demo
> ships with. If atlas/CUDA is unavailable, halt and surface — shipping
> without `recent_burn_share_12mo` requires explicit human approval.
> (Header renumbered 03 → 09 to match the filename rename already in the
> tree.)

## Purpose

Run [Prithvi-EO 2.0](https://github.com/NASA-IMPACT/Prithvi-EO-2.0) with the burn-scar downstream task over the pilot AOI to produce a per-pixel burn-probability raster covering the trailing 12 months of Sentinel-2 L2A imagery. The output threshold-binarised feeds the per-asset `recent_burn_share_12mo` feature in the Stage-2 exposure score.

This is a Stage 1b inference-only work-unit. No fine-tuning. No new labels. The point is to leverage Prithvi's published burn-scar fine-tune to fill the gap between the latest ICNF Áreas Ardidas vintage (~1-year publication lag) and the current date.

## Prerequisites (do not start without these)

- [ ] `PRE_DEV_CHECKLIST.md` complete through section H.
- [ ] `prompts/01_data_audit.md` shipped — Sentinel-2 L2A reachability and AOI coverage GREEN.
- [ ] `terratorch>=1.2` installed; `uv run python -c "import terratorch; print(terratorch.__version__)"` succeeds.
- [ ] Pilot AOI `data/aoi/pilot.geojson` frozen.
- [ ] Read `CLAUDE.md` end-to-end; specifically the anti-pattern *"Prithvi burn-scar inference is essentially ignition prediction"* — no, it isn't.

## Deliverables

1. **`src/wildfire_exposure_eo/burn_scar.py`** — pure functions:
   - `resolve_prithvi_burn_scar_model() -> ModelHandle` — verifies the Hugging Face model ID at runtime, downloads/loads checkpoint, returns a TerraTorch-friendly handle. The model ID is **not hardcoded in code** — it comes from `config/burn_scar.yaml` and is captured in the run's provenance dict (CLAUDE.md non-negotiable #1).
   - `query_recent_s2(aoi, window_months=12) -> list[StacItem]` — pystac-client search against MS PC for cloud-filtered S2 L2A items intersecting the AOI in the trailing window. Returns a deterministically-ordered list with every ID logged (CLAUDE.md verify-then-act).
   - `infer_burn_probability(items, model_handle, aoi) -> xarray.DataArray` — runs the model over the items, returns a single per-pixel max-probability raster clipped to the AOI.
   - `write_burn_scar_cog(da, path, provenance) -> Path` — COG writer with full provenance metadata embedded.

2. **`config/burn_scar.yaml`** — operational config:
   ```yaml
   model:
     family: "prithvi-eo-2.0"
     downstream_task: "burn-scar"
     hf_model_id: "TBD-verified-at-audit"   # captured in provenance
     backbone_param_count: 300_000_000
   inference:
     window_months: 12
     s2_max_cloud_cover: 30
     binarisation_threshold: 0.5
     output_format: "cog"
   ```

3. **`src/wildfire_exposure_eo/schemas/burn_scar.py`** — Pydantic v2 model `BurnScarRun` capturing every provenance field (model_id, model_version, hf_revision_sha, s2_item_ids, aoi_geometry_sha, window_start, window_end, run_id, code_commit_sha, terratorch_version, torch_version).

4. **`stac/burn-scar-recent/`** — STAC items for the produced COGs, following the collection definition in `inventory.yaml`.

5. **`tests/unit/test_burn_scar.py`** — at least:
   - Test that `resolve_prithvi_burn_scar_model` raises if `hf_model_id` is the placeholder `"TBD-verified-at-audit"`.
   - Test that `query_recent_s2` returns deterministically-ordered items.
   - Test that the COG writer embeds the provenance dict in the GeoTIFF tags.

6. **`tests/integration/test_burn_scar_smoke.py`** — runs end-to-end on the 1 km × 1 km smoke AOI with a tiny window (1 month). Exits 0 in under 5 minutes on CPU.

7. **`prompts/_session_log.md`** — append session entry.

## Constraints

- **Inference only.** No fine-tuning. If Prithvi's published burn-scar weights are insufficient for Portuguese Atlantic landscapes, document the gap in `docs/limitations.md` and exit — fine-tuning is a separate work-unit.
- **No invented model IDs.** Verify the HF model ID before committing. Place a TODO marker if unverified.
- **CRS explicit.** Output raster CRS is `EPSG:4326` for STAC compatibility; reprojection from the native S2 UTM zone is done with documented resampling (`nearest` for probability — *not* bilinear; revisit if calibration shows artefacts).
- **Frozen backbone.** No accidental training mode. Use `model.eval()` and `torch.no_grad()` explicitly; assert in a unit test.
- **Deterministic ordering.** S2 items sorted by datetime ascending, tie-break by item ID. Logged in the run's provenance.
- **Output COG only.** No intermediate GeoTIFFs, no Shapefiles, no pickled tensors.
- **No probability claim in any user-facing text.** The output is a per-pixel *probability of burn-scar presence as inferred by Prithvi-Burn-Scar*, not a probability that the pixel is burned. The README, comments, and STAC asset titles use *burn-scar inference probability* throughout.
- **No "ignition" or "fire prediction" language anywhere.** CLAUDE.md anti-pattern.

## Test gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright src tests
uv run pytest -q
uv run pytest tests/integration/test_burn_scar_smoke.py -v --runslow
uv run stac-validator stac/catalog.json --recursive
```

All must pass before the task is complete.

## Verification (do this before declaring done)

1. Run the smoke test end-to-end on the 1 km × 1 km AOI. Visually inspect the output COG in QGIS — burn-probability values should align with the most-recent ICNF burn polygon in the smoke AOI (if any).
2. Run on the full pilot AOI. Confirm the produced COG covers the bbox and the byte count is reasonable (<200 MB for 30×30 km at 10 m).
3. Inspect the provenance dict in the run's JSON sidecar. Every Pydantic field populated, no nulls except where documented.
4. Compare your top-decile burn-probability pixels against the latest ICNF Áreas Ardidas vintage where the windows overlap. Spearman correlation reported in `docs/burn_scar_audit.md`.
5. Run `uv run wildfire-exposure-eo audit --aoi data/aoi/pilot.geojson` and confirm the audit table now includes a `prithvi-burn-scar` GREEN row.

## Out of scope for this prompt

- Fine-tuning Prithvi on Portuguese labels.
- Comparing Prithvi-Burn-Scar against EFFIS active-fire products (separate validation work-unit).
- Updating the exposure score weights (already set in `config/exposure_score.yaml`).
- The per-asset feature aggregation (Stage 2, separate prompt).
- Any "ignition prediction" framing.

Surface anything in this list as a question before doing it.

## Done when

- All test gates pass.
- A burn-probability COG for the full pilot AOI is written under `outputs/cogs/burn_scar_<run_id>.tif`.
- The matching STAC item validates under `stac/burn-scar-recent/`.
- The `audit` command reports `prithvi-burn-scar` GREEN.
- The Spearman crosscheck against ICNF Áreas Ardidas (overlap window) is documented in `docs/burn_scar_audit.md`.
- `prompts/_session_log.md` is updated.
- A PR exists on `main` with the change, green CI, and a one-paragraph description.
