# STAC fixture items

Minimal synthetic STAC items used by `tests/integration/test_stac_smoke.py`
to exercise the resolve-stac pipeline offline.

These are **synthetic** — IDs, dates, and asset hrefs are illustrative and
deliberately distinguishable from real Microsoft Planetary Computer items
(prefixes `FIXTURE_*`). They satisfy CLAUDE.md non-negotiable #1 because
they live under `tests/fixtures/` and never enter a production manifest.

Each file follows the STAC 1.0 Item spec (the `pystac.Item.from_file()`
parser is the consumer). Geometry overlaps the smoke AOI at
`data/aoi/smoke.geojson`.
