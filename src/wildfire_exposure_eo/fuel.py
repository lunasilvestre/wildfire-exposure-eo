"""Fuel-layer derivation from EFFIS fuel map + DGT COSc land-cover (WU-5).

Pure raster reclass + resample — no network, no ML. All inputs are read from
the WU-3 cache. The output is a 2-band COG (fuel class, severity × 100) on the
explicit pilot/smoke grid at 10 m resolution (EPSG:32629).

Non-negotiables respected:
  #2 — CRS is explicit at every step; no implicit reprojection.
  #3 — Full provenance sidecar written next to the COG.
  #5 — Output is a COG with a STAC item.
  #10 — AOI is read from file; no hardcoded coordinates.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rasterio
import rasterio.transform
import yaml
from rasterio.crs import CRS
from rasterio.warp import Resampling, reproject, transform_bounds

from wildfire_exposure_eo.schemas.fuel_layer import (
    Crosswalk,
    CrosswalkEntry,
    FuelLayerProvenance,
    GridSpec,
)

log = logging.getLogger(__name__)

# Internal class code used for non-fuel pixels (EFFIS nodata=0 and COSc non-fuel)
_NON_FUEL_CLASS: int = 0
_NON_FUEL_SEVERITY: float = 0.0

# COSc class codes that represent non-fuel surfaces (CLAUDE.md non-negotiable #1:
# values read from the official QML legend shipped inside the DGT zip).
_COSC_NON_FUEL_CODES: frozenset[int] = frozenset(
    {
        100,  # Artificializado (artificial/built-up)
        211,  # Culturas anuais de outono/inverno (winter annual crops)
        212,  # Culturas anuais de primavera/verão (summer annual crops)
        213,  # Outras áreas agrícolas (other agricultural)
        500,  # Superfícies sem vegetação (bare surfaces, rock)
        610,  # Zonas húmidas (wetlands)
        620,  # Água (water bodies)
    }
)

# COSc class code for spontaneous herbaceous vegetation (post-fire / abandoned land).
# Rule 2: where EFFIS says forest (models 8-10) but COSc says herbaceous, trust
# COSc -- the stand has likely burned or been cleared since the EFFIS vintage.
_COSC_HERBACEOUS_CODE: int = 420

# EFFIS NFFL codes considered "forest" for the purpose of COSc rule 2.
_EFFIS_FOREST_CODES: frozenset[int] = frozenset({8, 9, 10})

# EFFIS nodata value (uint8 raster)
_EFFIS_NODATA: int = 0

# COSc nodata value (uint16 raster, per QML: 65535)
_COSC_NODATA: int = 65535

# Output COG nodata — 255 reserved (uint8)
_COG_NODATA: int = 255

# Output grid CRS
_GRID_CRS = "EPSG:32629"


# ── SHA helpers ────────────────────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Public functions ───────────────────────────────────────────────────────────


def pilot_grid(aoi_path: Path, *, resolution_m: int = 10) -> GridSpec:
    """Compute an explicit raster grid for the AOI in EPSG:32629.

    The grid is snapped outward so every AOI envelope vertex lands on a
    grid cell boundary — identical calls with the same file return byte-
    identical transforms (determinism requirement).

    CRS is EPSG:32629 (UTM zone 29N — the standard projected CRS for
    mainland Portugal). Resolution is 10 m by default.
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    aoi_raw = json.loads(aoi_path.read_text())
    features = aoi_raw.get("features", [aoi_raw])
    geom_wgs84 = unary_union([shape(f["geometry"]) for f in features])
    aoi_sha = _sha256_bytes(aoi_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))

    dst_crs = CRS.from_epsg(32629)
    src_crs = CRS.from_epsg(4326)
    left, bottom, right, top = transform_bounds(src_crs, dst_crs, *geom_wgs84.bounds)

    # Snap outward to the next multiple of resolution_m
    res = float(resolution_m)
    left = np.floor(left / res) * res
    bottom = np.floor(bottom / res) * res
    right = np.ceil(right / res) * res
    top = np.ceil(top / res) * res

    width = round((right - left) / res)
    height = round((top - bottom) / res)

    transform = rasterio.transform.from_bounds(left, bottom, right, top, width, height)

    return GridSpec(
        crs=_GRID_CRS,
        transform=tuple(transform[:6]),
        width=width,
        height=height,
        resolution_m=resolution_m,
        aoi_geometry_sha=aoi_sha,
    )


