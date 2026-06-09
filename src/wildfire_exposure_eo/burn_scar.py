"""Prithvi burn-scar inference over the AOI (Stage 1b, prompt 09).

Runs the pretrained `Prithvi-EO-2.0-300M-BurnScars` checkpoint (inference
only, frozen weights) over the trailing window of Sentinel-2 L2A scenes and
composites a single per-pixel max raster of the model's class-1 softmax
score, written as a COG with full provenance.

Terminology guard (CLAUDE.md): this detects burn *scars* — post-event
spectral signatures of fires that already happened. The raster value is a
*burn-scar inference probability*, not a calibrated probability that a pixel
burned, and not any form of fire forecast.

Radiometry note: MS PC serves S2 L2A digital numbers as published by ESA.
Processing baseline ≥ 04.00 carries a +1000 BOA offset that must be
subtracted before the /10000 reflectance scaling; the offset is applied
per item from its `s2:processing_baseline` property.

Heavy dependencies (torch, terratorch, stackstac, rioxarray) are imported
inside the functions that need them so the module stays importable in
test/CLI contexts that never run inference.
"""

from __future__ import annotations

import calendar
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from shapely.geometry import mapping

from wildfire_exposure_eo.schemas import HF_MODEL_ID_PLACEHOLDER, BurnScarConfig, BurnScarRun
from wildfire_exposure_eo.stac import (
    PC_STAC_URL,
    _default_client_factory,
    _item_datetime,
)

if TYPE_CHECKING:
    import numpy as np
    import pystac
    import xarray as xr
    from pystac_client import Client
    from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/burn_scar.yaml")
NODATA = -9999.0
OUTPUT_CRS = "EPSG:4326"
RESAMPLING = "nearest"  # probability raster: never interpolate values (prompt 09)
SCL_ASSET = "SCL"
PC_SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"
S2_COLLECTION = "sentinel-2-l2a"

#: SAS tokens per collection, with their expiry. Refreshed per scene read so
#: long pilot runs never hold a stale token.
_SAS_CACHE: dict[str, tuple[str, datetime]] = {}


def load_burn_scar_config(path: Path = DEFAULT_CONFIG_PATH) -> BurnScarConfig:
    """Parse and validate `config/burn_scar.yaml`."""
    import yaml

    payload = yaml.safe_load(path.read_text())
    return BurnScarConfig.model_validate(payload)


@dataclass(frozen=True)
class ModelHandle:
    """A loaded, eval-mode Prithvi burn-scar model plus its preprocessing contract."""

    model: Any  # terratorch SemanticSegmentationTask (torch.nn.Module), eval mode
    hf_model_id: str
    hf_revision_sha: str
    model_version: str
    checkpoint_path: Path
    model_config_path: Path
    means: tuple[float, ...]
    stds: tuple[float, ...]
    device: str


