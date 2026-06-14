"""Publish the pipeline's geodata as committed STAC assets (WU-9, prompt 15).

One-shot, idempotent publishing step:

1. **Scored-asset GeoParquet** — copies the backdated pilot
   ``exposure_<run_id>.parquet`` (the run the committed validation report
   describes, selected via the WU-7 metrics JSON) into
   ``stac/exposure-assets/<item-id>/`` and registers a new ``exposure-assets``
   collection + item, carrying the run-level provenance dict (identical across
   rows — asserted) in the item properties. CRS EPSG:4326, asserted.
2. **Fuel-class COGs** — copies each fuel-layer item's COG from the gitignored
   ``outputs/cogs/`` path into the item's directory under ``stac/fuel-layer/``
   and repoints the asset href at the committed file.
3. **Burn-scar COG** — 38 MB, over the repo's 2 000 kB committed-file cap:
   repoints the asset href at its Cloudflare R2 public URL (``--asset-base-url``,
   default ``https://wildfire.cheias.pt``). The file itself is uploaded to the R2
   bucket out-of-band (see prompts/_session_log.md); R2 serves it with CORS +
   byte-range so the static geobrowser can read the COG client-side.

Usage::

    uv run python scripts/15_publish_stac.py            # pilot artefacts
    uv run python scripts/15_publish_stac.py --dry-run  # report, write nothing

After running: ``uv run stac-validator stac/catalog.json --recursive``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import pystac

# Repo-root import shim so the script runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wildfire_exposure_eo.schemas import ScoredAsset

_ROOT = Path(__file__).resolve().parents[1]
_STAC_ROOT = _ROOT / "stac"
_PARQUET_DIR = _ROOT / "outputs" / "parquet"
_COG_DIR = _ROOT / "outputs" / "cogs"
_VAL_DIR = _ROOT / "outputs" / "validation"

#: Cloudflare R2 bucket (custom domain ``wildfire.cheias.pt``) hosting the geodata
#: too large for the 2 000 kB committed-file cap — the burn-scar COG. CORS +
#: byte-range enabled (verified) so the static Pages geobrowser reads the COG
#: client-side. See prompts/_session_log.md.
_ASSET_BASE_URL = "https://wildfire.cheias.pt"
#: IANA-registered media type for (Geo)Parquet assets.
_PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
_COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


def _validated_run_id() -> str:
    """run_id of the backdated pilot run the committed validation report used."""
    cands = sorted(p for p in _VAL_DIR.glob("metrics_*.json") if "_smoke_" not in p.name)
    if not cands:
        raise FileNotFoundError(f"no pilot metrics JSON under {_VAL_DIR}")
    return str(json.loads(cands[-1].read_text())["source_run_id"])


def publish_exposure_assets(catalog: pystac.Catalog, run_id: str, *, dry_run: bool) -> None:
    """Copy the scored GeoParquet into stac/ and register collection + item."""
    item_id = f"exposure-assets-{run_id}"
    src = _PARQUET_DIR / f"exposure_{run_id}.parquet"
    if not src.exists():
        raise FileNotFoundError(f"validated exposure parquet missing: {src}")

    gdf = gpd.read_parquet(src)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{src.name}: expected explicit EPSG:4326, got {gdf.crs}")
    prov_strings = set(gdf["provenance"])
    if len(prov_strings) != 1:
        raise ValueError(f"{src.name}: expected run-level provenance, got {len(prov_strings)}")
    # Validate the provenance contract before publishing (non-negotiable #3).
    ScoredAsset.model_validate(gdf.drop(columns="geometry").iloc[0].to_dict())
    provenance = json.loads(next(iter(prov_strings)))

    child = catalog.get_child("exposure-assets")
    if child is not None and not isinstance(child, pystac.Collection):
        raise TypeError(f"stac child 'exposure-assets' is a {type(child).__name__}")
    if child is not None and child.get_item(item_id) is not None:
        print(f"exposure-assets: item {item_id} already published — skipping")
        return
    if dry_run:
        print(f"[dry-run] would publish {item_id} ({len(gdf)} rows) from {src.name}")
        return

    minx, miny, maxx, maxy = (float(v) for v in gdf.total_bounds)
    bbox = [minx, miny, maxx, maxy]
    geom = {
        "type": "Polygon",
        "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
    }
    run_dt = datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)

    if isinstance(child, pystac.Collection):
        collection = child
    else:
        collection = pystac.Collection(
            id="exposure-assets",
            title="Scored critical-infrastructure assets (exposure rank)",
            description=(
                "Per-run GeoParquet of OSM-derived critical-infrastructure assets "
                "scored for relative wildfire exposure. One row per asset with the "
                "full ScoredAsset provenance contract (source artefact SHA-256s, "
                "STAC item ids, config + code commit). The exposure score is a "
                "relative, AOI-normalised screening rank in [0, 1] and "
                "exposure_rank the integer position (1 = most exposed) — never a "
                "probability of fire. CRS EPSG:4326."
            ),
            extent=pystac.Extent(
                spatial=pystac.SpatialExtent([bbox]),
                temporal=pystac.TemporalExtent([[run_dt, None]]),
            ),
            license="MIT",
        )
        catalog.add_child(collection)

    item = pystac.Item(
        id=item_id,
        geometry=geom,
        bbox=bbox,
        datetime=run_dt,
        properties={
            "wildfire_exposure_eo:provenance": provenance,
            "wildfire_exposure_eo:n_assets": len(gdf),
            "start_datetime": f"{provenance['window_start']}T00:00:00Z",
            "end_datetime": f"{provenance['window_end']}T23:59:59Z",
        },
    )
    item_dir = _STAC_ROOT / "exposure-assets" / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    dst = item_dir / src.name
    shutil.copy2(src, dst)
    item.add_asset(
        "exposure_assets",
        pystac.Asset(
            href=str(dst.resolve()),
            title="Scored-asset GeoParquet (exposure rank, full provenance)",
            description=(
                f"{len(gdf)} assets, EPSG:4326, snappy GeoParquet. Score window "
                f"{provenance['window_start']}..{provenance['window_end']} "
                f"(backdated run validated against the 2025 ICNF vintage — see "
                f"docs/validation_report.md). exposure_score is a relative, "
                f"AOI-normalised screening rank, not a probability."
            ),
            media_type=_PARQUET_MEDIA_TYPE,
            roles=["data"],
        ),
    )
    collection.add_item(item)
    collection.extent = pystac.Extent.from_items(list(collection.get_items()))
    print(f"exposure-assets: published {item_id} ({len(gdf)} rows, {dst.stat().st_size} B)")


def commit_fuel_cogs(catalog: pystac.Catalog, *, dry_run: bool) -> None:
    """Copy each fuel-layer COG next to its item and repoint the asset href."""
    collection = catalog.get_child("fuel-layer")
    if not isinstance(collection, pystac.Collection):
        raise TypeError("stac child 'fuel-layer' missing or not a Collection")
    for item in collection.get_items():
        asset = item.assets["fuel_class"]
        href_name = Path(asset.href).name
        item_self = item.get_self_href()
        if item_self is None:
            raise ValueError(f"{item.id}: item has no self href")
        item_dir = Path(item_self).parent
        committed = item_dir / href_name
        if committed.exists() and not asset.href.startswith("../../../outputs"):
            print(f"fuel-layer: {item.id} already committed — skipping")
            continue
        src = _COG_DIR / href_name
        if not src.exists():
            raise FileNotFoundError(f"{item.id}: source COG missing: {src}")
        if dry_run:
            print(f"[dry-run] would commit {src.name} → {committed}")
            continue
        shutil.copy2(src, committed)
        asset.href = str(committed.resolve())
        print(f"fuel-layer: committed {href_name} ({committed.stat().st_size} B)")


def point_burn_scar_at_r2(catalog: pystac.Catalog, asset_base_url: str, *, dry_run: bool) -> None:
    """Repoint the burn-scar asset href at its Cloudflare R2 public URL."""
    collection = catalog.get_child("burn-scar-recent")
    if not isinstance(collection, pystac.Collection):
        raise TypeError("stac child 'burn-scar-recent' missing or not a Collection")
    for item in collection.get_items():
        asset = item.assets["burn_scar_probability"]
        href_name = Path(asset.href).name
        url = f"{asset_base_url}/{href_name}"
        if asset.href == url:
            print(f"burn-scar-recent: {item.id} already points at R2 — skipping")
            continue
        if dry_run:
            print(f"[dry-run] would repoint {item.id} → {url}")
            continue
        asset.href = url
        # Drop the legacy GitHub-Release tag property if an earlier run set it.
        item.properties.pop("wildfire_exposure_eo:release_tag", None)
        print(f"burn-scar-recent: {item.id} → {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument(
        "--asset-base-url",
        default=_ASSET_BASE_URL,
        help="public base URL (Cloudflare R2) hosting the burn-scar COG",
    )
    args = parser.parse_args()

    catalog = pystac.Catalog.from_file(str(_STAC_ROOT / "catalog.json"))
    run_id = _validated_run_id()
    publish_exposure_assets(catalog, run_id, dry_run=args.dry_run)
    commit_fuel_cogs(catalog, dry_run=args.dry_run)
    point_burn_scar_at_r2(catalog, args.asset_base_url, dry_run=args.dry_run)
    if not args.dry_run:
        catalog.normalize_hrefs(str(_STAC_ROOT))
        for child in catalog.get_children():
            for item in child.get_items() if isinstance(child, pystac.Collection) else []:
                item.make_asset_hrefs_relative()
        catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
        print("catalog saved (self-contained, relative hrefs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
