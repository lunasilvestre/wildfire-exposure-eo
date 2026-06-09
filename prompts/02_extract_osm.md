# Prompt 02 — OSM extraction (Phase 2)

> **Close-out amendment (2026-06-09).** Executed as **WU-2** of
> [`00_CLOSEOUT_PLAN.md`](00_CLOSEOUT_PLAN.md). Scope unchanged. The May 14
> session on this prompt died without deliverables — start fresh; the tree
> contains nothing from it. Downstream consumers are now WU-6 (features +
> score) and WU-7 (validation); Phase 6/7 label-fusion/training references
> below are obsolete (training was cut).

## Purpose

Implement `wildfire-exposure-eo fetch-osm`: a CLI command that queries the Overpass API for every infrastructure class defined in `data/taxonomy/critical_infrastructure.yaml`, clips results to the pilot AOI, and writes a single GeoParquet of all assets with full provenance. This is the **universe-of-assets** artefact every downstream phase consumes — Phase 6 weak-label fusion, Phase 10 per-asset feature extraction, Phase 11 score composition, Phase 12 validation all walk these rows.

This phase has no ML, no foundation models, no raster work. It's a *deterministic, reproducible, provenance-rich vector extract*. If it lands clean, the four phases that follow have a stable input contract; if it skips a provenance field or fudges the snapshot date, every downstream rerun becomes ambiguous.

## Prerequisites (do not start without these)

- [ ] `pre-dev-v0` shipped; `prompts/01_data_audit.md` closed (audit GREEN includes OSM Overpass row).
- [ ] `data/aoi/pilot.geojson` and `data/aoi/smoke.geojson` committed.
- [ ] `data/taxonomy/critical_infrastructure.yaml` v0.1.0 committed and parsable (13 classes — `power.*`, `emergency.*`, `education.*`, `telecom.*`, `water.*`, `transport.*`).
- [ ] `uv sync --locked` succeeds; `pytest -q` green.
- [ ] Read `CLAUDE.md` end-to-end. The non-negotiables you'll hit hardest here are #1 (no invented IDs — every OSM ID is real and verified), #2 (CRS explicit — outputs in EPSG:4326), #3 (provenance is mandatory), #5 (GeoParquet only — no Shapefile, no GPKG).

## Deliverables

1. **`src/wildfire_exposure_eo/osm.py`** — pure functions:
   - `load_taxonomy(path: Path) -> Taxonomy` — Pydantic-validated load of `critical_infrastructure.yaml`. Returns a frozen `Taxonomy` with a `version`, a `taxonomy_sha` (SHA-256 of the file bytes), and a list of `InfrastructureClass` records.
   - `build_overpass_query(klass: InfrastructureClass, bbox: tuple[float, float, float, float]) -> str` — composes the Overpass QL block for one class. The bbox is a clean numeric tuple; tag filters come verbatim from the YAML and must not be string-interpolated with untrusted input (the YAML *is* trusted; the bbox is the only numeric injection point).
   - `query_overpass(query: str, *, endpoint: str, timeout_s: int = 60, retries: int = 2, fallback_endpoint: str | None = None) -> OverpassResult` — issues the POST, parses JSON, captures the `osm3s.timestamp_osm_base` field as the snapshot timestamp. Retries primary endpoint with exponential backoff on 5xx; falls back to `https://overpass.kumi.systems/api/interpreter` after primary exhaustion.
   - `geometrise(elements: list[dict], *, klass: InfrastructureClass) -> gpd.GeoDataFrame` — converts Overpass JSON elements into a `GeoDataFrame` in EPSG:4326. Nodes → `Point`; closed ways → `Polygon` (if class allows; otherwise `LineString`); open ways → `LineString`; relations → `MultiPolygon` for `power=substation`-type relations, log+skip otherwise (relations are sparse and class-specific).
   - `write_geoparquet(gdf: gpd.GeoDataFrame, path: Path, *, run_provenance: dict) -> Path` — writes a single GeoParquet with the documented schema (see Pydantic model below). CRS metadata pinned to `EPSG:4326`. Compression `snappy`. Per-row `provenance` is a nested struct.