def resolve_prithvi_burn_scar_model(
    config: BurnScarConfig | None = None,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    device: str | None = None,
) -> ModelHandle:
    """Download (cached) and load the pinned burn-scar checkpoint in eval mode.

    The model ID and revision come from `config/burn_scar.yaml`, never from
    code (CLAUDE.md non-negotiable #1). Raises `ValueError` while the config
    still carries the unverified placeholder. Normalisation means/stds are
    read from the model's own config file at the pinned revision.
    """
    cfg = config or load_burn_scar_config(config_path)
    if cfg.model.hf_model_id == HF_MODEL_ID_PLACEHOLDER:
        raise ValueError(
            "config/burn_scar.yaml still carries the placeholder "
            f"hf_model_id={HF_MODEL_ID_PLACEHOLDER!r}; verify the real model ID "
            "against the Hugging Face API before running inference "
            "(CLAUDE.md non-negotiable #1)."
        )

    import torch
    import yaml
    from huggingface_hub import hf_hub_download

    model_config_path = Path(
        hf_hub_download(
            repo_id=cfg.model.hf_model_id,
            filename=cfg.model.config_file,
            revision=cfg.model.hf_revision_sha,
        )
    )
    checkpoint_path = Path(
        hf_hub_download(
            repo_id=cfg.model.hf_model_id,
            filename=cfg.model.checkpoint_file,
            revision=cfg.model.hf_revision_sha,
        )
    )

    model_cfg = yaml.safe_load(model_config_path.read_text())
    data_args = model_cfg["data"]["init_args"]
    means = tuple(float(v) for v in data_args["means"])
    stds = tuple(float(v) for v in data_args["stds"])

    from terratorch.cli_tools import LightningInferenceModel

    # from_config's signature says Path but it feeds LightningCLI, which
    # rejects non-string argv entries at runtime — hence the str() casts and
    # the targeted ignores for the (wrong) upstream annotation.
    lim = LightningInferenceModel.from_config(
        str(model_config_path),  # pyright: ignore[reportArgumentType]
        str(checkpoint_path),  # pyright: ignore[reportArgumentType]
    )
    model = lim.model
    model.eval()  # frozen backbone, inference only — asserted in unit tests
    resolved_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model.to(resolved_device)

    version = (
        f"{cfg.model.family}:{cfg.model.downstream_task}:"
        f"{cfg.model.backbone_param_count // 1_000_000}M"
    )
    logger.info(
        "[burn-scar] loaded %s @ %s on %s",
        cfg.model.hf_model_id,
        cfg.model.hf_revision_sha[:8],
        resolved_device,
    )
    return ModelHandle(
        model=model,
        hf_model_id=cfg.model.hf_model_id,
        hf_revision_sha=cfg.model.hf_revision_sha,
        model_version=version,
        checkpoint_path=checkpoint_path,
        model_config_path=model_config_path,
        means=means,
        stds=stds,
        device=resolved_device,
    )


