# Prompt 03 — STAC item resolution (Phase 3)

## Purpose

Implement `wildfire-exposure-eo resolve-stac`: a CLI command that resolves a deterministic manifest of Microsoft Planetary Computer STAC items for the pilot AOI, covering Sentinel-2 L2A, Sentinel-1 GRD, Cop-DEM GLO-30, and ESA WorldCover. The output is a single JSON manifest — not imagery — that downstream phases (6 label fusion, 7 training, 10 feature extraction) consume to know *which* items to read with `stackstac` / `odc-stac`.

The manifest is the project's **single source of truth for which scenes the pipeline saw**. Re-running the same `--aoi` and `--date-windows` on the same calendar day should return the same manifest. The point of this phase is reproducibility, not data movement — nothing is downloaded here.

## Prerequisites (do not start without these)

- [ ] `prompts/01_data_audit.md` closed; `audit` reports GREEN for Sentinel-2 L2A, Sentinel-1 GRD, Cop-DEM GLO-30, ESA WorldCover.
- [ ] `prompts/02_extract_osm.md` shipped *or* in flight on a parallel branch — the OSM GeoParquet is **not** an input to this phase; the two can run in any order.
- [ ] `data/aoi/pilot.geojson` and `data/aoi/smoke.geojson` committed.
- [ ] `pystac-client >= 0.8` resolved by `uv sync --locked`.
- [ ] Read `docs/methodology.md` → "§3 — STAC item resolution". The two-pass cloud-cover asymmetry (spring strict, summer relaxed) is the load-bearing decision here.
- [ ] Read `CLAUDE.md` end-to-end. The non-negotiables you'll hit hardest are #1 (no invented STAC IDs — every item is real and verified), #3 (provenance is mandatory), and the verify-then-act protocol (list, log every ID, then resolve).

## Deliverables

1. **`src/wildfire_exposure_eo/stac.py`** — pure functions:
   - `resolve_sentinel_2(aoi: ShapelyGeom, window_start: date, window_end: date, *, max_cloud_cover: int, client: Client) -> list[StacItemRef]` — calls `pystac-client.search` against `sentinel-2-l2a` on Microsoft Planetary Computer with the given AOI + datetime window + `eo:cloud_cover` filter. Returns a list of typed `StacItemRef` records sorted by `(datetime ascending, id ascending)`.
   - `resolve_sentinel_1(aoi, window_start, window_end, *, mode="IW", polarizations=("VV", "VH"), client) -> list[StacItemRef]` — same shape, `sentinel-1-grd` collection.
   - `resolve_cop_dem(aoi, client) -> list[StacItemRef]` — single static layer; returns whatever items intersect the AOI (usually 1).
   - `resolve_worldcover(aoi, *, vintage: int = 2021, client) -> list[StacItemRef]` — single vintage; returns AOI-intersecting items.
   - `build_manifest(...) -> StacManifest` — top-level orchestrator. Composes the four resolution passes into one `StacManifest` Pydantic record with full provenance. Implements the two-pass S2 asymmetry:
     - **spring composite**: `2025-03-01 → 2025-06-15`, `eo:cloud_cover <= 30`
     - **summer composite**: `2025-07-01 → 2025-10-31`, `eo:cloud_cover <= 60`, plus a note in provenance that summer relaxes the threshold
     - Both windows are configurable via CLI but ship with these defaults documented in methodology.md.
   - `write_manifest(manifest: StacManifest, path: Path) -> Path` — writes manifest JSON with `indent=2` and stable key ordering for diff-friendliness.

