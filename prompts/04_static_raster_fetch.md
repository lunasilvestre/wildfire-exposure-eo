# Prompt 04 — Static raster fetch (Phase 4)

> **Close-out amendment (2026-06-09).** Executed as **WU-3** of
> [`00_CLOSEOUT_PLAN.md`](00_CLOSEOUT_PLAN.md). Scope unchanged, framing
> changed: EFFIS + COS/COSc are now direct **score inputs** for the WU-5
> fuel-layer derivation — not Phase 6/7 training inputs (training was cut).
> The May 14 session on this prompt died without deliverables — start fresh.

## Purpose

Implement `wildfire-exposure-eo fetch-rasters`: a CLI command that fetches the four static raster sources that aren't STAC-queryable in our toolchain, caches them under `data/cache/`, verifies integrity, and emits a per-source manifest with full provenance. Sources:

- **ETH Global Canopy Height 2020** — 3-degree COG tiles from `libdrive.ethz.ch`.
- **EFFIS European Fuel Map** — single GeoTIFF from `forest-fire.emergency.copernicus.eu`.
- **DGT COSc 2024 Pré-Verão** — 10 m raster from DGT Centro de Dados.
- **DGT COS 2023 v1** — vector GeoPackage from `geo2.dgterritorio.gov.pt`.

The pipeline cannot start label-fusion (Phase 6) or training (Phase 7) without these on disk. Direct-download URLs are stable but vary in size (KB to GB) and require checksum-based idempotency. The output is a small manifest JSON listing what was fetched, when, with what URL, what SHA-256, and what bytes.

## Prerequisites (do not start without these)

- [ ] `prompts/01_data_audit.md` closed; audit GREEN for ETH GCH, EFFIS, DGT COSc, DGT COS.
- [ ] `data/aoi/pilot.geojson` committed (drives ETH GCH tile selection).
- [ ] `scripts/00_eth_gch_fetch.sh`, `scripts/00_effis_fetch.sh`, `scripts/00_dgt_fetch.sh`, `scripts/00_icnf_fetch.sh` already in the repo as reference. **This prompt should not just shell-out to those — port their logic into Python with proper error handling, retries, and Pydantic-validated provenance.** The shell scripts are starting documentation, not the target architecture.
- [ ] `data/` is gitignored except for AOI / taxonomy / crosswalks. `data/cache/` is the fetch destination; nothing here is committed.
- [ ] Read `docs/data_sources.md` end-to-end for the URL patterns, license posture, and known gaps per source.
- [ ] Read `CLAUDE.md`. Hard-hitting non-negotiables here: #1 (no invented URLs; use what `audit` verified), #4 (deterministic — same call returns same cache hit), #7 (no PII; no private operator data).

## Deliverables

1. **`src/wildfire_exposure_eo/static_rasters.py`** — pure functions:
   - `compute_eth_gch_tile_ids(aoi: ShapelyGeom) -> list[str]` — given an AOI, compute the 3-degree tile names that cover it (e.g. `N39W009`, `N42W009`). Tile names follow ETH's SW-corner convention: `N<lat_2d><E|W><lon_3d>`.
   - `fetch_eth_gch_tile(tile_id: str, *, cache_dir: Path, force: bool = False) -> FetchRecord` — fetches one ETH GCH 3° COG. Verifies TIFF magic bytes (`49 49 2A 00` for little-endian) via range-GET on the first 16 bytes before committing the full download. Idempotent — if the local file exists and its SHA-256 matches the expected (computed once and cached in a sidecar `.sha256` file), skip.
   - `fetch_effis_fuel_map(*, cache_dir, force=False) -> FetchRecord` — single GeoTIFF from EFFIS's data-and-services portal. Same idempotency pattern.
   - `fetch_dgt_cosc(vintage: str, *, cache_dir, force=False) -> FetchRecord` — DGT COSc raster, vintage = "2024_pre_verao" by default. Pulls from the DGT CDD endpoint documented in `scripts/00_dgt_fetch.sh`.
   - `fetch_dgt_cos(vintage: str, *, cache_dir, force=False) -> FetchRecord` — DGT COS GeoPackage, vintage = "2023_v1" by default.
   - `build_fetch_manifest(records: list[FetchRecord], aoi: ShapelyGeom) -> StaticRasterManifest` — top-level orchestrator. Composes per-source records into a single manifest with run-level provenance.
   - `write_manifest(manifest, path) -> Path` — JSON output with stable key ordering.

