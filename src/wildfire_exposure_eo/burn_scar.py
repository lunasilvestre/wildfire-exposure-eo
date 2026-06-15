"""Prithvi burn-scar inference over the AOI (Stage 1b, prompt 09).

Runs the pretrained `Prithvi-EO-2.0-300M-BurnScars` checkpoint (inference
only, frozen weights) over the trailing window of Sentinel-2 L2A scenes and
composites a single per-pixel raster of the model's class-1 softmax score
under the configured reducer (`reduce_stack`: max | median | p75 | p85 | p90 |
consensus_N — WU-10), written as a COG with full provenance. `max` is
bit-identical to the original np.fmax composite.

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
import hashlib
import json
import logging
import time
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

#: Default deterministic seed (CLAUDE.md non-negotiable #4). Mixed with each
#: scene's STAC item id to choose that scene's crop-grid origin offset, so the
#: choice is reproducible across runs and stable per scene regardless of input
#: order.
DEFAULT_SEED = 42
PC_SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"
S2_COLLECTION = "sentinel-2-l2a"

#: SAS tokens per collection, with their expiry. Refreshed per scene read so
#: long pilot runs never hold a stale token.
_SAS_CACHE: dict[str, tuple[str, datetime]] = {}

#: Per-scene retry policy for transient blob/network failures. The delays
#: must outlast PC's server-side SAS rotation so a retry gets a fresh token.
_SCENE_ATTEMPTS = 3
_SCENE_RETRY_DELAYS_S = (30, 180)

#: GDAL/curl HTTP discipline for blob reads. Without a timeout, a connection
#: the server has half-closed (CLOSE-WAIT) blocks a read forever and the
#: scene-level retry never fires — observed on the 2026-06-10 pilot run.
#: Applied with setdefault so externally-set values win.
_GDAL_HTTP_DEFAULTS = {
    "GDAL_HTTP_TIMEOUT": "120",
    "GDAL_HTTP_CONNECTTIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "5",
}


def _apply_gdal_http_defaults() -> None:
    """Make hung blob reads fail fast instead of blocking forever."""
    import os

    for key, value in _GDAL_HTTP_DEFAULTS.items():
        os.environ.setdefault(key, value)


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


#: Reducer names that map to a NaN-aware percentile of the per-scene scores.
#: `max` is handled separately so it is *bit-identical* to the legacy np.fmax
#: accumulator (and never emits an all-NaN-slice warning).
_PERCENTILE_REDUCERS = {"median": 50.0, "p75": 75.0, "p85": 85.0, "p90": 90.0}


def _parse_consensus_fraction(reducer: str) -> float:
    """`consensus_N` -> required fraction N/10 of scenes scoring >=0.5.

    Raises `ValueError` on a malformed suffix (non-integer or outside 1..10).
    """
    suffix = reducer[len("consensus_") :]
    try:
        n = int(suffix)
    except ValueError as exc:
        raise ValueError(
            f"unrecognised reducer {reducer!r}: consensus_N needs an integer N (got {suffix!r})"
        ) from exc
    if not 1 <= n <= 10:
        raise ValueError(f"unrecognised reducer {reducer!r}: consensus_N needs 1<=N<=10")
    return n / 10.0


def reduce_stack(stack: np.ndarray, reducer: str) -> np.ndarray:
    """Reduce a `(n_scene, y, x)` per-scene score stack to one `(y, x)` composite.

    Masked pixels are NaN per scene. Supported reducers:

    * ``max`` — per-pixel maximum, NaN only where every scene is NaN. This is
      *bit-identical* to the legacy ``np.fmax``-accumulator path (asserted in
      `tests/unit/test_burn_scar.py::test_reducer_max_backward_compat`), so the
      key being absent in an old config reproduces the original composite.
    * ``median`` / ``p75`` / ``p85`` / ``p90`` — NaN-ignoring percentile across
      the scenes that observed each pixel; a single-scene spike no longer
      survives the whole window. NaN where every scene is NaN.
    * ``consensus_N`` (N in 1..10) — a pixel is 1.0 only when the fraction of
      *observing* scenes scoring >=0.5 exceeds N/10 (e.g. consensus_5 = majority
      vote), else 0.0; NaN where every scene is NaN.

    Raises `ValueError` on any other string.
    """
    import warnings

    import numpy as np

    if stack.ndim != 3:
        raise ValueError(f"reduce_stack expects a (n_scene, y, x) stack, got shape {stack.shape}")

    if reducer == "max":
        # np.fmax.reduce == the old `composite = np.fmax(composite, prob)` loop,
        # all-NaN -> NaN, and emits no all-NaN-slice warning.
        return np.fmax.reduce(stack, axis=0)

    all_nan = np.all(np.isnan(stack), axis=0)

    if reducer in _PERCENTILE_REDUCERS:
        q = _PERCENTILE_REDUCERS[reducer]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)  # all-NaN slices
            out = np.nanpercentile(stack, q, axis=0).astype(np.float32)
        out[all_nan] = np.nan
        return out

    if reducer.startswith("consensus_"):
        frac_required = _parse_consensus_fraction(reducer)
        observed = np.sum(~np.isnan(stack), axis=0)
        hits = np.nansum(stack >= 0.5, axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            share = np.where(observed > 0, hits / observed, np.nan)
        out = (share > frac_required).astype(np.float32)
        out[all_nan] = np.nan
        return out

    raise ValueError(
        f"unrecognised reducer {reducer!r}; expected one of "
        "max | median | p75 | p85 | p90 | consensus_N"
    )


def filter_to_season(
    items: list[pystac.Item],
    *,
    season_start_month: int,
    season_end_month: int,
) -> list[pystac.Item]:
    """Keep only items whose acquisition month is in `[start, end]` (1-indexed).

    Logs how many items are dropped (CLAUDE.md verify-then-act). When the
    window spans the year boundary (start > end, e.g. Nov..Feb) the range wraps.
    A 1..12 window is a no-op. Order is preserved.
    """
    if not 1 <= season_start_month <= 12 or not 1 <= season_end_month <= 12:
        raise ValueError(
            f"season months must be 1..12, got {season_start_month}..{season_end_month}"
        )
    if season_start_month == 1 and season_end_month == 12:
        return items

    def in_season(month: int) -> bool:
        if season_start_month <= season_end_month:
            return season_start_month <= month <= season_end_month
        return month >= season_start_month or month <= season_end_month  # wraps year-end

    kept = [it for it in items if in_season(_item_datetime(it).month)]
    dropped = len(items) - len(kept)
    logger.info(
        "[burn-scar] fire-season filter [%d..%d]: kept %d, dropped %d off-season item(s)",
        season_start_month,
        season_end_month,
        len(kept),
        dropped,
    )
    return kept


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

    PC serves a server-side-cached token, so near rotation the endpoint can
    return one with only minutes of validity left — enough to die mid-scene
    (observed on the 2026-06-09 pilot run). Near-expiry tokens are therefore
    used but never cached, and the per-scene retry in
    `infer_burn_probability` re-signs after the rotation window.
    """
    margin = timedelta(minutes=5)
    cached = _SAS_CACHE.get(collection)
    now = datetime.now(UTC)
    if cached and cached[1] > now + margin:
        return cached[0]
    resp = requests.get(PC_SAS_TOKEN_URL.format(collection=collection), timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    token = str(payload["token"])
    expiry = datetime.fromisoformat(str(payload["msft:expiry"]).replace("Z", "+00:00"))
    if expiry > datetime.now(UTC) + margin:
        _SAS_CACHE[collection] = (token, expiry)
    else:
        _SAS_CACHE.pop(collection, None)
        logger.warning(
            "[burn-scar] PC returned a near-expiry SAS token for %s (expires %s); not caching",
            collection,
            expiry.isoformat(),
        )
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


def scene_origin_offset(
    item_id: str, tile_stride: int, *, seed: int = DEFAULT_SEED
) -> tuple[int, int]:
    """Deterministic per-scene crop-grid origin offset ``(dy, dx)`` in ``[0, stride)``.

    De-grid fix (WU-10). terratorch ``tiled_inference`` slides a fixed
    axis-aligned crop lattice (origin 0,0; stride ``tile_stride``) over every
    scene. Each Prithvi/ViT crop carries a tent-shaped class-1 response
    (~5x core/border), and because the lattice is phase-locked to the same UTM
    grid across all ~179 scenes, the per-pixel composite stacks every tent at
    the same pixels into a saturated grid of squares.

    Shifting the crop-grid ORIGIN by a per-scene ``(dy, dx)`` makes the tent
    land at different pixels each scene, so a percentile composite (p85)
    averages it out instead of reinforcing it. The offset is derived from a
    stable per-scene key (the STAC ``item_id``) mixed with ``seed`` via blake2b
    — NOT Python ``hash`` (salted, non-reproducible) — so the choice is
    deterministic (non-negotiable #4) and independent of input order.

    Returns ``(0, 0)`` when ``tile_stride <= 1`` (no room to jitter).
    """
    if tile_stride <= 1:
        return 0, 0
    digest = hashlib.blake2b(f"{seed}:{item_id}".encode(), digest_size=8).digest()
    dy = int.from_bytes(digest[:4], "big") % tile_stride
    dx = int.from_bytes(digest[4:], "big") % tile_stride
    return dy, dx


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
    tile_origin_jitter: bool = False,
    seed: int = DEFAULT_SEED,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Class-1 softmax for one scene on the common grid; NaN where masked.

    Returns `(prob, x_coords, y_coords)` or None when no valid pixel survives
    the SCL/no-data mask.

    When `tile_origin_jitter` is set the crop-grid origin is shifted by a
    deterministic per-scene `(dy, dx)` (see `scene_origin_offset`) before
    `tiled_inference` and the shift is inverted on the returned probability, so
    the per-crop ViT centre-bias tent lands at different pixels each scene and a
    percentile composite averages it out instead of phase-locking it into a
    saturated grid (WU-10 de-grid fix).
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

    means = np.asarray(handle.means, dtype=np.float32)[:, None, None]
    stds = np.asarray(handle.stds, dtype=np.float32)[:, None, None]
    offset = _boa_offset(item)
    refl = np.clip((bands - offset) / 10000.0, 0.0, None)
    # Masked pixels are replaced with the per-band reflectance MEAN so they
    # normalise to ~0.0 (neutral) rather than -mean/std (a strongly negative,
    # out-of-distribution value the US-HLS fine-tune never saw). This closes a
    # latent fragility flagged in WU-10 — not the root cause of the grid, but a
    # cleaner no-data treatment than the old `0.0` fill.
    refl = np.where(valid[None, :, :], refl, means)
    normed = ((refl - means) / stds).astype(np.float32)

    normed, orig_h, orig_w = _pad_to_min(normed, tile_size, tile_size)

    dy, dx = (0, 0)
    if tile_origin_jitter:
        dy, dx = scene_origin_offset(item.id, tile_stride, seed=seed)
        if dy or dx:
            # Reflect-pad the top/left by (dy, dx) so the crop lattice that
            # tiled_inference anchors at (0, 0) now starts (dy, dx) earlier
            # relative to the real scene — i.e. the origin is jittered. Reflect
            # (not edge/zero) keeps the padded border in-distribution. The shift
            # is inverted by slicing [dy:dy+H, dx:dx+W] off the result below.
            normed = np.pad(normed, [(0, 0), (dy, 0), (dx, 0)], mode="reflect")
            # tiled_inference still needs the array to cover at least one crop.
            normed, _, _ = _pad_to_min(normed, tile_size, tile_size)
        logger.info("[burn-scar]   %s: crop-origin jitter (dy=%d, dx=%d)", item.id, dy, dx)

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
    # Invert the origin jitter: drop the (dy, dx) reflect-pad, then crop to the
    # original (unpadded) scene extent. np.ascontiguousarray makes an owned copy
    # so the in-place NaN mask below does not alias the padded buffer.
    prob_full = probs[0, 1].cpu().numpy().astype(np.float32)
    prob = np.ascontiguousarray(prob_full[dy : dy + orig_h, dx : dx + orig_w])
    prob[~valid] = np.nan
    return prob, arr.x.values, arr.y.values


#: Row-block height (pixels) for the block-wise stack reduction. Reading a
#: `(n_scene, _REDUCE_BLOCK_ROWS, W)` slab across all scenes at once bounds peak
#: RAM regardless of scene count: at W~3500 / 179 scenes a 256-row block is
#: ~0.6 GB, vs ~9 GB to hold every full-res scene array simultaneously.
_REDUCE_BLOCK_ROWS = 256


def infer_burn_probability(
    items: list[pystac.Item],
    model_handle: ModelHandle,
    aoi: BaseGeometry,
    *,
    s2_assets: tuple[str, ...],
    scl_mask_classes: tuple[int, ...],
    reducer: str = "max",
    tile_size: int = 512,
    tile_stride: int = 448,
    tile_origin_jitter: bool = False,
    seed: int = DEFAULT_SEED,
) -> xr.DataArray:
    """Composite burn-scar inference scores over `items`, clipped to `aoi`.

    `reducer="max"` is bit-identical to the legacy np.fmax composite. Thin
    single-reducer wrapper over `infer_burn_probability_multi` — see there for
    the streaming / memory-bounded details and the `tile_origin_jitter` de-grid.
    """
    return infer_burn_probability_multi(
        items,
        model_handle,
        aoi,
        s2_assets=s2_assets,
        scl_mask_classes=scl_mask_classes,
        reducers=(reducer,),
        tile_size=tile_size,
        tile_stride=tile_stride,
        tile_origin_jitter=tile_origin_jitter,
        seed=seed,
    )[reducer]


def infer_burn_probability_multi(
    items: list[pystac.Item],
    model_handle: ModelHandle,
    aoi: BaseGeometry,
    *,
    s2_assets: tuple[str, ...],
    scl_mask_classes: tuple[int, ...],
    reducers: tuple[str, ...] = ("max",),
    tile_size: int = 512,
    tile_stride: int = 448,
    tile_origin_jitter: bool = False,
    seed: int = DEFAULT_SEED,
) -> dict[str, xr.DataArray]:
    """Composite burn-scar scores under one or more reducers in a single pass.

    Scenes are processed one at a time in the given (deterministic) order on a
    shared 10 m grid in the items' majority UTM zone; per-scene SAS signing
    keeps tokens fresh on long runs. Each scene's per-pixel score is streamed
    to an on-disk memmap stack so peak RAM stays bounded (`_REDUCE_BLOCK_ROWS`
    rows × every scene at a time), never the whole ~179-scene stack at once.
    After the loop the stack is reduced block-wise by each `reduce_stack(name)`,
    reprojected to EPSG:4326 (nearest, explicit CRS — non-negotiable #2) and
    clipped to the AOI; masked pixels are NaN. Returns ``{reducer: DataArray}``.

    When `tile_origin_jitter` is set, each scene's crop-grid origin is shifted by
    a deterministic per-scene offset (seeded by `seed` + item id) so the per-crop
    ViT centre-bias tent does not phase-lock into a saturated grid across the
    stack (WU-10 de-grid). The offset is inverted per scene, so the composites
    stay on the common grid.

    Emitting several candidate composites from the SAME scene stack is how the
    WU-10 validation harness picks a reducer data-drivenly without paying for
    inference once per candidate.
    """
    import shutil
    import tempfile

    import numpy as np
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    import xarray as xr
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds

    if not items:
        raise ValueError("no S2 items to infer over — query_recent_s2 returned an empty list")
    if not reducers:
        raise ValueError("at least one reducer is required")
    # Validate every reducer string up front so a long pilot run never reaches
    # the reduce step only to fail on a typo.
    for name in reducers:
        _validate_reducer_name(name)
    _apply_gdal_http_defaults()

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

    # Stream each scene's score into an on-disk memmap stack so we never hold
    # every full-res scene array in RAM at once (CLAUDE.md verify-then-act /
    # the WU-10 memory budget on a single 24 GB GPU host).
    tmpdir = tempfile.mkdtemp(prefix="burn_scar_stack_")
    stack_path = Path(tmpdir) / "scene_stack.dat"
    stack: np.memmap | None = None
    xs: np.ndarray | None = None
    ys: np.ndarray | None = None
    n_used = 0
    try:
        for i, item in enumerate(items, start=1):
            logger.info("[burn-scar] scene %d/%d %s", i, len(items), item.id)
            result = None
            for attempt in range(1, _SCENE_ATTEMPTS + 1):
                try:
                    result = _scene_probability(
                        item,
                        model_handle,
                        s2_assets=s2_assets,
                        bounds=bounds,
                        epsg=epsg,
                        scl_mask_classes=scl_mask_classes,
                        tile_size=tile_size,
                        tile_stride=tile_stride,
                        tile_origin_jitter=tile_origin_jitter,
                        seed=seed,
                    )
                    break
                except Exception as exc:
                    # Transient blob/network failures, including a SAS token
                    # that expired mid-scene. Drop the cached token and retry
                    # with delays that outlast PC's server-side token rotation.
                    _SAS_CACHE.clear()
                    if attempt == _SCENE_ATTEMPTS:
                        raise RuntimeError(
                            f"scene {item.id} failed after {attempt} attempt(s)"
                        ) from exc
                    delay = _SCENE_RETRY_DELAYS_S[attempt - 1]
                    logger.warning(
                        "[burn-scar]   %s attempt %d/%d failed (%s); re-signing, retrying in %ds",
                        item.id,
                        attempt,
                        _SCENE_ATTEMPTS,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            if result is None:
                continue
            prob, xs, ys = result
            if stack is None:
                # First surviving scene fixes the grid; size the memmap for the
                # worst case (every item contributes) — unused trailing scenes
                # are simply never reduced over.
                stack = np.memmap(
                    stack_path,
                    dtype=np.float32,
                    mode="w+",
                    shape=(len(items), prob.shape[0], prob.shape[1]),
                )
            elif prob.shape != stack.shape[1:]:
                raise ValueError(
                    f"scene {item.id} grid {prob.shape} != reference {stack.shape[1:]}"
                )
            stack[n_used] = prob
            n_used += 1

        if stack is None or xs is None or ys is None:
            raise ValueError(
                f"all {len(items)} scene(s) were fully masked over the AOI — "
                "nothing to composite (clouds/no-data); widen the window"
            )
        stack.flush()
        used = stack[:n_used]
        logger.info(
            "[burn-scar] reducing %d scene(s) with %s in %d-row blocks",
            n_used,
            list(reducers),
            _REDUCE_BLOCK_ROWS,
        )
        composites = {name: _reduce_stack_blockwise(used, name) for name in reducers}
    finally:
        # Release the memmap before deleting its backing file.
        stack = None
        shutil.rmtree(tmpdir, ignore_errors=True)

    def _finalise(arr: np.ndarray) -> xr.DataArray:
        da = xr.DataArray(arr, dims=("y", "x"), coords={"y": ys, "x": xs})
        da = da.rio.write_crs(f"EPSG:{epsg}")
        da = da.rio.write_nodata(np.nan)
        da4326 = da.rio.reproject(OUTPUT_CRS, resampling=Resampling.nearest, nodata=np.nan)
        return da4326.rio.clip([mapping(aoi)], crs=OUTPUT_CRS, drop=True, all_touched=True)

    return {name: _finalise(arr) for name, arr in composites.items()}


def _validate_reducer_name(reducer: str) -> None:
    """Raise `ValueError` now if `reducer` is not a name `reduce_stack` accepts."""
    if reducer == "max" or reducer in _PERCENTILE_REDUCERS:
        return
    if reducer.startswith("consensus_"):
        _parse_consensus_fraction(reducer)
        return
    raise ValueError(
        f"unrecognised reducer {reducer!r}; expected one of "
        "max | median | p75 | p85 | p90 | consensus_N"
    )


def _reduce_stack_blockwise(stack: np.ndarray, reducer: str) -> np.ndarray:
    """`reduce_stack` applied in row blocks so peak RAM stays bounded.

    Identical output to `reduce_stack(stack, reducer)` — the blocking only
    controls how many scene rows are materialised at once.
    """
    import numpy as np

    _n_scene, height, width = stack.shape
    out = np.empty((height, width), dtype=np.float32)
    for y0 in range(0, height, _REDUCE_BLOCK_ROWS):
        y1 = min(y0 + _REDUCE_BLOCK_ROWS, height)
        # np.asarray pulls just this block off the memmap into RAM.
        block = np.asarray(stack[:, y0:y1, :], dtype=np.float32)
        out[y0:y1, :] = reduce_stack(block, reducer)
    return out


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
                f"{run.reducer}-composite over the trailing S2 L2A window; relative "
                "model score in [0, 1], nodata -9999. Not a calibrated probability, "
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
            f"{provenance.reducer}-composited over the trailing S2 L2A window); relative "
            "model score, not a calibrated probability and not a fire forecast"
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