def months_back(end: date, months: int) -> date:
    """`end` minus `months` calendar months, day clamped to the target month."""
    year, month = end.year, end.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(end.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def query_recent_s2(
    aoi: BaseGeometry,
    window_months: int = 12,
    *,
    max_cloud_cover: int = 30,
    window_end: date | None = None,
    client: Client | None = None,
) -> list[pystac.Item]:
    """Resolve S2 L2A items for the trailing window, deterministically ordered.

    Sorted by `(datetime, item_id)`; every returned ID is logged before any
    raster is read (CLAUDE.md verify-then-act). `window_end` defaults to
    today (UTC) — pass it explicitly for reproducible runs; the provenance
    record captures whichever window was used.
    """
    end = window_end or datetime.now(UTC).date()
    start = months_back(end, window_months)
    cli: Any = client if client is not None else _default_client_factory(PC_STAC_URL)
    search = cli.search(
        collections=[S2_COLLECTION],
        intersects=mapping(aoi),
        datetime=f"{start.isoformat()}/{end.isoformat()}",
        query={"eo:cloud_cover": {"lte": max_cloud_cover}},
    )
    items = sorted(search.items(), key=lambda it: (_item_datetime(it), it.id))
    logger.info(
        "[burn-scar] %s %s..%s cloud<=%d%%: %d candidate item(s)",
        S2_COLLECTION,
        start.isoformat(),
        end.isoformat(),
        max_cloud_cover,
        len(items),
    )
    for it in items:
        logger.info("[burn-scar]   %s  %s", _item_datetime(it).isoformat(), it.id)
    return items


def _pc_sas_token(collection: str) -> str:
    """Fetch (and cache until near-expiry) a Planetary Computer SAS token.

    Kept in-repo instead of adding the `planetary-computer` package: one GET
    against the public token endpoint (non-negotiable #8 — prefer the
    existing stack).
    """
    cached = _SAS_CACHE.get(collection)
    now = datetime.now(UTC)
    if cached and cached[1] > now + timedelta(minutes=5):
        return cached[0]
    resp = requests.get(PC_SAS_TOKEN_URL.format(collection=collection), timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    token = str(payload["token"])
    expiry = datetime.fromisoformat(str(payload["msft:expiry"]).replace("Z", "+00:00"))
    _SAS_CACHE[collection] = (token, expiry)
    return token


def _signed_item(item: pystac.Item, assets: tuple[str, ...]) -> pystac.Item:
    """Clone `item` with SAS-signed hrefs on the referenced assets."""
    token = _pc_sas_token(S2_COLLECTION)
    clone = item.clone()
    for key in assets:
        asset = clone.assets.get(key)
        if asset is None:
            raise ValueError(f"item {item.id} is missing expected asset {key!r}")
        sep = "&" if "?" in asset.href else "?"
        asset.href = f"{asset.href}{sep}{token}"
    return clone


def _item_epsg(item: pystac.Item) -> int:
    """EPSG code from the proj extension (`proj:epsg` or newer `proj:code`)."""
    props = item.properties or {}
    raw = props.get("proj:epsg")
    if raw:
        return int(raw)
    code = props.get("proj:code")
    if code:
        return int(str(code).rsplit(":", 1)[-1])
    raise ValueError(f"item {item.id} carries no proj:epsg / proj:code")


def _boa_offset(item: pystac.Item) -> float:
    """+1000 DN offset for processing baseline ≥ 04.00, else 0 (logged if unknown)."""
    raw = (item.properties or {}).get("s2:processing_baseline")
    if raw is None:
        logger.warning("[burn-scar] %s has no s2:processing_baseline; assuming no offset", item.id)
        return 0.0
    try:
        return 1000.0 if float(raw) >= 4.0 else 0.0
    except ValueError:
        logger.warning("[burn-scar] %s has baseline %r; assuming no offset", item.id, raw)
        return 0.0


def _pad_to_min(arr: np.ndarray, min_h: int, min_w: int) -> tuple[np.ndarray, int, int]:
    """Edge-pad the trailing two dims up to (min_h, min_w); returns original size."""
    import numpy as np

    h, w = arr.shape[-2:]
    pad_h = max(0, min_h - h)
    pad_w = max(0, min_w - w)
    if pad_h or pad_w:
        pad = [(0, 0)] * (arr.ndim - 2) + [(0, pad_h), (0, pad_w)]
        arr = np.pad(arr, pad, mode="edge")
    return arr, h, w


def _scene_probability(
    item: pystac.Item,
    handle: ModelHandle,
    *,
    s2_assets: tuple[str, ...],
    bounds: tuple[float, float, float, float],
    epsg: int,
    scl_mask_classes: tuple[int, ...],
    tile_size: int,
    tile_stride: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Class-1 softmax for one scene on the common grid; NaN where masked.

    Returns `(prob, x_coords, y_coords)` or None when no valid pixel survives
    the SCL/no-data mask.
    """
    import numpy as np
    import stackstac
    import torch
    from rasterio.enums import Resampling
    from terratorch.tasks.tiled_inference import tiled_inference

    signed = _signed_item(item, (*s2_assets, SCL_ASSET))
    stack = stackstac.stack(
        [signed],
        assets=[*s2_assets, SCL_ASSET],
        bounds=bounds,
        epsg=epsg,
        resolution=10,
        dtype=np.dtype("float32"),
        # stackstac's stub says int|float, but numpy 2 can_cast rejects python
        # scalars for a float32 output — it must be an np.float32 instance.
        fill_value=np.float32(0),  # pyright: ignore[reportArgumentType]
        rescale=False,
        resampling=Resampling.nearest,
    )
    arr = stack.isel(time=0).compute()
    data = arr.values  # (band, y, x)
    bands, scl = data[: len(s2_assets)], data[len(s2_assets)]

    valid = (bands > 0).any(axis=0) & ~np.isin(scl, list(scl_mask_classes))
    if not bool(valid.any()):
        logger.info("[burn-scar]   %s: fully masked on AOI grid, skipped", item.id)
        return None

    offset = _boa_offset(item)
    refl = np.clip((bands - offset) / 10000.0, 0.0, None)
    refl = np.where(valid[None, :, :], refl, 0.0)  # no_data_replace=0, per model config
    means = np.asarray(handle.means, dtype=np.float32)[:, None, None]
    stds = np.asarray(handle.stds, dtype=np.float32)[:, None, None]
    normed = ((refl - means) / stds).astype(np.float32)

    normed, orig_h, orig_w = _pad_to_min(normed, tile_size, tile_size)
    model = handle.model

    def forward(x: torch.Tensor) -> torch.Tensor:
        out = model(x)
        logits = out.output if hasattr(out, "output") else out
        return torch.nn.functional.softmax(logits, dim=1)

    # from_numpy is zero-copy (~200 MB/scene on the pilot grid); torch's stubs
    # mis-mark it as private, hence the targeted ignore.
    batch = torch.from_numpy(normed[None])  # pyright: ignore[reportPrivateImportUsage]
    with torch.no_grad():
        probs = tiled_inference(
            forward,
            batch,
            h_crop=tile_size,
            w_crop=tile_size,
            h_stride=tile_stride,
            w_stride=tile_stride,
            device=handle.device,
            verbose=False,
        )
    prob = probs[0, 1].cpu().numpy()[:orig_h, :orig_w].astype(np.float32)
    prob[~valid] = np.nan
    return prob, arr.x.values, arr.y.values


def infer_burn_probability(
    items: list[pystac.Item],
    model_handle: ModelHandle,
    aoi: BaseGeometry,
    *,
    s2_assets: tuple[str, ...],
    scl_mask_classes: tuple[int, ...],
    tile_size: int = 512,
    tile_stride: int = 448,
) -> xr.DataArray:
    """Max-composite burn-scar inference probability over `items`, clipped to `aoi`.

    Scenes are processed one at a time in the given (deterministic) order on
    a shared 10 m grid in the items' majority UTM zone; per-scene SAS signing
    keeps tokens fresh on long runs. The composite is reprojected to
    EPSG:4326 with nearest resampling (explicit CRS, non-negotiable #2) and
    clipped to the AOI polygon; masked pixels are NaN.
    """
    import numpy as np
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    import xarray as xr
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds

    if not items:
        raise ValueError("no S2 items to infer over — query_recent_s2 returned an empty list")

    epsg_counts = Counter(_item_epsg(it) for it in items)
    epsg = epsg_counts.most_common(1)[0][0]
    if len(epsg_counts) > 1:
        logger.warning(
            "[burn-scar] items span %d UTM zones %s; gridding on majority EPSG:%d",
            len(epsg_counts),
            dict(epsg_counts),
            epsg,
        )
    bounds = transform_bounds("EPSG:4326", f"EPSG:{epsg}", *aoi.bounds)

    composite: np.ndarray | None = None
    xs: np.ndarray | None = None
    ys: np.ndarray | None = None
    for i, item in enumerate(items, start=1):
        logger.info("[burn-scar] scene %d/%d %s", i, len(items), item.id)
        result = _scene_probability(
            item,
            model_handle,
            s2_assets=s2_assets,
            bounds=bounds,
            epsg=epsg,
            scl_mask_classes=scl_mask_classes,
            tile_size=tile_size,
            tile_stride=tile_stride,
        )
        if result is None:
            continue
        prob, xs, ys = result
        composite = prob if composite is None else np.fmax(composite, prob)

    if composite is None or xs is None or ys is None:
        raise ValueError(
            f"all {len(items)} scene(s) were fully masked over the AOI — "
            "nothing to composite (clouds/no-data); widen the window"
        )

    da = xr.DataArray(composite, dims=("y", "x"), coords={"y": ys, "x": xs})
    da = da.rio.write_crs(f"EPSG:{epsg}")
    da = da.rio.write_nodata(np.nan)
    da4326 = da.rio.reproject(OUTPUT_CRS, resampling=Resampling.nearest, nodata=np.nan)
    clipped = da4326.rio.clip([mapping(aoi)], crs=OUTPUT_CRS, drop=True, all_touched=True)
    return clipped


def write_stac_item(
    run: BurnScarRun,
    cog_path: Path,
    *,
    stac_root: Path = Path("stac"),
) -> Path:
    """Register the produced COG as a STAC item under `stac/burn-scar-recent/`.

    Creates the root catalog and the `burn-scar-recent` collection (per the
    definition in `inventory.yaml`) on first use; subsequent runs append
    items and re-derive the collection extent. The catalog is saved
    self-contained with relative hrefs so `stac-validator --recursive`
    passes on the committed tree.
    """
    from datetime import time as dtime

    import pystac
    import rasterio
    from shapely.geometry import box

    with rasterio.open(cog_path) as src:
        b = src.bounds
        bbox = [float(b.left), float(b.bottom), float(b.right), float(b.top)]

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

    window_start_dt = datetime.combine(run.window_start, dtime.min, tzinfo=UTC)
    window_end_dt = datetime.combine(run.window_end, dtime.max, tzinfo=UTC)

    child = catalog.get_child("burn-scar-recent")
    if child is not None:
        if not isinstance(child, pystac.Collection):
            raise TypeError(f"stac child 'burn-scar-recent' is a {type(child).__name__}")
        collection = child
    else:
        collection = pystac.Collection(
            id="burn-scar-recent",
            title="Recent burn-scar inference probability (Prithvi-Burn-Scar)",
            description=(
                "Per-run burn-scar inference probability raster over the AOI for a "
                "trailing Sentinel-2 L2A window, derived by running the pretrained "
                "Prithvi-EO 2.0 burn-scar downstream task (inference only). Detects "
                "burn scars — post-event spectral signatures of fires that already "
                "happened. Values are relative model scores, not calibrated "
                "probabilities and not fire forecasts. Fills the gap between the "
                "latest ICNF Áreas Ardidas vintage and the current date. See "
                "inventory.yaml: burn-scar-recent."
            ),
            extent=pystac.Extent(
                spatial=pystac.SpatialExtent([bbox]),
                temporal=pystac.TemporalExtent([[window_start_dt, window_end_dt]]),
            ),
            license="MIT",
        )
        catalog.add_child(collection)

    item_id = f"burn-scar-{run.run_id}"
    if collection.get_item(item_id) is not None:
        raise ValueError(f"STAC item {item_id} already exists under {stac_root}")

    item = pystac.Item(
        id=item_id,
        geometry=mapping(box(bbox[0], bbox[1], bbox[2], bbox[3])),
        bbox=bbox,
        datetime=run.created_at_utc,
        properties={
            "wildfire_exposure_eo:provenance": run.model_dump(mode="json"),
        },
    )
    item.common_metadata.start_datetime = window_start_dt
    item.common_metadata.end_datetime = window_end_dt
    item.add_asset(
        "burn_scar_probability",
        pystac.Asset(
            href=str(cog_path.resolve()),
            title="Burn-scar inference probability (Prithvi-Burn-Scar class-1 softmax)",
            description=(
                "Max-composite over the trailing S2 L2A window; relative model "
                "score in [0, 1], nodata -9999. Not a calibrated probability, "
                "not a fire forecast."
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
    logger.info("[burn-scar] STAC item %s -> %s", item_id, item_path)
    return item_path


def write_burn_scar_cog(da: xr.DataArray, path: Path, provenance: BurnScarRun) -> Path:
    """Write the probability raster as a deflate COG with embedded provenance.

    The full `BurnScarRun` record goes into the GeoTIFF tags (JSON under
    `WILDFIRE_EXPOSURE_EO_PROVENANCE`) and into a `.json` sidecar next to the
    COG. NaN becomes the documented nodata value.
    """
    import numpy as np
    import rioxarray  # noqa: F401  (registers the .rio accessor)

    path.parent.mkdir(parents=True, exist_ok=True)
    out = da.fillna(NODATA).astype("float32")
    out = out.rio.write_nodata(NODATA)
    tags = {
        "WILDFIRE_EXPOSURE_EO_PROVENANCE": provenance.model_dump_json(),
        "RUN_ID": provenance.run_id,
        "MODEL_ID": provenance.model_id,
        "HF_REVISION_SHA": provenance.hf_revision_sha,
        "VALUE_DESCRIPTION": (
            "burn-scar inference probability (Prithvi-Burn-Scar class-1 softmax, "
            "max-composited over the trailing S2 L2A window); relative model "
            "score, not a calibrated probability and not a fire forecast"
        ),
    }
    out.rio.to_raster(path, driver="COG", compress="deflate", tags=tags)

    sidecar = path.with_suffix(".json")
    sidecar.write_text(
        json.dumps(provenance.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    np_max = float(np.nanmax(np.where(out.values == NODATA, np.nan, out.values)))
    logger.info("[burn-scar] wrote %s (max prob %.3f) + sidecar %s", path, np_max, sidecar.name)
    return path