2. **`src/wildfire_exposure_eo/schemas/osm_asset.py`** — Pydantic v2 models:

   ```python
   class OsmAssetProvenance(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")

       osm_snapshot_iso: datetime           # from Overpass osm3s.timestamp_osm_base
       overpass_endpoint: str               # URL actually used (primary or fallback)
       overpass_query_sha: str              # SHA-256 of the QL query string
       taxonomy_sha: str                    # SHA-256 of critical_infrastructure.yaml at run time
       taxonomy_version: str                # "0.1.0" per the YAML's version key
       run_id: str                          # e.g. "2026-05-15T08-12-44Z-abc1"
       code_commit_sha: str                 # current git HEAD
       aoi_path: str                        # "data/aoi/pilot.geojson"
       aoi_geometry_sha: str                # SHA-256 of the AOI feature's WKB

   class OsmAsset(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

       asset_id: str                        # canonical "osm:<type>/<id>", e.g. "osm:way/12345678"
       osm_type: Literal["node", "way", "relation"]
       osm_id: int                          # raw OSM integer ID (positive)
       asset_class: str                     # e.g. "power.transmission_line"
       geometry_wkb: bytes                  # EPSG:4326 WKB
       centroid_lon: float                  # in [-180, 180]
       centroid_lat: float                  # in [-90, 90]
       tags: dict[str, str]                 # raw OSM tags returned for this element
       provenance: OsmAssetProvenance
   ```

   The `arbitrary_types_allowed=True` lets us round-trip WKB cleanly. CI runs `OsmAsset.model_validate(...)` on a sample row from every GeoParquet output (already supported by `validate-schemas` workflow — extend that job to cover OSM).

3. **CLI subcommand in `src/wildfire_exposure_eo/cli.py`**:

   ```
   uv run wildfire-exposure-eo fetch-osm \
     --aoi data/aoi/pilot.geojson \
     --taxonomy data/taxonomy/critical_infrastructure.yaml \
     --out outputs/parquet/osm_assets_{run_id}.parquet \
     [--endpoint https://overpass-api.de/api/interpreter] \
     [--fallback-endpoint https://overpass.kumi.systems/api/interpreter] \
     [--smoke]
   ```

   `--smoke` reads from `data/aoi/smoke.geojson` regardless of `--aoi` and writes to `outputs/parquet/osm_assets_smoke_{run_id}.parquet`.