2. **`src/wildfire_exposure_eo/schemas/stac_manifest.py`** — Pydantic v2 models:

   ```python
   class StacItemRef(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")

       collection: str                      # "sentinel-2-l2a", "sentinel-1-grd", etc.
       item_id: str                         # exact STAC item ID from MS PC
       datetime_iso: datetime               # item's nominal datetime
       bbox: tuple[float, float, float, float]   # [W, S, E, N], EPSG:4326
       cloud_cover: float | None            # only populated for S2 and similar
       assets_referenced: list[str]         # asset keys actually needed downstream (B02, B03, ..., vh, vv, ...)
       href_root: str                       # the MS PC blob root (no SAS token; tokens are runtime-only)
       extra: dict[str, str | int | float] = Field(default_factory=dict)  # mode, polarizations, etc.

   class StacWindow(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")

       label: str                           # "spring", "summer", "static"
       start: date
       end: date
       max_cloud_cover: int | None          # None for SAR / DEM / WorldCover
       items: list[StacItemRef]
       items_returned: int                  # len(items); duplicated for diff readability
       relaxed_threshold_reason: str | None # for summer S2 — documented asymmetry

   class StacManifest(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")

       run_id: str
       code_commit_sha: str
       aoi_path: str
       aoi_geometry_sha: str
       resolved_at_utc: datetime
       stac_catalog_url: str                # MS PC root, "https://planetarycomputer.microsoft.com/api/stac/v1"
       collections: dict[str, list[StacWindow]]   # e.g. {"sentinel-2-l2a": [spring, summer], "sentinel-1-grd": [...], ...}
       totals: dict[str, int]               # quick-look counts per collection
   ```

   The schema is frozen + `extra="forbid"` so an accidentally-added field surfaces at validation time, not silently. CI's `validate-schemas` job extends to cover this manifest.

3. **CLI subcommand in `src/wildfire_exposure_eo/cli.py`**:

   ```
   uv run wildfire-exposure-eo resolve-stac \
     --aoi data/aoi/pilot.geojson \
     --out outputs/manifests/stac_{run_id}.json \
     [--spring-start 2025-03-01] [--spring-end 2025-06-15] [--spring-cloud 30] \
     [--summer-start 2025-07-01] [--summer-end 2025-10-31] [--summer-cloud 60] \
     [--worldcover-vintage 2021] \
     [--catalog https://planetarycomputer.microsoft.com/api/stac/v1] \
     [--smoke]
   ```

   `--smoke` uses `data/aoi/smoke.geojson` and writes to `outputs/manifests/stac_smoke_{run_id}.json`.

4. **`tests/unit/test_stac.py`** — at minimum:
   - Resolution determinism: stub `pystac_client.Client` to return a fixed list; assert output ordering is `(datetime, id)`.
   - Two-pass S2 cloud-cover: stub the client; verify the spring call uses `eo:cloud_cover<=30` and the summer call uses `eo:cloud_cover<=60`; confirm the `relaxed_threshold_reason` on the summer window is non-empty.
   - Empty result handling: a collection returning zero items produces a `StacWindow` with `items=[]` and `items_returned=0`; the run does not raise.
   - Provenance: stubbed end-to-end; every field on `StacManifest` populated and Pydantic-validates.
   - SAS-token stripping: `href_root` never contains a `?se=...&sig=...` query string; if MS PC returns signed URLs, we strip them.

5. **`tests/integration/test_stac_smoke.py`** — runs `resolve-stac --smoke` with the `pystac_client.Client` constructor monkeypatched to return a fixture client backed by `tests/fixtures/stac/`. Asserts:
   - Manifest file exists at expected path.
   - Manifest round-trips through `StacManifest.model_validate_json`.
   - `totals` matches per-window `items_returned` summed across windows.
   - `aoi_geometry_sha` matches the smoke AOI's actual SHA.

6. **`prompts/_session_log.md`** — append a session entry per the template.

## Constraints

