# Scaling notes

> This document describes the **documented path** for scaling beyond the pilot
> AOI. "Documented" is not "done" — the demonstrator runs on a single ~30×30 km
> pilot district (Sever do Vouga). A national rollout requires the items below.
> No production claims (CLAUDE.md non-negotiable #9).

---

## Current scope

The pipeline is designed for a single GeoJSON-defined AOI, processed in a single
session on a laptop (smoke path: ~2.5 min CPU; pilot: a few hours CPU + optional
GPU for burn-scar inference). All outputs are STAC-native (COG + GeoParquet);
large geodata is hosted on Cloudflare R2 (`wildfire.cheias.pt`) with full
CORS support for browser-client access.

The OSM asset extract, static-raster fetch, burn-scar inference, feature
extraction, scoring, and validation steps are all exposed as CLI commands
(`uv run wildfire-exposure-eo <cmd>`) and driven by `data/aoi/pilot.geojson`.
To run on a different AOI, freeze a new GeoJSON under `data/aoi/` (do not edit
`pilot.geojson` — it is frozen per CLAUDE.md non-negotiable #10) and pass it
with `--aoi`.

---

## Asset volume: the documented PostGIS path

The current pipeline stores per-asset features and scores as GeoParquet
(snappy-compressed, STAC-linked). The README "Stack" section already documents
the PostGIS path for production-grade asset volumes: when the number of scored
assets grows beyond what a single in-memory GeoDataFrame can handle, the
GeoParquet can be bulk-loaded into PostGIS and the zonal-statistics step
(currently via `exactextract`) can be run against a database cursor. This
is documented intent, not implemented code. The existing GeoParquet schema
(`src/wildfire_exposure_eo/schemas/scored_asset.py`) is compatible with this
path — no schema change is required.

---

## Cross-AOI comparability: the national reference-distribution requirement

Scores are **AOI-relative** (percentile-ranked within the AOI; see
`docs/limitations.md` L3). A score of 0.90 in Sever do Vouga is not comparable
to a score of 0.90 in Monchique without a national reference distribution.

The reference-distribution requirement is documented in
`docs/strategy.md` §7 item (6) and in `docs/operationalization.md` §7 risk #3.
Pillar 3 of the operationalization program (`prompts/20`) must resolve this
design decision before any absolute actionable thresholds can be published.

Until that work lands, all outputs from this repo carry the explicit boundary:
*"Scores are AOI-relative. A national rollout would need a national reference
distribution built from a representative sample of Portuguese mainland AOIs."*

---

## Geodata hosting: Cloudflare R2

Large raster outputs (burn-scar display COGs, ~36–49 MB) and large vector
outputs (ICNF Áreas Ardidas GeoJSON, ~8 MB) are hosted on Cloudflare R2 at
`wildfire.cheias.pt` with a CORS policy (`*` origin, GET/HEAD, `Range` header
allowed). This replaces the earlier GitHub Release approach, which served
byte-ranges (HTTP 206) but no `access-control-allow-origin` header, blocking
browser-client reads from the static GitHub Pages site.

The R2 bucket is `wildfire-exposure-eo`; the upload recipe (rclone `r2:` remote,
bucket root, sha256 verified) is recorded in the WU-9/WU-10 session-log entries
in `prompts/_session_log.md`. The upload step is a **human action** (not
automated in CI) to preserve the human-in-the-loop boundary on live data
publication.

A national rollout would require either more R2 objects (one set per AOI) or
a tile-serving backend. The existing TiTiler instance at `api.cheias.pt/raster`
is operational and already referenced by the EO-MCP server; it can serve any
COG hosted on R2 without a code change.

---

## Burn-scar inference: GPU vs CPU

The burn-scar inference step (`uv run wildfire-exposure-eo burn-scar`) runs on
CPU or GPU. The pilot run (179 Sentinel-2 scenes, 12-month trailing window)
completed in ~19 minutes on a GPU (NVIDIA RTX 3090). CPU runtime scales roughly
linearly with scene count and is dominated by the tiled inference forward pass;
a rough estimate is 5–10 minutes per scene on a modern CPU. For a national
rollout (hundreds of AOIs × 100–200 scenes each), a persistent GPU instance or
a managed inference endpoint would be needed. This is documented but not
implemented.

---

## What "scaling" is not

This section does not describe any of the following as done or planned:

- Real-time or near-real-time fire-danger alerting (requires a live FWI source
  and operational infrastructure, neither of which this project provides)
- Per-asset notification or alerting to infrastructure operators
- Integration with e-Redes, REN, or DGEG private asset registries (non-negotiable
  #7 forbids private operator data)
- Probabilistic fire-spread modelling at the asset level

Any of the above would require a funded, operationally maintained service — a
different project with a different data contract.

<!-- maintained alongside docs/operationalization.md; update when the R2 layout or hosting model changes -->
