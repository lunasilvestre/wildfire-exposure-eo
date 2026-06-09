# Prompt 05 — ICNF burns ingestion (Phase 5)

> **Close-out amendment (2026-06-09).** Executed as **WU-4** of
> [`00_CLOSEOUT_PLAN.md`](00_CLOSEOUT_PLAN.md). Scope unchanged — this stays
> the validation ground truth (WU-7) and the historical-burn feature source
> (WU-6). The May 14 session on this prompt died without deliverables —
> start fresh.

## Purpose

Implement `wildfire-exposure-eo fetch-burns`: a CLI command that queries the ICNF *Áreas Ardidas* ArcGIS REST MapServer for every published vintage (1975 – latest, currently 2025), converts perimeter polygons to GeoParquet, and writes a single multi-year vector deliverable with full provenance. This is **the project's validation ground truth** — Phase 12 lift / Spearman / Brier all use these polygons as labels, and Phase 10 uses them to compute `historical_burn_count_25y` and `historical_burn_share` features per asset.

It's also the single most temporally rich source the project has. Treating it carefully means: per-vintage provenance (not just one timestamp for the whole multi-year merge), explicit attribution (ICNF cares about this), and a clean leakage boundary for validation.

## Prerequisites (do not start without these)

- [ ] `prompts/01_data_audit.md` closed; audit GREEN for ICNF Áreas Ardidas. `scripts/00_icnf_fetch.sh` already verifies the ArcGIS REST endpoint works.
- [ ] `data/aoi/pilot.geojson` committed.
- [ ] Read `docs/data_sources.md` → "ICNF Áreas Ardidas" entry for the endpoint pattern, attribution string, and known gaps (≥1 ha aggregation, pre-1990 coarseness).
- [ ] Read `docs/methodology.md` → "§12 — Validation, temporal leakage". The leakage rule (validation polygons must be from years *after* the score-input window) is enforced downstream; this phase just makes the vintage column unambiguous so Phase 12 can assert.
- [ ] Read `CLAUDE.md`. Non-negotiables that bite here: #1 (no invented vintages or feature IDs), #3 (provenance — per row, includes vintage), #5 (GeoParquet only).

## Deliverables

1. **`src/wildfire_exposure_eo/burns.py`** — pure functions:
   - `discover_icnf_layers(*, mapserver_url: str = ICNF_MAPSERVER_URL) -> list[IcnfLayerDescriptor]` — queries the REST MapServer's `?f=json` endpoint to enumerate available layers and their `year` (1975 → latest). Returns a list of `IcnfLayerDescriptor` with `layer_id`, `year`, `name`, `feature_count_total`.
   - `fetch_icnf_layer(layer: IcnfLayerDescriptor, aoi: ShapelyGeom, *, batch_size: int = 1000) -> gpd.GeoDataFrame` — pulls all features for one year intersecting the AOI bbox via the `query` REST endpoint (`?where=1%3D1&geometry=...&outFields=*&f=geojson`). Handles pagination (ArcGIS REST returns at most `batch_size` features per page; loop on `resultOffset` until exhausted). CRS is EPSG:3763 (the ICNF native — Portuguese national grid); reprojects to EPSG:4326 before returning.
   - `combine_burns(per_year: dict[int, gpd.GeoDataFrame]) -> gpd.GeoDataFrame` — concatenates per-year frames into one with a `year` column, applies a stable sort `(year ascending, area_ha descending, feature_id ascending)`, and assigns canonical row IDs.
   - `write_burns_geoparquet(gdf, path, *, run_provenance) -> Path` — writes a single GeoParquet with the documented schema. CRS pinned to EPSG:4326. Compression `snappy`. Per-row provenance is a nested struct.

2. **`src/wildfire_exposure_eo/schemas/burn_perimeter.py`** — Pydantic v2 models:

   ```python
   class IcnfLayerDescriptor(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")

       layer_id: int
       year: int                            # 1975..latest
       name: str                            # ICNF's layer name, verbatim
       feature_count_total: int             # what the server reports before any AOI filter

   class BurnPerimeterProvenance(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")

       icnf_layer_id: int
       icnf_layer_name: str
       vintage_year: int
       mapserver_url: str
       fetched_at_utc: datetime
       run_id: str
       code_commit_sha: str
       aoi_path: str
       aoi_geometry_sha: str
       license: str = "ICNF open data, attribution required"
       attribution: str = "ICNF – Áreas Ardidas em Portugal Continental"

   class BurnPerimeter(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

       row_id: str                          # canonical "icnf:<year>:<feature_id>"
       vintage_year: int                    # primary join key for Phase 10 / 12
       icnf_feature_id: int                 # raw ID from ArcGIS REST
       geometry_wkb: bytes                  # EPSG:4326 WKB
       area_ha: float                       # from ICNF attribute (preferred) or computed (fallback)
       provenance: BurnPerimeterProvenance
   ```

3. **CLI subcommand**:

   ```
   uv run wildfire-exposure-eo fetch-burns \
     --aoi data/aoi/pilot.geojson \
     --out outputs/parquet/icnf_burns_{run_id}.parquet \
     [--start-year 1975] [--end-year 2025] \
     [--mapserver-url ...] \
     [--smoke]
   ```

   `--smoke` switches to the smoke AOI. Fewer polygons but the same pipeline shape.

