"""FireScope risk-raster reader for the WU-21 head-to-head benchmark.

FireScope (INSAIT Institute + ETH Zürich, arXiv:2511.17171) publishes a
Europe-wide ~30 m wildfire **risk** raster, ``oracle_unet.tif``, on the Hugging
Face dataset ``INSAIT-Institute/firescope-risk-2026`` under CC-BY-4.0. The file
is ~12.3 GB LFS-backed and byte-range readable, so we never download it: GDAL
``/vsicurl/`` window-reads the few pixels under our scored-asset locations.

Vocabulary firewall (non-negotiable #6). FireScope's pixel values are *their*
"risk" in *their* terminology — quoted and attributed. The uint8 0..254 band is
**undocumented in units**, so we treat it as a **relative risk rank**, never a
probability, and never convert our own *exposure rank* into one. ``255`` is the
declared nodata sentinel.

This module's numeric core (:func:`sample_raster_at_points`) is a pure function
over any open rasterio dataset and is exercised by a hermetic test against a
synthetic raster with a known answer. The live ``/vsicurl/`` read
(:func:`sample_firescope_at_points`) is deliberately kept out of the default
pytest gate — it needs network access to Hugging Face.

CRS is explicit everywhere (non-negotiable #2): our assets are EPSG:4326,
FireScope is EPSG:3857; we reproject the sample points to the raster CRS with a
documented :class:`pyproj.Transformer` before reading — no implicit reprojection.
No raw FireScope raster is committed to this repo; only derived comparison
artefacts (CC-BY-4.0 attribution travels with them).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from rasterio.io import DatasetReader

# ---------------------------------------------------------------------------
# Real, verified FireScope identifiers (non-negotiable #1 — no invented IDs).
# Source of truth: scripts/21_firescope_feasibility.py →
# outputs/diagnostics/21_firescope_feasibility.json (regenerated this WU).
# ---------------------------------------------------------------------------
HF_DATASET_ID = "INSAIT-Institute/firescope-risk-2026"
HF_DATASET_REVISION = "c387af41553015c6799ad0bcf116b14e464a6264"
RASTER_FILENAME = "oracle_unet.tif"
RASTER_LFS_OID_SHA256 = "b41bfbefef0813ee037086d46cd120f108c8431bad8ae3f03434ccaff6df2b0c"
RASTER_SIZE_BYTES = 12292291352
RASTER_URL = f"https://huggingface.co/datasets/{HF_DATASET_ID}/resolve/main/{RASTER_FILENAME}"
VSICURL_URL = f"/vsicurl/{RASTER_URL}"
LICENSE = "CC-BY-4.0"
ARXIV_ID = "2511.17171"
ATTRIBUTION = (
    "FireScope (INSAIT Institute + ETH Zürich), Europe-wide wildfire-risk raster "
    f"oracle_unet.tif, Hugging Face dataset {HF_DATASET_ID} "
    f"(revision {HF_DATASET_REVISION[:12]}), CC-BY-4.0, arXiv:{ARXIV_ID}."
)

#: FireScope raster CRS (verified from the file via /vsicurl/).
FIRESCOPE_CRS = "EPSG:3857"
#: Declared nodata sentinel for the uint8 band.
FIRESCOPE_NODATA = 255

# GDAL env knobs for an efficient, range-only /vsicurl/ open (no directory listing,
# only .tif extensions allowed). Passed to ``rasterio.Env``.
GDAL_VSICURL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_TIMEOUT": "30",
    "VSI_CACHE": "TRUE",
}


@dataclass(frozen=True)
class PointSample:
    """Result of sampling a raster at a set of points.

    ``values`` is a float array aligned with the input points; cells that fell on
    nodata or outside the raster footprint are ``np.nan``. ``n_valid`` /
    ``n_nodata`` / ``n_outside`` partition the points for an honest coverage line.
    """

    values: NDArray[np.float64]
    n_valid: int
    n_nodata: int
    n_outside: int


def sample_raster_at_points(
    dataset: DatasetReader,
    lons: NDArray[np.float64],
    lats: NDArray[np.float64],
    *,
    points_crs: str = "EPSG:4326",
) -> PointSample:
    """Nearest-cell sample of an open raster at lon/lat points, CRS-explicit.

    The points are given in ``points_crs`` (default EPSG:4326). They are
    reprojected to ``dataset.crs`` with an explicit :class:`pyproj.Transformer`
    (``always_xy=True``) before indexing — no implicit reprojection
    (non-negotiable #2). Sampling is **nearest cell**, which is the correct
    reducer for a categorical/relative-rank surface: we read the value of the
    pixel each point lands in, not an interpolation across pixels.

    A single window covering the bounding box of all in-footprint points is read
    once (range request), so this is cheap even against the 12 GB FireScope raster:
    only the pixels needed are fetched. Points outside the raster footprint, or on
    the nodata sentinel, become ``np.nan`` in the returned ``values``.

    Determinism: pure function of (dataset bytes, points); no RNG, no clock.
    """
    from pyproj import Transformer
    from rasterio.windows import Window

    lons = np.asarray(lons, dtype="float64")
    lats = np.asarray(lats, dtype="float64")
    if lons.shape != lats.shape:
        raise ValueError(f"lons {lons.shape} and lats {lats.shape} length mismatch")
    if lons.ndim != 1:
        raise ValueError(f"lons/lats must be 1-D, got {lons.ndim}-D")
    n = lons.size
    if dataset.crs is None:
        raise ValueError("raster has no CRS — refusing to assume one")

    transformer = Transformer.from_crs(points_crs, dataset.crs, always_xy=True)
    xs, ys = transformer.transform(lons, lats)
    xs = np.asarray(xs, dtype="float64")
    ys = np.asarray(ys, dtype="float64")

    left, bottom, right, top = dataset.bounds
    in_footprint = (xs >= left) & (xs <= right) & (ys >= bottom) & (ys <= top)

    values = np.full(n, np.nan, dtype="float64")
    if not in_footprint.any():
        return PointSample(values=values, n_valid=0, n_nodata=0, n_outside=n)

    fx = xs[in_footprint]
    fy = ys[in_footprint]

    # Global integer (row, col) of each in-footprint point via the raster's inverse
    # transform — the cell each point lands in. Clamp to the grid (a point exactly
    # on the right/top edge maps to width/height, which is one past the last cell).
    inv = ~dataset.transform
    g_cols_f, g_rows_f = inv * (fx, fy)
    g_rows = np.clip(
        np.floor(np.asarray(g_rows_f, dtype="float64")).astype("int64"),
        0,
        dataset.height - 1,
    )
    g_cols = np.clip(
        np.floor(np.asarray(g_cols_f, dtype="float64")).astype("int64"),
        0,
        dataset.width - 1,
    )

    # One window spanning all needed cells (>= 1x1 by construction; single read).
    r0, r1 = int(g_rows.min()), int(g_rows.max())
    c0, c1 = int(g_cols.min()), int(g_cols.max())
    win = Window(col_off=c0, row_off=r0, width=(c1 - c0) + 1, height=(r1 - r0) + 1)  # type: ignore[no-untyped-call]
    block = dataset.read(1, window=win)

    nodata = dataset.nodata
    sub = block[g_rows - r0, g_cols - c0].astype("float64")
    if nodata is not None:
        sub = np.where(sub == float(nodata), np.nan, sub)

    values[np.flatnonzero(in_footprint)] = sub

    n_valid = int(np.isfinite(values).sum())
    n_outside = n - int(in_footprint.sum())
    n_nodata = int(in_footprint.sum()) - int(np.isfinite(sub).sum())
    return PointSample(values=values, n_valid=n_valid, n_nodata=n_nodata, n_outside=n_outside)


def sample_firescope_at_points(
    lons: NDArray[np.float64],
    lats: NDArray[np.float64],
    *,
    points_crs: str = "EPSG:4326",
    url: str = VSICURL_URL,
) -> PointSample:
    """Sample the live FireScope raster at lon/lat points via GDAL ``/vsicurl/``.

    Opens the published ``oracle_unet.tif`` with byte-range reads (no full
    download) and delegates to :func:`sample_raster_at_points`. Network-dependent;
    not part of the default pytest gate. ``points_crs`` defaults to our asset CRS
    (EPSG:4326); the raster is EPSG:3857 and the reprojection is explicit inside
    :func:`sample_raster_at_points`.
    """
    import rasterio
    from rasterio.env import Env

    # **GDAL_VSICURL_ENV maps to Env's **options (GDAL config); pyright's stub
    # mis-binds the first kwarg to `aws_unsigned`, hence the targeted ignore.
    with Env(**GDAL_VSICURL_ENV), rasterio.open(url) as ds:  # type: ignore[arg-type]
        if str(ds.crs) != FIRESCOPE_CRS:
            raise ValueError(
                f"FireScope CRS is {ds.crs!r} — expected {FIRESCOPE_CRS!r} "
                "(provenance drift; refusing to sample under a wrong assumption)"
            )
        return sample_raster_at_points(ds, lons, lats, points_crs=points_crs)


def provenance() -> dict[str, str | int]:
    """Provenance dict for any FireScope-derived artefact (#1, #3, CC-BY-4.0)."""
    return {
        "firescope_dataset_id": HF_DATASET_ID,
        "firescope_dataset_revision": HF_DATASET_REVISION,
        "firescope_raster_filename": RASTER_FILENAME,
        "firescope_raster_lfs_oid_sha256": RASTER_LFS_OID_SHA256,
        "firescope_raster_size_bytes": RASTER_SIZE_BYTES,
        "firescope_raster_url": RASTER_URL,
        "firescope_crs": FIRESCOPE_CRS,
        "firescope_nodata": FIRESCOPE_NODATA,
        "firescope_license": LICENSE,
        "firescope_arxiv": ARXIV_ID,
        "firescope_attribution": ATTRIBUTION,
    }