def load_crosswalk(path: Path) -> Crosswalk:
    """Load and Pydantic-validate config/fuel_crosswalk.yaml.

    The file SHA-256 is embedded in the returned object to anchor provenance.
    """
    raw = yaml.safe_load(path.read_text())
    crosswalk_sha = _sha256_file(path)

    entries = tuple(CrosswalkEntry.model_validate(e) for e in raw.get("entries", []))
    return Crosswalk(
        version=raw["version"],
        source=raw["source"],
        source_taxonomy=raw["source_taxonomy"],
        internal_taxonomy_ref=raw["internal_taxonomy_ref"],
        cosc_herbaceous_override_severity=float(raw["cosc_herbaceous_override_severity"]),
        entries=entries,
        crosswalk_sha=crosswalk_sha,
    )


def reclass_effis(
    effis_path: Path,
    grid: GridSpec,
    crosswalk: Crosswalk,
) -> tuple[np.ndarray, np.ndarray]:
    """Reproject the EFFIS raster to grid and apply the crosswalk.

    Returns two uint8 arrays on the pilot grid:
      - klass: EFFIS NFFL code (0 = non-fuel / EFFIS nodata)
      - severity_x100: severity × 100 (0–100; EFFIS-nodata pixels map to
        class 0 / severity 0, so every output pixel is assigned — the COG
        nodata value 255 is reserved but not produced on this path)

    CRS is set explicitly from the EFFIS file; reprojection uses nearest-
    neighbour (categorical source — no interpolation of class codes).
    """
    dst_crs = CRS.from_string(grid.crs)
    transform = rasterio.transform.Affine(*grid.transform)

    with rasterio.open(effis_path) as ds:
        src_crs = ds.crs
        if src_crs is None:
            raise ValueError(f"EFFIS raster has no embedded CRS: {effis_path}")
        log.debug("EFFIS source CRS: %s, res: %s", src_crs, ds.res)

        src_data = ds.read(1)
        src_transform = ds.transform
        src_nodata = int(ds.nodata) if ds.nodata is not None else _EFFIS_NODATA

    # Reproject to the pilot grid (nearest-neighbour — categorical data)
    dst_raw = np.zeros((grid.height, grid.width), dtype=np.uint8)
    reproject(
        source=src_data.astype(np.uint8),
        destination=dst_raw,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=transform,
        dst_crs=dst_crs,
        resampling=Resampling.nearest,
        src_nodata=src_nodata,
        dst_nodata=0,
    )

    # Apply crosswalk: non-nodata pixels get class+severity; nodata → 255
    klass = np.full_like(dst_raw, _COG_NODATA, dtype=np.uint8)
    severity_x100 = np.full_like(dst_raw, _COG_NODATA, dtype=np.uint8)

    unique_codes = np.unique(dst_raw[dst_raw != 0])
    for code in unique_codes:
        mask = dst_raw == int(code)
        _ic, sev = crosswalk.severity_for_code(int(code))
        # Band 1 stores the EFFIS NFFL code directly (WU-6 decodes via crosswalk).
        klass[mask] = int(code)
        severity_x100[mask] = round(sev * 100)

    # Pixels that were EFFIS nodata (0) — mark as non-fuel class code 0
    effis_nodata_mask = dst_raw == 0
    klass[effis_nodata_mask] = 0
    severity_x100[effis_nodata_mask] = 0

    return klass, severity_x100