4. **`tests/unit/test_osm.py`** — at minimum:
   - Taxonomy round-trip: load → Pydantic-validate → SHA matches a known fixture.
   - Query construction: one test per `osm_filters` shape (node-only, way-only, multi-way, relation-bearing) asserts the produced QL string is structurally correct (parseable Overpass QL — use a small substring assertion if a full parser isn't available).
   - Geometry construction: test fixtures of synthetic Overpass JSON exercise node→Point, open-way→LineString, closed-way→Polygon, relation-with-outer→MultiPolygon paths. Assert CRS is EPSG:4326 on the output `GeoDataFrame`.
   - Provenance population: a stubbed-network end-to-end test confirms every `OsmAssetProvenance` field is populated and Pydantic-validates.
   - Empty-result handling: a class that returns zero elements does not raise; produces zero rows for that class but the run still succeeds (only a hard 5xx-after-all-retries fails the run).

5. **`tests/integration/test_osm_smoke.py`** — runs `fetch-osm --smoke` against the 1 × 1 km smoke AOI with `query_overpass` monkeypatched to return fixture JSON (no network). Asserts:
   - Output file exists at the expected path.
   - File round-trips through `gpd.read_parquet`.
   - Every row Pydantic-validates as `OsmAsset`.
   - At least 3 distinct `asset_class` values present (matches `audit`'s GREEN-threshold guarantee on the pilot AOI).
   - `OsmAssetProvenance.aoi_geometry_sha` matches the smoke AOI's actual SHA.

6. **`prompts/_session_log.md`** — append a session entry per the template.

## Constraints

- **No invented OSM IDs.** Every row's `osm_type` + `osm_id` is straight from Overpass. If a relation is malformed and geometrisation fails, log+skip; do not synthesise an ID.
- **No hardcoded coordinates.** AOI bbox is derived from `data/aoi/pilot.geojson` only.
- **CRS is EPSG:4326 always.** No silent reprojection. If a future caller wants UTM, that's a Phase-10 concern, not this one.
- **GeoParquet only.** No Shapefile, no GeoPackage, no pickled DataFrames, no CSV.
- **Snapshot timestamp comes from Overpass, not local clock.** The `osm3s.timestamp_osm_base` field is the ground truth for "what state of OSM did I just read?" If a future visitor wants to re-run this against the same OSM state, that timestamp is the handle.
- **Deterministic ordering.** Output GeoParquet rows are sorted by `(asset_class, osm_type, osm_id)`. Diffing two runs from the same snapshot should produce byte-identical output (modulo `run_id` and `code_commit_sha`).
- **Provenance per row, not per file.** Yes, this duplicates fields. The duplication is the point — downstream joins (Phase 6, 10) carry provenance with the row, not with a sidecar that can drift.
- **Empty per-class results are not failures.** A class that returns zero elements on the AOI (e.g., `power.transmission_line` on a rural smoke tile) produces zero rows but does not raise. The CLI prints a YELLOW row for that class. Only a hard "Overpass returned 5xx after primary + fallback + retries" fails the whole run.
- **Tag dict is verbatim.** Don't filter or rename tags — store the full element-level tag map. Future analysis may want `name`, `operator`, `voltage`, `start_date`, etc.; we're not deciding now which matter.
- **No PII.** OSM has `addr:*` tags on some assets (especially `amenity=hospital`). These are public-by-construction (OSM is public) but feel free to surface in the README's *Limitations* doc that we surface them.

## Test gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright src tests
uv run pytest -q
uv run pytest tests/integration/test_osm_smoke.py -v
uv run wildfire-exposure-eo fetch-osm --smoke    # network-free if smoke fixture stubs are in place; live otherwise
```

All must pass. The smoke command run *with* live Overpass is encouraged as a verification step (see below) but not required for CI green.

## Verification (do this before declaring done)

1. **Run live against the pilot AOI** — `uv run wildfire-exposure-eo fetch-osm --aoi data/aoi/pilot.geojson`. Captures real Overpass data. Document the snapshot timestamp in the run log.
2. **Inspect the GeoParquet manually** — `duckdb -c "INSTALL spatial; LOAD spatial; SELECT asset_class, COUNT(*) FROM 'outputs/parquet/osm_assets_*.parquet' GROUP BY asset_class ORDER BY 1;"`. Sanity-check counts against what `audit` reported — the same Overpass should return matching numbers (within a few units if Overpass tags changed mid-run).
3. **Spot-check 2–3 known OSM IDs** in the iD editor at `https://www.openstreetmap.org/edit?way=<osm_id>` — confirm the tag set in your row matches what OSM has live.
4. **Round-trip provenance** — open the GeoParquet in Python, take row 0's `provenance` dict, validate it through `OsmAssetProvenance.model_validate(...)`; pretty-print and confirm every field is non-empty and parseable.
5. **Two-run determinism** — re-run with the *same* `--aoi` and the *same* OSM snapshot timestamp (Overpass returns the same snapshot if you query again immediately). Confirm row counts match exactly. The output files won't be byte-identical because `run_id` and `code_commit_sha` differ between runs; assert the *non-provenance columns* match instead.
6. **Diff against `audit`** — the `audit` command's `osm-overpass` probe reports per-class counts. Reconciling those counts with the GeoParquet row counts is the cleanest sanity check that the two code paths see the same OSM.

## Out of scope for this prompt

- **No buffering of assets.** That's Phase 10 — buffer radius reads come from `criticality_weight` and `buffer_radius_m` in the taxonomy, but applying them is downstream work.
- **No spatial index construction.** GeoParquet's native column-stats are enough for the row-counts and bbox-prefilters Phase 10 needs.
- **No OSM history queries** (`[adiff:...]`, `[diff:...]`). Pilot is current-snapshot only.
- **No address / contact info enrichment.** We store the OSM tag dict; we don't fetch addresses from external services.
- **No species / fuel classification at this stage.** Pure asset extraction.
- **No STAC catalog assembly for the OSM output.** The GeoParquet is a vector deliverable; STAC integration is Phase 13.

Surface anything from this list as a question before doing it.

## Done when

- All test gates pass on the smoke AOI.
- A live run against the pilot AOI produces an `outputs/parquet/osm_assets_<run_id>.parquet` with rows for ≥ 3 of the 13 infrastructure classes.
- Every row Pydantic-validates as `OsmAsset` against the committed schema.
- CI `validate-schemas` job extended to spot-check OSM GeoParquet schema on every push.
- `prompts/_session_log.md` has a new entry.
- A PR exists on `main` with green CI, a one-paragraph description, and at minimum the smoke-test fixture committed under `tests/fixtures/overpass/` so subsequent CC sessions can rerun unit tests offline.