2. **`src/wildfire_exposure_eo/schemas/static_raster_manifest.py`** — Pydantic v2 models:

   ```python
   class FetchRecord(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")

       source_id: Literal["eth-gch-2020", "effis-fuel-map", "dgt-cosc", "dgt-cos"]
       vintage: str                         # "2020", "2023", "2024_pre_verao", "2023_v1"
       tile_id: str | None                  # ETH GCH tile name, or None for single-file sources
       source_url: str                      # exact URL fetched
       local_path: str                      # path under data/cache/
       bytes_downloaded: int
       sha256: str
       fetched_at_utc: datetime
       cache_hit: bool                      # True if local file matched expected SHA
       license: str                         # "CC-BY 4.0", "CC-BY-NC 4.0", "Free, no auth", etc.
       attribution: str                     # required attribution string per source

   class StaticRasterManifest(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")

       run_id: str
       code_commit_sha: str
       aoi_path: str
       aoi_geometry_sha: str
       resolved_at_utc: datetime
       records: list[FetchRecord]
       totals_bytes: int
       totals_by_source: dict[str, int]
   ```

3. **CLI subcommand**:

   ```
   uv run wildfire-exposure-eo fetch-rasters \
     --aoi data/aoi/pilot.geojson \
     --cache-dir data/cache \
     --out outputs/manifests/static_rasters_{run_id}.json \
     [--cosc-vintage 2024_pre_verao] \
     [--cos-vintage 2023_v1] \
     [--only eth-gch,effis,cosc,cos] \
     [--force] \
     [--smoke]
   ```

   `--only` filters which sources to fetch (useful for iterating). `--force` re-downloads even if cache-hit. `--smoke` switches to the smoke AOI (which still needs ETH GCH for at least one tile — pick one that includes Sever do Vouga).

4. **`tests/unit/test_static_rasters.py`** — at minimum:
   - ETH GCH tile-name computation: given the smoke AOI, the function returns `["N39W009"]` (Sever do Vouga sits in that 3° tile). Property test: any AOI in [-180,180]×[-90,90] produces tile names matching the regex `N\d{2}[EW]\d{3}`.
   - Idempotency: with a stubbed `requests.get` that asserts it's *not* called when a valid cached file exists, confirm `fetch_eth_gch_tile` short-circuits on SHA match.
   - TIFF magic check: a fixture file with mangled magic bytes fails fast (no full download attempted).
   - Per-source error paths: 5xx + retry path; 404 + immediate-fail path; SHA mismatch + log + re-download path.
   - Provenance population: every `FetchRecord` field populated and Pydantic-validates.

5. **`tests/integration/test_static_rasters_smoke.py`** — runs `fetch-rasters --smoke --only eth-gch` against a fixture HTTP server (use `pytest-httpserver` if available; otherwise monkeypatch `requests.get` to return canned bytes). Asserts:
   - Manifest file exists.
   - Manifest round-trips through `StaticRasterManifest.model_validate_json`.
   - Cache directory contains the expected file.
   - `cache_hit=False` on first run, `cache_hit=True` on second run with same arguments.

6. **`prompts/_session_log.md`** — append entry.

## Constraints