- **No invented STAC IDs.** Every item ID in the manifest is straight from the catalog. If a `pystac_client.search()` returns zero items for a window, the manifest lists zero items — never synthesised.
- **Verify-then-act.** Per CLAUDE.md: list candidates with `search().items()`, log every returned ID at INFO level, *then* persist into the manifest. Skipping the log step is a bug.
- **Deterministic ordering.** Items within each `StacWindow` are sorted by `(datetime ascending, item_id ascending)`. Two runs of the same query against an unchanged MS PC catalog must produce byte-identical `items` arrays.
- **No SAS tokens persisted.** MS PC asset HREFs include time-limited SAS tokens. Strip them before persisting. Downstream raster code re-signs at read time via `planetary-computer.sign()`.
- **No downloads.** This phase only resolves IDs and metadata. Any actual COG read is Phase 4/6/7/10 work.
- **No hardcoded coordinates.** AOI comes from the file.
- **No hardcoded date windows in code** — defaults live in the CLI; the *contract* (spring strict, summer relaxed) lives in `methodology.md` and is repeated in the manifest's `relaxed_threshold_reason`.
- **Provenance per manifest, not per item.** Items are leaves; the manifest is the unit that earns a `run_id`. Downstream consumers carry `manifest_path` or `manifest_sha` as the join key.
- **Assets-referenced is explicit, not all-assets.** For S2 we name the bands we actually use (`B02`, `B03`, `B04`, `B08`, `B11`, `B12`, `SCL` for masking — others are skipped). For S1: `vh`, `vv`. For Cop-DEM: `data`. For WorldCover: `map`. This keeps `stackstac` reads narrow and reproducible.

## Test gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright src tests
uv run pytest -q
uv run pytest tests/integration/test_stac_smoke.py -v
uv run wildfire-exposure-eo resolve-stac --smoke    # live; document the manifest at the run-log entry
```

All must pass. Live smoke run against MS PC is encouraged as a verification step (see below).

## Verification (do this before declaring done)

1. **Run live against the pilot AOI** — `uv run wildfire-exposure-eo resolve-stac --aoi data/aoi/pilot.geojson`. Captures real MS PC data. Document totals + manifest path in the session log.
2. **Inspect totals** — open the JSON; per-collection counts should look like:
   - `sentinel-2-l2a`: ≥ 5 items in spring window, ≥ 5 in summer window (typical PT-coast cadence).
   - `sentinel-1-grd`: ≥ 10 items combined (S1A only, IW mode, both polarizations).
   - `cop-dem-glo-30`: 1–2 items (it's a single static tile-grid).
   - `esa-worldcover`: 1–2 items.
   If totals are wildly off, suspect the bbox or the cloud-cover thresholds.
3. **Spot-check 2–3 item IDs** — paste an S2 item ID into MS PC's catalog browser at `https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a` and confirm the item exists, intersects the AOI, and has the cloud-cover stamped in your manifest.
4. **Two-run determinism** — re-run within minutes; assert the `collections` dict produces identical item ID arrays (provenance fields like `run_id` will differ; data fields must match).
5. **Diff against `audit`** — the audit's per-collection counts should be in the same ballpark as the manifest's `totals`. Reconciling them is the cleanest cross-check.

## Out of scope for this prompt

- **No imagery download.** That's Phase 4 (static rasters) and Phase 6/7 (S2/S1 reads via stackstac).
- **No cloud-mask logic.** The summer window's relaxed cloud-cover is *resolved*, not *masked*. Masking via S2 SCL is Phase 6 work.
- **No HLS resolution.** HLS is AUXILIARY per `docs/data_sources.md`; revisit when inter-annual harmonisation matters.
- **No Dynamic World resolution.** GEE-only; flagged FUTURE.
- **No catalog merging.** Pilot uses MS PC only; Element84 / Copernicus DSE are documented alternatives, not active sources.

Surface anything from this list as a question before doing it.

## Done when

- All test gates pass.
- A live run produces `outputs/manifests/stac_<run_id>.json` with non-zero `totals` for all four collections.
- Every manifest Pydantic-validates as `StacManifest` against the committed schema.
- CI `validate-schemas` job extended to spot-check STAC manifests on every push.
- `prompts/_session_log.md` has a new entry.
- A PR exists on `main` with green CI, a one-paragraph description, and a small fixture under `tests/fixtures/stac/` enabling offline unit-test reruns.