def _apply_cosc_rules(
    klass: np.ndarray,
    severity_x100: np.ndarray,
    cosc_dst: np.ndarray,
    crosswalk: Crosswalk,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply the COSc decision table to EFFIS-derived arrays (pure array logic).

    Split from refine_with_cosc so the decision-table unit tests exercise the
    shipped implementation directly, without rasterio I/O.
    """
    out_klass = klass.copy()
    out_sev = severity_x100.copy()

    herbaceous_sev_x100 = round(crosswalk.cosc_herbaceous_override_severity * 100)

    # Rule 1: COSc non-fuel pixels → class 0, severity 0
    non_fuel_mask = np.isin(cosc_dst, list(_COSC_NON_FUEL_CODES))
    out_klass[non_fuel_mask] = 0
    out_sev[non_fuel_mask] = 0

    # Rule 2: COSc herbaceous + EFFIS forest → downgrade to herbaceous severity
    cosc_herbaceous = cosc_dst == _COSC_HERBACEOUS_CODE
    effis_forest = np.isin(klass, list(_EFFIS_FOREST_CODES))
    herb_override_mask = cosc_herbaceous & effis_forest
    out_klass[herb_override_mask] = 0  # treat as non-fuel-class (herbaceous → no EFFIS code)
    out_sev[herb_override_mask] = herbaceous_sev_x100

    # Rule 3: all other COSc codes — retain EFFIS class and severity (no-op)

    return out_klass, out_sev


def refine_with_cosc(
    klass: np.ndarray,
    severity_x100: np.ndarray,
    cosc_path: Path,
    grid: GridSpec,
    crosswalk: Crosswalk,
) -> tuple[np.ndarray, np.ndarray]:
    """Refine the EFFIS-derived arrays using DGT COSc land-cover.

    Decision table (each rule maps to one unit test):

    Rule 1 — COSc non-fuel: if COSc is any non-fuel code (artificial,
      agricultural, bare, water, wetlands) → set class=0, severity=0,
      regardless of EFFIS. COSc is the more recent and higher-resolution
      source for settled / managed surfaces.

    Rule 2 — COSc herbaceous overrides forest: if COSc code is 420
      (Vegetação herbácea espontânea) AND EFFIS class is in {8, 9, 10}
      (timber litter group = forest) → trust COSc state (the stand was
      likely burned or cleared since the EFFIS 2023 vintage) and downgrade
      to the herbaceous severity defined in the crosswalk header.

    Rule 3 — Otherwise EFFIS stands: for all other COSc codes (shrubland 410,
      forest 311–323, etc.) the EFFIS class and severity are retained.

    CRS is set explicitly from the COSc file; reprojection uses nearest-
    neighbour (categorical source).
    """
    dst_crs = CRS.from_string(grid.crs)
    transform = rasterio.transform.Affine(*grid.transform)

    with rasterio.open(cosc_path) as ds:
        src_crs = ds.crs
        if src_crs is None:
            raise ValueError(f"COSc raster has no embedded CRS: {cosc_path}")
        log.debug("COSc source CRS: %s, res: %s", src_crs, ds.res)

        src_data = ds.read(1)
        src_transform = ds.transform
        src_nodata_val = int(ds.nodata) if ds.nodata is not None else _COSC_NODATA

    # Reproject COSc to the pilot grid (nearest-neighbour — categorical)
    cosc_dst = np.zeros((grid.height, grid.width), dtype=np.uint16)
    reproject(
        source=src_data,
        destination=cosc_dst,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=transform,
        dst_crs=dst_crs,
        resampling=Resampling.nearest,
        src_nodata=src_nodata_val,
        dst_nodata=0,
    )

    return _apply_cosc_rules(klass, severity_x100, cosc_dst, crosswalk)


def write_fuel_cog(
    klass: np.ndarray,
    severity_x100: np.ndarray,
    grid: GridSpec,
    path: Path,
    *,
    provenance: FuelLayerProvenance,
) -> Path:
    """Write a 2-band COG: band 1 = fuel_class (uint8), band 2 = severity_x100 (uint8).

    Nodata = 255 for both bands.
    Resampling is nearest (source is categorical — no interpolation).
    A JSON provenance sidecar is written next to the COG.

    Band 1 (fuel_class): EFFIS NFFL code (1–13); 0 = non-fuel / COSc override.
    Band 2 (severity_x100): severity × 100, range 0–100; 0 for non-fuel pixels.
    """
    from rasterio.shutil import copy as rio_copy

    path.parent.mkdir(parents=True, exist_ok=True)
    transform = rasterio.transform.Affine(*grid.transform)
    dst_crs = CRS.from_string(grid.crs)

    tags = {
        "WILDFIRE_EXPOSURE_EO_PROVENANCE": provenance.model_dump_json(),
        "RUN_ID": provenance.run_id,
        "BAND_1": "fuel_class (EFFIS NFFL code; 0=non-fuel or COSc override)",
        "BAND_2": "severity_x100 (severity * 100, 0–100; 255 reserved as nodata, unused)",
        "VALUE_DESCRIPTION": (
            "Fuel-class raster derived from EFFIS European Fuel Map crosswalk "
            "refined by DGT COSc 2024 land-cover. "
            f"EFFIS native resolution: {provenance.effis_native_res_m:.0f} m "
            "(coarser than 10 m output grid). "
            "COS species-level refinement is future work."
        ),
    }

    tmp_path = path.with_suffix(".raw.tif")
    try:
        with rasterio.open(
            tmp_path,
            "w",
            driver="GTiff",
            height=grid.height,
            width=grid.width,
            count=2,
            dtype="uint8",
            crs=dst_crs,
            transform=transform,
            nodata=_COG_NODATA,
        ) as tmp:
            tmp.write(klass, 1)
            tmp.write(severity_x100, 2)
            tmp.update_tags(**tags)
            tmp.update_tags(1, name="fuel_class")
            tmp.update_tags(2, name="severity_x100")

        # Convert to COG in one copy step (rasterio >= 1.3 COG driver)
        rio_copy(
            tmp_path,
            path,
            driver="COG",
            compress="deflate",
            resampling="nearest",
            blocksize=512,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    sidecar = path.with_suffix(".json")
    sidecar.write_text(
        json.dumps(provenance.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    log.info("fuel-layer COG: %s (+sidecar %s)", path, sidecar.name)
    return path


def write_stac_item(
    cog_path: Path,
    provenance: FuelLayerProvenance,
    *,
    stac_root: Path = Path("stac"),
) -> Path:
    """Register the fuel-layer COG as a STAC item under `stac/fuel-layer/`.

    Appends to the existing catalog (attached child pattern — see WU-1 session
    log: `Collection.from_file` detaches, so we always use `catalog.get_child`).
    The item bbox is reprojected to WGS84 for STAC compliance.
    """
    import pystac
    from pyproj import Transformer
    from shapely.geometry import box, mapping

    # Derive WGS84 bbox from the grid
    grid = provenance.grid
    t = rasterio.transform.Affine(*grid.transform)
    left = t.c
    top = t.f
    right = left + t.a * grid.width
    bottom = top + t.e * grid.height

    transformer = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    minx, miny = transformer.transform(left, bottom)
    maxx, maxy = transformer.transform(right, top)
    bbox = [float(minx), float(miny), float(maxx), float(maxy)]
    geom = mapping(box(bbox[0], bbox[1], bbox[2], bbox[3]))

    catalog_path = stac_root / "catalog.json"
    if catalog_path.exists():
        catalog = pystac.Catalog.from_file(str(catalog_path))
    else:
        catalog = pystac.Catalog(
            id="wildfire-exposure-eo",
            title="wildfire-exposure-eo STAC catalog",
            description=(
                "STAC-native artifacts of the wildfire-exposure-eo demonstrator: "
                "per-collection rasters and vectors produced by the pipeline, "
                "with full per-run provenance."
            ),
        )

    child = catalog.get_child("fuel-layer")
    if child is not None:
        if not isinstance(child, pystac.Collection):
            raise TypeError(f"stac child 'fuel-layer' is a {type(child).__name__}")
        collection = child
    else:
        collection = pystac.Collection(
            id="fuel-layer",
            title="Fuel-layer COG (EFFIS + COSc crosswalk)",
            description=(
                "Per-run fuel-class raster derived from the EFFIS pan-European fuel "
                "map refined by DGT COSc land-cover via the Scott & Burgan crosswalk. "
                "Band 1: EFFIS NFFL fuel-model class (uint8, 0=non-fuel). "
                "Band 2: severity weight × 100 (uint8). "
                "Expert-set severities; not a calibrated fire-behaviour model. "
                "See config/fuel_crosswalk.yaml."
            ),
            extent=pystac.Extent(
                spatial=pystac.SpatialExtent([bbox]),
                temporal=pystac.TemporalExtent([[datetime.now(UTC), None]]),
            ),
            license="MIT",
        )
        catalog.add_child(collection)

    item_id = f"fuel-layer-{provenance.run_id}"
    if collection.get_item(item_id) is not None:
        raise ValueError(f"STAC item {item_id} already exists under {stac_root}")

    item = pystac.Item(
        id=item_id,
        geometry=geom,
        bbox=bbox,
        datetime=datetime.now(UTC),
        properties={
            "wildfire_exposure_eo:provenance": provenance.model_dump(mode="json"),
        },
    )
    item.add_asset(
        "fuel_class",
        pystac.Asset(
            href=str(cog_path.resolve()),
            title="Fuel-class COG (EFFIS NFFL class + severity × 100)",
            description=(
                f"Band 1: EFFIS NFFL fuel-model code (0=non-fuel). "
                f"Band 2: severity × 100. "
                f"EFFIS native res {provenance.effis_native_res_m:.0f} m, "
                f"output grid 10 m EPSG:32629. "
                f"Crosswalk version {provenance.crosswalk_version}."
            ),
            media_type="image/tiff; application=geotiff; profile=cloud-optimized",
            roles=["data"],
        ),
    )
    collection.add_item(item)
    collection.extent = pystac.Extent.from_items(list(collection.get_items()))

    catalog.normalize_hrefs(str(stac_root))
    item.make_asset_hrefs_relative()
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)

    item_path = Path(item.get_self_href() or "")
    log.info("[fuel-layer] STAC item %s → %s", item_id, item_path)
    return item_path