4. **`tests/unit/test_burns.py`** — at minimum:
   - Layer discovery: stub `requests.get` for the `?f=json` MapServer index; assert `discover_icnf_layers` returns a list with the expected year span (1975..2025) and that each `IcnfLayerDescriptor` Pydantic-validates.
   - Pagination handling: stub a layer that returns 2 500 features paginated in 3 calls; assert the combined frame has 2 500 rows and that `resultOffset` was incremented correctly.
   - CRS reprojection: a fixture feature in EPSG:3763 is round-tripped to EPSG:4326 with an area within 1% of expected (allowing for projection distortion).
   - Provenance population: every `BurnPerimeterProvenance` field populated; vintage_year matches the source layer.
   - Empty AOI: a layer that returns zero features for an AOI does not raise; produces zero rows for that vintage but the run still succeeds.
   - Year ordering: combined frame is sorted `(year ascending, area_ha descending, feature_id ascending)`.

5. **`tests/integration/test_burns_smoke.py`** — runs `fetch-burns --smoke` against a fixture HTTP server (or `requests.get` monkeypatch) returning canned ArcGIS REST JSON for 3 sample years (e.g., 2017, 2020, 2024). Asserts:
   - Output GeoParquet exists at expected path.
   - Frame round-trips through `gpd.read_parquet`.
   - Every row Pydantic-validates.
   - `vintage_year` values match what the fixture returned.

6. **`prompts/_session_log.md`** — append entry.

## Constraints

- **No invented vintage years.** Years come from the MapServer's layer index. If a year is missing from the server (gaps in early-1980s coverage exist), the manifest reflects that — do not synthesise.
- **No invented feature IDs.** `icnf_feature_id` is straight from the REST response.
- **CRS reprojection is explicit and documented.** ICNF publishes in EPSG:3763; this phase stores in EPSG:4326 for cross-source joins. The reprojection is logged at INFO and noted in provenance.
- **Pagination is mandatory.** ArcGIS REST caps responses at 1000–2000 features per page depending on layer; loop on `resultOffset` + `returnExceededLimitFeatures=true` semantics. Without pagination, large years (2017, 2003) silently truncate.
- **Per-row provenance carries vintage.** This is the key downstream-leakage anchor. Phase 12 asserts `vintage_year > score_inputs_window.end.year`. If a row's `vintage_year` is wrong, leakage detection fails silently.
- **Area normalisation.** ICNF features include an `area_ha` attribute; prefer it. If absent or 0, compute via `gdf.to_crs("EPSG:3763").area / 10000`. Always reproject before computing area (no degree-area computations).
- **GeoParquet only.** No Shapefile, no GPKG as output.
- **Deterministic ordering.** Two runs against the same MapServer state must produce byte-identical non-provenance columns. Sort by `(vintage_year, area_ha descending, feature_id)`.
- **No PII.** ICNF perimeters are public-by-construction. No ignition-cause attribution data (that's behind the ICNF SGIF account-gated layer; future-work alignment).

## Test gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright src tests
uv run pytest -q
uv run pytest tests/integration/test_burns_smoke.py -v
uv run wildfire-exposure-eo fetch-burns --smoke    # live; smaller than full pilot
```

All must pass. Full live run against the pilot AOI is the verification step.

## Verification (do this before declaring done)

1. **Run live against the pilot AOI** — `uv run wildfire-exposure-eo fetch-burns --aoi data/aoi/pilot.geojson`. Document row count, vintage span, and bytes in the session log.
2. **Inspect counts per year** — `duckdb -c "SELECT vintage_year, COUNT(*), SUM(area_ha) FROM 'outputs/parquet/icnf_burns_*.parquet' GROUP BY 1 ORDER BY 1;"`. Sanity-check against ICNF's public annual statistics — totals should match the ICNF figure for that year, restricted to the AOI.
3. **Spot-check the 2017 Aveiro fire** — the AOI covers part of the Sept-2024 Sever do Vouga complex; also check 2017 / 2020. Query for `vintage_year IN (2017, 2020, 2024)` and confirm row counts are non-zero.
4. **Spot-check geometry** — load the GeoParquet in QGIS or `geopandas`; overlay on the pilot AOI; perimeters should fall inside the AOI bbox or intersect it.
5. **Spot-check provenance** — pick row 0, validate through `BurnPerimeter.model_validate(...)`; pretty-print; confirm vintage and attribution are correct.
6. **Reproject sanity** — compute total area_ha via shapely-on-EPSG:4326 (incorrect; should differ from the stored area) vs. EPSG:3763 reprojection (correct; should match stored area). Confirms the stored area was computed in the right CRS.
7. **Cross-check against `audit`** — audit's ICNF probe reports total burn-row count for the AOI; this phase's output should match within a small delta (the audit uses a sampled subset of years).

## Out of scope for this prompt

- **No burn-cause attribution** (lightning, arson, accidental, etc.). That's in ICNF SGIF, which is account-gated. Documented as FUTURE under `docs/data_sources.md`.
- **No fire-spread modelling.** This phase only ingests *what burned*, not *how it spread*.
- **No EFFIS burn-area cross-validation.** EFFIS BA is AUXILIARY; reconciling EFFIS vs ICNF discrepancies is a future-work analysis, not a pilot deliverable.
- **No VIIRS active-fire ingestion.** Different product (thermal anomaly, not perimeter); AUXILIARY per `docs/data_sources.md`.
- **No spatial pre-aggregation by municipality.** Phase 10/11 do portfolio aggregation; this phase ships row-level perimeters.

Surface anything from this list as a question before doing it.

## Done when

- All test gates pass.
- A live run against the pilot AOI produces an `outputs/parquet/icnf_burns_<run_id>.parquet` with rows from at least 5 distinct vintages.
- Every row Pydantic-validates as `BurnPerimeter`.
- CI `validate-schemas` job extended to spot-check burn GeoParquet schema.
- `prompts/_session_log.md` has a new entry.
- A PR exists on `main` with green CI, a one-paragraph description, and small ArcGIS-REST fixture JSON under `tests/fixtures/icnf/`.
