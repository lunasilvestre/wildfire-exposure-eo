# Prompt 06 — Fuel layer from EFFIS + COSc (WU-5)

> **Drafted 2026-06-11 at the attended close-out checkpoint** per
> [`00_CLOSEOUT_PLAN.md`](00_CLOSEOUT_PLAN.md) ("write it first"). Executed as
> **WU-5**. The old Phase-6 weak-label *training* pipeline is dead — the
> COSc/COS fusion logic survives only as an input to this derivation. No ML.

## Purpose

Implement `wildfire-exposure-eo fuel-layer`: derive a fuel-class raster on an
explicit pilot grid from the EFFIS pan-European fuel map refined by DGT COSc
land-cover, via a versioned crosswalk config. Output is a 2-band COG plus a
STAC item. Downstream, WU-6 consumes band 2 (severity) as the
`fuel_class_severity_weight` zonal input — the single heaviest weight (0.30)
in `config/exposure_score.yaml`.

Pure raster reclass + resample. Deterministic, no network beyond reading the
WU-3 cache.

## Prerequisites (do not start without these)

- [ ] WU-3 shipped: `fetch-rasters` cache + manifest contain EFFIS fuel map
      and DGT COSc entries. Read the manifest first; log the cache paths and
      checksums you will consume. If either source is missing from the cache,
      stop and write `prompts/_HIL.md` — do not re-fetch ad hoc.
- [ ] WU-1 shipped: `stac/` catalog exists (this WU appends an item).
- [ ] `data/aoi/pilot.geojson` + `data/aoi/smoke.geojson` load.
- [ ] Read `CLAUDE.md` end-to-end. Non-negotiables you will hit hardest:
      #2 (CRS explicit), #3 (provenance), #5 (COG + STAC), #10 (AOI frozen).

## Deliverables

1. **`config/fuel_crosswalk.yaml`** — versioned (`version: "0.1.0"`) mapping:
   every EFFIS fuel-map class code present on the pilot AOI →
   `{internal_class: str, severity: float}` with `severity ∈ [0,1]` and a
   one-line comment per entry citing the Scott & Burgan fuel-model rationale.
   Severities are **expert-set constants** (same epistemic status as the
   score weights) — say so in the file header. Unmapped EFFIS codes
   encountered at runtime are a hard error naming the code, never a silent
   default (#1: no invented mappings).

2. **`src/wildfire_exposure_eo/fuel.py`** — pure functions:
   - `pilot_grid(aoi_path: Path, *, resolution_m: int = 10) -> GridSpec` —
     explicit grid: EPSG:32629, bounds = AOI envelope snapped outward to the
     resolution, derived from the AOI file only (no literals). `GridSpec`
     (Pydantic, frozen) carries `crs`, `transform`, `width`, `height`,
     `resolution_m`, `aoi_geometry_sha`.
   - `load_crosswalk(path: Path) -> Crosswalk` — Pydantic-validated; carries
     `crosswalk_sha` (SHA-256 of file bytes).
   - `reclass_effis(effis: xr.DataArray, crosswalk: Crosswalk) -> tuple[xr.DataArray, xr.DataArray]`
     — (class, severity) on the EFFIS native grid.
   - `refine_with_cosc(klass, severity, cosc: xr.DataArray) -> tuple[...]` —
     decision table, documented in the docstring like `docs/methodology.md`
     §6: (1) COSc non-fuel → class 0, severity 0.0 regardless of EFFIS;
     (2) COSc herbaceous where EFFIS says forest → trust COSc state (stand
     likely burned/cleared since the EFFIS vintage), reclass to grass
     severity; (3) otherwise EFFIS class stands. Every rule maps to one
     unit test.
   - `write_fuel_cog(klass, severity, grid: GridSpec, path: Path, *, provenance: dict) -> Path`
     — 2-band COG: band 1 `fuel_class` (uint8), band 2 `severity_x100`
     (uint8). Nodata 255. Resampling: **nearest** for both sources
     (categorical). Provenance sidecar JSON next to the COG.

3. **STAC item** under `stac/fuel-layer/` appended to the existing catalog
   via the *attached* child collection (see WU-1's session-log gotcha:
   `Collection.from_file` detaches — use the parent's child reference).

4. **CLI subcommand** `fuel-layer` with `--aoi`, `--crosswalk`, `--out
   outputs/cogs/fuel_class_{run_id}.tif`, `--smoke` (smoke AOI grid,
   `fuel_class_smoke_{run_id}.tif`).

5. **Schemas** — `schemas/fuel_layer.py`: `GridSpec`, `Crosswalk`,
   `FuelLayerProvenance` (`effis_cache_path` + `effis_sha256` +
   `effis_vintage`, same trio for COSc, `crosswalk_sha`,
   `crosswalk_version`, `grid` nested, `run_id`, `code_commit_sha`,
   `aoi_path`, `aoi_geometry_sha`). Frozen, `extra="forbid"`.

6. **Tests** — unit: one per decision-table rule, crosswalk round-trip +
   unmapped-code error, grid determinism (same AOI → identical transform);
   property (`hypothesis`): grid snap covers the AOI envelope for arbitrary
   small bboxes. Integration: smoke-AOI run from cached fixtures asserts COG
   opens, CRS == EPSG:32629, both bands present, provenance sidecar
   validates.

7. **`prompts/_session_log.md`** entry.

## Constraints

- **Effective resolution honesty.** The COG grid is 10 m but the fuel signal
  is EFFIS-native (~100 m+) except where COSc refines it. Write the actual
  EFFIS native resolution (read it from the raster, don't assume) into the
  provenance sidecar and the STAC item description. WU-7 quotes it.
- **CRS explicit at every step** — `rio.set_crs`/`rio.write_crs` on load of
  each source; reprojection to the pilot grid is one explicit
  `rio.reproject` call with documented resampling. No implicit alignment.
- **No new dependencies.** rioxarray/rasterio/xarray already in the stack
  suffice. If something seems missing, stop and ask (#8).
- **Smoke before pilot.** Full decision table exercised on the smoke grid
  and gates green before the pilot-AOI run.

## Test gates

```bash
uv run ruff check . && uv run ruff format --check . \
  && uv run pyright src tests scripts && uv run pytest -q
uv run stac-validator validate stac/catalog.json --recursive   # v4 CLI spelling
uv run wildfire-exposure-eo fuel-layer --smoke
```

## Verification (before declaring done)

1. Pilot run; open the COG, assert grid == `pilot_grid(...)` exactly.
2. Class histogram vs. EFFIS source histogram — reclass must not invent
   area: per-class pixel shares within ±2 % of source shares after
   accounting for COSc overrides (log the comparison table).
3. Severity band spot-check: 3 hand-picked locations (a dense eucalyptus
   stand, an urban core, an agricultural plain — choose by inspecting COSc),
   confirm severity ordering matches intuition and the crosswalk comments.
4. STAC item validates; catalog still validates recursively.

## Out of scope

- COS (vector, species-level) refinement — COSc-only at this stage; note it
  as future work in the sidecar description.
- Any training, any ML, any probability language.
- National grids; pilot + smoke AOIs only.

## Done when

Gates green; smoke + pilot COGs exist with sidecars; STAC item committed and
valid; decision-table tests pass; session-log entry appended; pushed with CI
green.