- **No invented URLs.** Use the URL patterns verified in `docs/data_sources.md` and in `scripts/00_*_fetch.sh`. If a URL fails, log + retry, then fall back to the documented alternative (e.g., for EFFIS, the WMS endpoint is documented as a fallback for the GeoTIFF download).
- **Idempotency is mandatory, not optional.** Re-running the command without `--force` must short-circuit on cache hits. The `cache_hit` field on `FetchRecord` distinguishes the two paths in the audit.
- **SHA-256 sidecar files** — for every downloaded raster, write a `<filename>.sha256` next to it containing the hex digest. On next run, this is the idempotency anchor.
- **License + attribution per record.** Hardcoded constants per source, copied from `docs/data_sources.md`. The compliance audit later in the project wants these structured, not buried in prose.
- **No PII or private operator data.** No e-Redes, REN, DGEG. CLAUDE.md #7.
- **Pre-download magic-byte verification for COGs.** Range-GET the first 16 bytes and assert the TIFF magic before committing to a multi-GB download. If the server returns HTML or an error page disguised as 200, this catches it cheaply.
- **Retries + fallback endpoints.** Each fetch function has: primary URL, 2 retries with exponential backoff, then either fallback URL or hard fail with a structured error.
- **Cache directory is gitignored** (`data/cache/` per `.gitignore`). Don't commit raster bytes.

## Test gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright src tests
uv run pytest -q
uv run pytest tests/integration/test_static_rasters_smoke.py -v
uv run wildfire-exposure-eo fetch-rasters --smoke --only eth-gch    # live; smallest live fetch
```

All must pass. The full live run (`fetch-rasters --aoi data/aoi/pilot.geojson`) is the verification step below.

## Verification (do this before declaring done)

1. **Run live against the pilot AOI, all sources** — `uv run wildfire-exposure-eo fetch-rasters --aoi data/aoi/pilot.geojson`. Document totals + per-source bytes in the session log.
2. **Inspect ETH GCH tile** — `rio info data/cache/eth-gch-2020/ETH_GlobalCanopyHeight_10m_2020_N39W009_Map.tif`. Confirm CRS is EPSG:4326, dtype is float32, nodata is set, and the AOI bbox falls within the tile's footprint.
3. **Inspect EFFIS fuel map** — `rio info data/cache/effis/effis_european_fuel_map.tif`. Confirm CRS, dtype (likely uint8 for class IDs), and that the bbox covers Iberia.
4. **Inspect DGT COSc raster** — `rio info data/cache/dgt-cosc/cosc_2024_pre_verao.tif`. Confirm CRS is EPSG:3763 (Portugal national grid). Note: reprojection to EPSG:4326 is *not* this phase's job — Phase 6 handles label-grid alignment.
5. **Inspect DGT COS GeoPackage** — `ogrinfo -so data/cache/dgt-cos/cos_2023_v1.gpkg`. Confirm layer count, geometry types, and that the species-code attribute column is present.
6. **Run twice, confirm idempotency** — the second `fetch-rasters` call should produce `cache_hit=true` for every record. Wall-clock should drop from minutes to seconds.
7. **Spot-check provenance** — open the JSON manifest, take any `FetchRecord`, validate it through `FetchRecord.model_validate(...)`; pretty-print and confirm every field is populated.
8. **Diff against `audit`** — the `audit` command's per-source GREEN/YELLOW status should match what this phase actually fetches. If a fetch fails where `audit` says GREEN, the audit's reachability check is too lenient — file as a follow-up.

## Out of scope for this prompt

- **No reprojection.** Rasters are stored in their native CRS. Phase 6 reprojects when fusing labels onto the COSc grid.
- **No clipping to AOI.** Whole tiles are cached. Phase 6/7 read windows via `rioxarray.clip` or `stackstac` AOI clipping.
- **No mosaicing.** Each tile/file is stored independently. Mosaicing for visualisation happens in Phase 13 (STAC catalog assembly) if at all.
- **No HLS, Dynamic World, Meta CH, VIIRS NRT, IPMA FWI.** Each is documented under `data_sources.md` with its own status (AUXILIARY or FUTURE) and is *not* part of the pilot's static-raster set.
- **No symbology / colormap application.** That's a display-time concern.

Surface anything here as a question before doing it.

## Done when

- All test gates pass.
- A live run against the pilot AOI produces a manifest with 4 `FetchRecord` entries (one per source) and non-zero `totals_bytes`.
- Every record Pydantic-validates.
- A second run produces `cache_hit=true` for all records (idempotency proven).
- `prompts/_session_log.md` has a new entry.
- A PR exists on `main` with green CI, a one-paragraph description, and small fixtures under `tests/fixtures/static_rasters/`.
