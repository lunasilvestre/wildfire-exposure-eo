"""Generate the static geodata + style bundle for the Pages geobrowser (WU-9, prompt 15).

Everything the `docs/` site renders is emitted here from the authoritative
pipeline artefacts — nothing hand-made:

* ``docs/app/data/exposure_assets_<run_id>.geojson`` — scored assets
  (EPSG:4326, full coordinate precision). Every source row is validated
  against the ``ScoredAsset`` schema before export; feature properties follow
  ``ExposureFeatureProperties``.
* ``docs/app/data/aoi.geojson`` — AOI boundary copy (EPSG:4326).
* ``outputs/geobrowser/fuel_class_3857_<run_id>.tif`` — display copy of the
  fuel COG warped to EPSG:3857 / GoogleMapsCompatible tiling, NEAREST
  resampling, ``ZOOM_LEVEL_STRATEGY=UPPER`` (no resolution loss). Required
  because maplibre-cog-protocol renders EPSG:3857 COGs only — the authoritative
  EPSG:32629 COG stays the STAC asset. Uploaded to Cloudflare R2 (byte-range
  capable): a same-origin committed COG renders live but stays BLANK under the
  local ``python -m http.server`` preview, which cannot serve the HTTP Range
  requests geotiff.js needs.
* ``outputs/geobrowser/burn_scar_3857_<run_id>.tif`` — same warp for the
  burn-scar COG (authoritative CRS EPSG:4326); uploaded to Cloudflare R2,
  too large to commit.
* ``outputs/geobrowser/icnf_burns_<run_id>.geojson`` — pilot ICNF perimeter
  display copy (EPSG:4326, full precision); uploaded to Cloudflare R2 (7.8 MB,
  over the repo's 2 000 kB committed-file cap).
* ``outputs/geobrowser/icnf_burns_<aoi>_<run_id>.geojson`` — per study-area
  ICNF perimeter display copy (EPSG:4326, 6 dp for compactness); uploaded to
  Cloudflare R2 and shown with its AOI. Reuses the
  ``outputs/parquet/icnf_burns_<aoi>_<run_id>.parquet`` fetched via
  ``wildfire-exposure-eo fetch-burns --aoi data/aoi/<aoi>.geojson``.
* ``outputs/geobrowser/{canopy_height,slope,nbr_delta,fuel_class}_<aoi>_3857_<run_id>.tif``
  — per-AOI model-INPUT display COGs warped to EPSG:3857 (NEAREST, no resolution
  loss) from ``outputs/cogs/{prefix}_<aoi>_<run_id>.tif``; uploaded to Cloudflare
  R2 and shown as toggleable per-AOI input layers synced to the AOI selector.
  The pilot gets canopy / slope / NBR-delta (its FUEL input is the dedicated
  ``fuel_class`` artefact); each study area gets all four. Relative model
  INPUTS, never a probability or forecast (non-negotiable #6).
* ``docs/app/data/style_data.json`` — colour LUTs sampled from the same
  matplotlib colormaps the WU-8 figures use (viridis rank / YlOrRd burn-scar /
  tab10 fuel classes), the fuel legend from ``config/fuel_crosswalk.yaml``,
  the validation headline read verbatim from the WU-7 metrics JSON, and the
  artefact manifest with an explicit CRS per file (non-negotiable #2).

Run selection is anchored on the latest validation metrics JSON so the site
always shows the run the committed validation report describes (the backdated
pilot run), not merely the newest scoring run.

Usage::

    uv run python scripts/15_make_geobrowser_data.py --smoke   # gate check
    uv run python scripts/15_make_geobrowser_data.py           # pilot (real)

``--smoke`` writes everything under ``outputs/logs/wu9-smoke-site/`` and
touches nothing in ``docs/``. Deterministic: no RNG, sorted iteration.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import rasterio.shutil
import yaml

# Repo-root import shim so the script runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wildfire_exposure_eo.schemas import (
    ExposureFeatureProperties,
    FuelLegendEntry,
    FwiOverlay,
    FwiOverlayComponent,
    GeobrowserArtifact,
    GeobrowserStyleData,
    InputRampSpec,
    InputRasterLayer,
    ScoredAsset,
    StudyAreaLayer,
    ValidationHeadline,
)
from wildfire_exposure_eo.stac import code_commit_sha

_ROOT = Path(__file__).resolve().parents[1]
_PARQUET_DIR = _ROOT / "outputs" / "parquet"
_COG_DIR = _ROOT / "outputs" / "cogs"
_VAL_DIR = _ROOT / "outputs" / "validation"
_GEOBROWSER_DIR = _ROOT / "outputs" / "geobrowser"
_CROSSWALK = _ROOT / "config" / "fuel_crosswalk.yaml"
_FIRE_WEATHER_CONFIG = _ROOT / "config" / "fire_weather.yaml"
_AOI_PILOT = _ROOT / "data" / "aoi" / "pilot.geojson"
_AOI_SMOKE = _ROOT / "data" / "aoi" / "smoke.geojson"

#: Manifest written by scripts/25_make_fwi_cogs.py (current-season EWDS FWI COGs).
_FWI_MANIFEST = _GEOBROWSER_DIR / "fwi_overlay_manifest.json"

#: Human labels for the FWI overlay components (display order is the manifest's).
_FWI_COMPONENT_LABELS = {
    "fwi": "Fire Weather Index (FWI)",
    "ffmc": "Fine Fuel Moisture Code (FFMC)",
    "dmc": "Duff Moisture Code (DMC)",
    "dc": "Drought Code (DC)",
    "isi": "Initial Spread Index (ISI)",
    "bui": "Build-Up Index (BUI)",
}

#: Cloudflare R2 bucket (custom domain ``wildfire.cheias.pt``) hosting the geodata
#: too large for the 2 000 kB committed-file cap — the burn-scar display COG and
#: ICNF burns GeoJSON. CORS + byte-range enabled (verified) so the static
#: geobrowser reads the COG client-side. See prompts/_session_log.md.
_ASSET_BASE_URL = "https://wildfire.cheias.pt"

#: GeoJSON feature properties exported for the site (subset of ScoredAsset).
#: ``historical_burn_share`` is lifted out of the nested ``features`` dict (see
#: ``_with_historical_burn_share``) so the geobrowser analyser table can offer an
#: honest burned-footprint filter without re-reading the GeoParquet client-side.
_EXPORT_PROPS = [
    "asset_id",
    "osm_type",
    "osm_id",
    "asset_class",
    "criticality_weight",
    "exposure_score",
    "exposure_rank",
    "historical_burn_share",
]

#: GeoJSON feature properties for the MERGED full-extent (Iberia) exposure layer.
#: Carries the per-AOI props plus ``aoi_name`` (which study area the row came
#: from — the per-AOI rank stays AOI-relative) and ``impact_severity`` (the
#: cross-AOI-comparable triage axis = score × criticality, normalised across the
#: pooled assets of all AOIs). See :func:`export_merged_iberia_geojson`.
_MERGED_EXPORT_PROPS = [*_EXPORT_PROPS, "aoi_name", "impact_severity"]

#: Wave-2 validation study areas beyond the pilot, in display order. Each is
#: scored independently (its own ``exposure_<aoi>_<run>.parquet``) and shown as
#: a toggleable exposure layer + AOI outline. The model_version is read from the
#: parquet provenance, never hardcoded (non-negotiable #1 / #3): these are
#: v0.3.0 runs and are labelled as such.
_STUDY_AREAS: tuple[tuple[str, str], ...] = (
    ("pedrogao_grande", "Pedrógão Grande"),
    ("serra_da_estrela", "Serra da Estrela"),
    ("peneda_geres", "Peneda-Gerês"),
    ("monchique", "Monchique"),
)

#: Coordinate precision (decimal degrees) for study-area exposure GeoJSON. 6 dp
#: is ≈0.1 m — finer than the asset geometry warrants, but keeps committed files
#: small. A study-area GeoJSON that still exceeds the 2 000 kB committed-file cap
#: after trimming is published to Cloudflare R2 instead (loads lazily on toggle).
_STUDY_AREA_COORD_PRECISION = 6

#: Committed-file cap (kB) enforced by the repo's check-added-large-files hook.
#: Study-area exposure GeoJSONs at or above this go to R2, not docs/app/data.
_COMMITTED_FILE_CAP_KB = 2000

#: Continuous model-INPUT raster kinds shown as toggleable per-AOI layers, with
#: the matplotlib colormap and FIXED display range each is stretched between.
#: Ranges are anchored to the measured COG value spread across the published
#: AOIs (canopy ~0-27 m, slope ~0-50°, NBR-delta ~-0.6..+1.1) and rounded to
#: readable bounds so one ramp reads consistently across every AOI. NEAREST
#: warp preserves raw values; nodata paints transparent client-side. Honest
#: scope (non-negotiable #6): these are relative model INPUTS, never
#: probabilities or forecasts. ``fuel_class`` is categorical and reuses the
#: existing ``fuel_legend`` — it carries no continuous ramp here.
_INPUT_RAMPS: tuple[tuple[str, str, str, str, float, float, str], ...] = (
    (
        "canopy_height",
        "Canopy height",
        "m",
        "YlGn",
        0.0,
        25.0,
        "Sentinel-2 / GEDI canopy height (metres). Taller, denser canopy is more "
        "fuel-loaded — a relative model input, not a probability.",
    ),
    (
        "slope",
        "Slope",
        "°",
        "YlOrBr",
        0.0,
        35.0,
        "Terrain slope (degrees, from the Copernicus DEM). Steeper slopes carry "
        "fire faster uphill — a relative model input, not a probability.",
    ),
    (
        "nbr_delta",
        "NBR-delta (ΔNBR)",
        "",
        "RdYlGn_r",
        -0.3,
        0.9,
        "Change in Normalized Burn Ratio over the window: positive (red) = "
        "vegetation loss / burn-severity signal, negative (green) = regrowth. A "
        "relative spectral input, not a probability and not a fire forecast.",
    ),
)

#: Input-kind → R2 display-COG filename prefix. The fuel input keeps the legacy
#: ``fuel_class`` prefix; the per-AOI fuel COGs are ``fuel_class_<aoi>_3857_*``.
_INPUT_PREFIX = {
    "canopy_height": "canopy_height",
    "slope": "slope",
    "nbr_delta": "nbr_delta",
    "fuel_class": "fuel_class",
}


#: Canonical published-artefact run-id: an ISO-ish UTC stamp YYYYMMDDTHHMMSSZ.
#: Working/diagnostic composites carry an extra descriptive token between the
#: prefix and the run-id (e.g. ``burn_scar_wu10multi_p85_<run_id>.tif``); those
#: must never be auto-selected for the published site.
_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")


def _latest(prefix: str, folder: Path, suffix: str, *, smoke: bool) -> Path:
    """Return the newest *canonical* artefact in *folder* (timestamps sort lexically).

    Canonical = the filename is exactly ``{prefix}[_smoke]_{run_id}{suffix}`` with
    a bare run-id (no extra descriptive token). This excludes working/diagnostic
    composites such as ``burn_scar_wu10degrid_p85_<run_id>.tif`` that share the
    prefix but must not be published.
    """
    pat = f"{prefix}_smoke_*{suffix}" if smoke else f"{prefix}_*{suffix}"
    middle = f"{prefix}_smoke_" if smoke else f"{prefix}_"
    cands = []
    for c in sorted(folder.glob(pat)):
        if not smoke and "_smoke_" in c.name:
            continue
        token = c.name[len(middle) : -len(suffix)] if suffix else c.name[len(middle) :]
        if _RUN_ID_RE.match(token):
            cands.append(c)
    if not cands:
        raise FileNotFoundError(
            f"No canonical artefact matching {pat!r} (bare run-id) in {folder}. "
            "Run the relevant WU pipeline step first, or rename the candidate to "
            f"{prefix}_<run_id>{suffix}."
        )
    return cands[-1]


def select_validated_run(*, smoke: bool) -> tuple[str, dict[str, Any]]:
    """Anchor on the latest WU-7 metrics JSON → (run_id, metrics dict).

    Guarantees the exported exposure GeoJSON is the run the committed
    validation report describes, not merely the newest scoring run.
    """
    metrics_path = _latest("metrics", _VAL_DIR, ".json", smoke=smoke)
    metrics = json.loads(metrics_path.read_text())
    return str(metrics["source_run_id"]), metrics


def _with_historical_burn_share(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add a top-level ``historical_burn_share`` column from the nested features.

    The Stage-2 features land in a single ``features`` column as a JSON string
    (e.g. ``{"slope_max_deg": ..., "historical_burn_share": 0.0, ...}``). The
    display GeoJSON flattens just ``historical_burn_share`` to a top-level
    property so the geobrowser can filter on a real burned-footprint signal
    without parsing nested JSON client-side. Rows whose features dict lacks the
    key (or have no ``features`` column at all) get ``None`` — the property is
    optional in :class:`ExposureFeatureProperties`. A descriptive footprint
    statistic, never a probability and never the post-window validation label.
    """
    out = gdf.copy()
    if "features" not in out.columns:
        # Float dtype (all-NaN): the GeoJSON writer emits JSON ``null`` for NaN,
        # and downstream ScoredAsset/ExposureFeatureProperties treat null as the
        # optional "feature absent for this run" case.
        out["historical_burn_share"] = np.nan
        return out

    def _share(raw: object) -> float:
        if not isinstance(raw, str) or not raw:
            return float("nan")
        val = json.loads(raw).get("historical_burn_share")
        return float("nan") if val is None else float(val)

    # Float dtype keeps the exported values numeric (the client-side analyser
    # filters on a real number); a missing share is NaN, which geopandas writes
    # as JSON ``null`` — never a bare NaN token.
    out["historical_burn_share"] = [_share(r) for r in out["features"]]
    return out


def export_exposure_geojson(parquet_path: Path, out_path: Path) -> int:
    """Scored parquet → display GeoJSON; every row schema-validated first."""
    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{parquet_path.name}: expected explicit EPSG:4326, got {gdf.crs}")
    for row in gdf.drop(columns="geometry").to_dict(orient="records"):
        ScoredAsset.model_validate(row)
    gdf = _with_historical_burn_share(gdf)
    # GeoDataFrame wrap keeps the type (and the geometry column's CRS) explicit
    # after column subsetting, which otherwise degrades to a DataFrame for the
    # type checker.
    out = gpd.GeoDataFrame(gdf.sort_values("exposure_rank")[[*_EXPORT_PROPS, "geometry"]])
    for props in out.drop(columns="geometry").to_dict(orient="records"):
        ExposureFeatureProperties.model_validate(props)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(out_path, driver="GeoJSON")
    return len(out)


def export_study_area_geojson(parquet_path: Path, out_path: Path, *, coord_precision: int) -> int:
    """Scored study-area parquet → display GeoJSON (same props as the pilot).

    Identical schema-validation and column subset as
    :func:`export_exposure_geojson`, but writes with a fixed coordinate
    precision so the committed file stays under the repo's large-file cap.
    """
    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{parquet_path.name}: expected explicit EPSG:4326, got {gdf.crs}")
    for row in gdf.drop(columns="geometry").to_dict(orient="records"):
        ScoredAsset.model_validate(row)
    gdf = _with_historical_burn_share(gdf)
    out = gpd.GeoDataFrame(gdf.sort_values("exposure_rank")[[*_EXPORT_PROPS, "geometry"]])
    for props in out.drop(columns="geometry").to_dict(orient="records"):
        ExposureFeatureProperties.model_validate(props)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(out_path, driver="GeoJSON", COORDINATE_PRECISION=coord_precision)
    return len(out)


def _impact_severity_raw(gdf: gpd.GeoDataFrame) -> pd.Series:  # type: ignore[type-arg]
    """Per-row raw triage severity = ``exposure_score`` × ``criticality_weight``.

    Both inputs are in [0, 1] (validated by ScoredAsset), so the product is too.
    This is the UN-normalised severity; the cross-AOI-comparable
    ``impact_severity`` divides it by the pooled global max (see
    :func:`export_merged_iberia_geojson`). NO re-score — derived from the
    existing scored parquet columns (non-negotiable #1 / #3).
    """
    return gdf["exposure_score"].astype(float) * gdf["criticality_weight"].astype(float)


def pooled_impact_severity_max(parquet_paths: list[Path]) -> float:
    """Global max of ``exposure_score`` × ``criticality_weight`` across all AOIs.

    The normaliser for the cross-AOI ``impact_severity`` axis: pooling every
    published AOI's assets and taking the single global max → 1 makes the
    full-extent (Iberia) layer's severity comparable across study areas (while
    the per-AOI ``exposure_rank`` stays AOI-relative — non-negotiable #6). Raises
    if no positive severity is found (a degenerate pool would make the axis
    meaningless).
    """
    gmax = 0.0
    for p in parquet_paths:
        gdf = gpd.read_parquet(p)
        m = float(_impact_severity_raw(gdf).max())
        gmax = max(gmax, m)
    if not (gmax > 0.0):
        raise ValueError(
            f"pooled impact-severity max is {gmax} "
            f"(no positive severity in {len(parquet_paths)} AOIs)"
        )
    return gmax


def export_merged_iberia_geojson(
    aoi_parquets: list[tuple[str, Path]],
    out_path: Path,
    *,
    global_sev_max: float,
    coord_precision: int,
) -> int:
    """Concatenate every AOI's scored assets → one full-extent (Iberia) GeoJSON.

    Each row carries the per-AOI display props (AOI-relative ``exposure_rank``
    untouched) plus ``aoi_name`` and ``impact_severity`` — the latter computed as
    ``exposure_score`` × ``criticality_weight`` then divided by *global_sev_max*
    (the pooled cross-AOI max), so the full-extent OUTPUT layer is coloured by a
    single cross-AOI-comparable triage axis. Every source row is validated
    against ``ScoredAsset`` first, and every merged feature against
    ``ExposureFeatureProperties`` (so ``impact_severity`` stays in [0, 1]).
    Honest scope (non-negotiable #6): a relative within-AOI exposure × asset
    criticality, normalised across study areas — NOT an absolute cross-region
    risk or probability.
    """
    parts: list[gpd.GeoDataFrame] = []
    for aoi_name, parquet_path in aoi_parquets:
        gdf = gpd.read_parquet(parquet_path)
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            raise ValueError(f"{parquet_path.name}: expected explicit EPSG:4326, got {gdf.crs}")
        for row in gdf.drop(columns="geometry").to_dict(orient="records"):
            ScoredAsset.model_validate(row)
        gdf = _with_historical_burn_share(gdf)
        gdf["aoi_name"] = aoi_name
        # Cross-AOI severity, clipped to [0, 1] against float round-off at the max.
        gdf["impact_severity"] = (_impact_severity_raw(gdf) / global_sev_max).clip(0.0, 1.0)
        parts.append(gpd.GeoDataFrame(gdf[[*_MERGED_EXPORT_PROPS, "geometry"]], crs="EPSG:4326"))
    merged = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326").sort_values(
        "impact_severity", ascending=False
    )
    for props in merged.drop(columns="geometry").to_dict(orient="records"):
        ExposureFeatureProperties.model_validate(
            {k: v for k, v in props.items() if k != "aoi_name"}
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(merged, crs="EPSG:4326").to_file(
        out_path, driver="GeoJSON", COORDINATE_PRECISION=coord_precision
    )
    return len(merged)


def study_area_provenance_model_version(parquet_path: Path) -> str:
    """Read ``model_version`` VERBATIM from the parquet provenance (#1 / #3).

    These study areas are v0.3.0 runs; the value is surfaced as-is and never
    relabelled. Raises if the provenance column or key is missing.
    """
    # pandas (not geopandas) so we can read the lone provenance column without
    # pulling the geometry — geopandas requires a geometry column be present.
    df = pd.read_parquet(parquet_path, columns=["provenance"])
    if "provenance" not in df.columns or len(df) == 0:
        raise ValueError(f"{parquet_path.name}: no provenance column")
    prov = json.loads(str(df["provenance"].iloc[0]))
    version = prov.get("model_version")
    if not version:
        raise ValueError(f"{parquet_path.name}: provenance missing model_version")
    return str(version)


def export_burns_geojson(
    parquet_path: Path, out_path: Path, *, coord_precision: int | None = None
) -> int:
    """ICNF burns parquet → display GeoJSON (feature_id, vintage_year, area_ha).

    ``coord_precision`` (decimal degrees) trims coordinate precision so the
    per-AOI burns files stay compact for R2 streaming; ``None`` keeps full
    precision (the pilot copy). 6 dp ≈ 0.1 m — finer than ICNF perimeter
    accuracy warrants, so it loses no honest geometry.
    """
    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{parquet_path.name}: expected explicit EPSG:4326, got {gdf.crs}")
    out = gpd.GeoDataFrame(
        gdf.sort_values(["vintage_year", "feature_id"])[
            ["feature_id", "vintage_year", "area_ha", "geometry"]
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if coord_precision is None:
        out.to_file(out_path, driver="GeoJSON")
    else:
        out.to_file(out_path, driver="GeoJSON", COORDINATE_PRECISION=coord_precision)
    return len(out)


def warp_to_3857_cog(src_path: Path, dst_path: Path) -> None:
    """Warp a COG to EPSG:3857 / GoogleMapsCompatible for client-side rendering.

    NEAREST resampling preserves raw values (categorical fuel codes and
    probability values alike — no interpolation, no smoothing);
    ``ZOOM_LEVEL_STRATEGY=UPPER`` picks the finer zoom level so the display
    copy never has a coarser pixel than the source. The source CRS must be
    explicit (non-negotiable #2).
    """
    with rasterio.open(src_path) as src:
        if src.crs is None:
            raise ValueError(f"{src_path.name}: source has no CRS")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    rasterio.shutil.copy(
        str(src_path),
        str(dst_path),
        driver="COG",
        TILING_SCHEME="GoogleMapsCompatible",
        ZOOM_LEVEL_STRATEGY="UPPER",
        RESAMPLING="NEAREST",
        OVERVIEW_RESAMPLING="NEAREST",
        COMPRESS="DEFLATE",
    )


def _lut(cmap_name: str) -> list[tuple[int, int, int]]:
    """256-step RGB LUT sampled from a matplotlib colormap."""
    cmap = plt.get_cmap(cmap_name)
    return [
        tuple(round(c * 255) for c in cmap(i / 255.0)[:3])  # type: ignore[misc]
        for i in range(256)
    ]


def fuel_legend(fuel_cog_path: Path) -> list[FuelLegendEntry]:
    """Fuel legend matching fig2: tab10 over the codes present, grey non-fuel."""
    crosswalk = {int(e["effis_code"]): e for e in yaml.safe_load(_CROSSWALK.read_text())["entries"]}
    with rasterio.open(fuel_cog_path) as src:
        band = src.read(1)
        nodata = src.nodata if src.nodata is not None else 255
    codes_present = sorted({int(c) for c in np.unique(band) if c not in (0, nodata)})
    cmap_base = plt.get_cmap("tab10", max(len(codes_present), 1))
    entries = [FuelLegendEntry(code=0, label="Non-fuel (0)", color=(204, 204, 204))]
    for i, code in enumerate(codes_present):
        entry = crosswalk.get(code, {})
        label = f"NFFL {code}: {entry.get('nffl_name', '?')} ({entry.get('internal_class', '?')})"
        rgb = tuple(round(c * 255) for c in cmap_base(i)[:3])
        entries.append(FuelLegendEntry(code=code, label=label, color=rgb))  # type: ignore[arg-type]
    return entries


def build_input_ramps() -> list[InputRampSpec]:
    """Display ramp + legend metadata per continuous model-INPUT kind.

    Each ramp carries the 256-step LUT sampled from its matplotlib colormap so
    the client paints the COG without matplotlib, plus the FIXED display range
    (:data:`_INPUT_RAMPS`) and an honest caption. The categorical ``fuel_class``
    kind is NOT included — it reuses the existing ``fuel_legend`` instead.
    """
    ramps: list[InputRampSpec] = []
    for kind, label, unit, cmap, vmin, vmax, caption in _INPUT_RAMPS:
        ramps.append(
            InputRampSpec(
                kind=kind,  # type: ignore[arg-type]
                label=label,
                unit=unit,
                cmap=cmap,
                lut=_lut(cmap),
                value_min=vmin,
                value_max=vmax,
                caption=caption,
            )
        )
    return ramps


def _input_cog_run(prefix: str, aoi: str) -> Path | None:
    """Latest source COG ``outputs/cogs/{prefix}_{aoi}_<run_id>.tif`` (or None)."""
    cands = sorted(_COG_DIR.glob(f"{prefix}_{aoi}_*.tif"))
    cands = [c for c in cands if _RUN_ID_RE.match(c.stem.replace(f"{prefix}_{aoi}_", ""))]
    return cands[-1] if cands else None


def build_input_layers(
    aoi: str, *, release_dir: Path, asset_base: str, kinds: tuple[str, ...]
) -> list[InputRasterLayer]:
    """Warp + emit one AOI's model-INPUT display COGs (canopy / slope / NBR / fuel).

    For each requested *kind* with a source COG under ``outputs/cogs`` named
    ``{prefix}_{aoi}_<run_id>.tif``, warp it to an EPSG:3857 display COG in
    ``release_dir`` (NEAREST, no resolution loss — :func:`warp_to_3857_cog`) for
    Cloudflare R2 upload, and return its :class:`InputRasterLayer` descriptor
    (href on the R2 base, CRS explicit per non-negotiable #2). Kinds without a
    source COG are simply skipped — the AOI then ships without that input layer.
    The fuel display COG keeps the legacy ``fuel_class_<aoi>_3857_<run>.tif``
    naming so it shares the burn-scar/fuel R2 convention.
    """
    layers: list[InputRasterLayer] = []
    for kind in kinds:
        prefix = _INPUT_PREFIX[kind]
        src = _input_cog_run(prefix, aoi)
        if src is None:
            continue
        run_id = src.stem.replace(f"{prefix}_{aoi}_", "")
        fname = f"{prefix}_{aoi}_3857_{run_id}.tif"
        dst = release_dir / fname
        warp_to_3857_cog(src, dst)
        layers.append(
            InputRasterLayer(
                kind=kind,  # type: ignore[arg-type]
                href=f"{asset_base}/{fname}",
                crs="EPSG:3857",
                run_id=run_id,
            )
        )
        print(f"  input {kind!r} {aoi!r}: {fname} ({dst.stat().st_size / 1e6:.2f} MB) → R2")
    return layers


def validation_headline(run_id: str, metrics: dict[str, Any]) -> ValidationHeadline:
    """Headline numbers read from the WU-7 metrics JSON — never re-derived."""
    full = metrics["full"]
    ablation = metrics["ablation"]
    degenerate = bool(full.get("degenerate", False))
    if degenerate:
        lift_kwargs: dict[str, float | None] = {}
    else:
        lift = {int(r["decile"]): r for r in full["lift_table"]}
        abl_lift = {int(r["decile"]): r for r in ablation["lift_table"]}
        lift_kwargs = {
            "top_decile_lift": float(lift[1]["lift"]),
            "cumulative_lift_top30pct": float(lift[3]["cumulative_lift"]),
            "spearman_rho": float(full["spearman_rho"]),
            "spearman_p": float(full["spearman_p"]),
            "ablation_top_decile_lift": float(abl_lift[1]["lift"]),
        }
    return ValidationHeadline(
        run_id=run_id,
        n_assets=int(full["n"]),
        n_burned=int(full["n_burned"]),
        base_rate=float(full["base_rate"]),
        degenerate=degenerate,
        window_end=str(metrics["window_end"]),
        validation_years=[int(y) for y in metrics["validation_years"]],
        **lift_kwargs,  # type: ignore[arg-type]
    )


def build_fwi_overlay(manifest_path: Path, asset_base: str) -> FwiOverlay | None:
    """Read the EWDS FWI COG manifest → :class:`FwiOverlay`, or ``None`` if absent.

    The manifest is written by ``scripts/25_make_fwi_cogs.py`` (the live EWDS
    pull). When it is missing the overlay is simply omitted from the bundle —
    the geobrowser then renders without the operational second axis rather than
    referencing COGs that were never produced. Each component's R2 href is built
    from the manifest filename and the public asset base; the attribution is read
    from the EWDS block of ``config/fire_weather.yaml`` (non-negotiable #1 — no
    invented identifiers).
    """
    if not manifest_path.exists():
        try:
            shown = manifest_path.relative_to(_ROOT)
        except ValueError:
            shown = manifest_path
        print(f"FWI overlay: no manifest at {shown} — overlay omitted")
        return None
    manifest = json.loads(manifest_path.read_text())
    fw_cfg = yaml.safe_load(_FIRE_WEATHER_CONFIG.read_text())
    attribution = str(fw_cfg["ewds_fwi"]["attribution"])
    components: list[FwiOverlayComponent] = []
    for comp in manifest["components"]:
        token = str(comp["component"])
        components.append(
            FwiOverlayComponent(
                component=token,
                label=_FWI_COMPONENT_LABELS.get(token, token.upper()),
                href=f"{asset_base}/{comp['filename']}",
                crs=str(manifest["display_crs"]),
                value_min=float(comp["value_min"]),
                value_max=float(comp["value_max"]),
            )
        )
    overlay = FwiOverlay(
        valid_date=str(manifest["fwi_valid_date"]),
        lag_note="~2-day lag",
        attribution=attribution,
        components=components,
    )
    print(f"FWI overlay: {len(components)} components, valid {overlay.valid_date}")
    return overlay


def _latest_study_area_parquet(name: str) -> Path | None:
    """Latest canonical ``exposure_<name>_<run_id>.parquet`` (or None)."""
    cands = sorted(_PARQUET_DIR.glob(f"exposure_{name}_*.parquet"))
    cands = [c for c in cands if _RUN_ID_RE.match(c.stem.replace(f"exposure_{name}_", ""))]
    return cands[-1] if cands else None


def resolve_aoi_parquets(pilot_parquet: Path) -> list[tuple[str, Path]]:
    """Ordered ``[(aoi_name, parquet)]`` for the pilot + every study area present.

    The pilot is first (``aoi_name == "pilot"``), then each study area in
    :data:`_STUDY_AREAS` display order that has a scored parquet. This is the
    pool the merged full-extent (Iberia) layer concatenates and the
    cross-AOI ``impact_severity`` normaliser ranges over.
    """
    pairs: list[tuple[str, Path]] = [("pilot", pilot_parquet)]
    for name, _label in _STUDY_AREAS:
        pq = _latest_study_area_parquet(name)
        if pq is not None:
            pairs.append((name, pq))
    return pairs


def build_study_areas(
    *,
    site_data: Path,
    release_dir: Path,
    asset_base: str,
    smoke: bool,
) -> list[StudyAreaLayer]:
    """Export each Wave-2 study area → committed (or R2) layer descriptors.

    For each AOI in :data:`_STUDY_AREAS`: pick the latest ``exposure_<aoi>_*``
    parquet, export the scored GeoJSON (committed under ``docs/app/data`` when it
    fits the large-file cap, else written to ``release_dir`` for R2 upload),
    copy the AOI outline, and read ``model_version`` verbatim from provenance.
    Returns the descriptors in display order. Skipped entirely in smoke mode and
    for AOIs without a scored parquet (the geobrowser then just omits them).
    """
    if smoke:
        return []
    layers: list[StudyAreaLayer] = []
    for name, label in _STUDY_AREAS:
        parquet = _latest_study_area_parquet(name)
        if parquet is None:
            print(f"study area {name!r}: no exposure parquet — skipped")
            continue
        run_id = parquet.stem.replace(f"exposure_{name}_", "")
        model_version = study_area_provenance_model_version(parquet)

        # Export to a staging path first so we can size-check before deciding
        # committed (docs/app/data) vs R2 (release_dir).
        fname = f"exposure_{name}_{run_id}.geojson"
        staged = release_dir / fname
        n_assets = export_study_area_geojson(
            parquet, staged, coord_precision=_STUDY_AREA_COORD_PRECISION
        )
        size_kb = staged.stat().st_size / 1024
        committed = size_kb < _COMMITTED_FILE_CAP_KB
        if committed:
            dst = site_data / fname
            dst.write_bytes(staged.read_bytes())
            staged.unlink()
            exposure_href = f"app/data/{fname}"
            where = f"committed ({size_kb:.0f} kB)"
        else:
            exposure_href = f"{asset_base}/{fname}"
            where = f"R2 ({size_kb:.0f} kB, over {_COMMITTED_FILE_CAP_KB} kB cap)"

        # AOI outline is always small — committed under docs/app/data.
        outline_src = _ROOT / "data" / "aoi" / f"{name}.geojson"
        aoi_gdf = gpd.read_file(outline_src)
        if aoi_gdf.crs is None or aoi_gdf.crs.to_epsg() != 4326:
            raise ValueError(f"{outline_src.name}: expected explicit EPSG:4326, got {aoi_gdf.crs}")
        outline_fname = f"aoi_{name}.geojson"
        aoi_gdf.to_file(site_data / outline_fname, driver="GeoJSON")
        minx, miny, maxx, maxy = (float(v) for v in aoi_gdf.total_bounds)

        # Per-AOI ICNF Áreas Ardidas perimeters: reuse the latest
        # icnf_burns_<aoi>_<run>.parquet if fetched (CLI `fetch-burns --aoi
        # data/aoi/<name>.geojson`), export to a compact (6 dp) GeoJSON in
        # release_dir for R2 upload, and attach its href. When no parquet exists
        # the AOI simply ships without an ICNF overlay (icnf_href stays None).
        icnf_href: str | None = None
        icnf_crs: str | None = None
        icnf_n: int | None = None
        icnf_cands = sorted(_PARQUET_DIR.glob(f"icnf_burns_{name}_*.parquet"))
        icnf_cands = [
            c for c in icnf_cands if _RUN_ID_RE.match(c.stem.replace(f"icnf_burns_{name}_", ""))
        ]
        if icnf_cands:
            icnf_pq = icnf_cands[-1]
            icnf_run = icnf_pq.stem.replace(f"icnf_burns_{name}_", "")
            icnf_fname = f"icnf_burns_{name}_{icnf_run}.geojson"
            icnf_n = export_burns_geojson(
                icnf_pq,
                release_dir / icnf_fname,
                coord_precision=_STUDY_AREA_COORD_PRECISION,
            )
            icnf_href = f"{asset_base}/{icnf_fname}"
            icnf_crs = "EPSG:4326"
            print(f"  ICNF perimeters {name!r}: {icnf_n} → R2 {icnf_fname}")
        else:
            print(f"  ICNF perimeters {name!r}: no parquet — overlay omitted")

        # Per-AOI model-INPUT display COGs (canopy / slope / NBR-delta / fuel),
        # warped to EPSG:3857 for R2. Each is toggleable and shown WITH this AOI.
        input_layers = build_input_layers(
            name,
            release_dir=release_dir,
            asset_base=asset_base,
            kinds=("canopy_height", "slope", "nbr_delta", "fuel_class"),
        )

        layers.append(
            StudyAreaLayer(
                name=name,
                label=label,
                exposure_href=exposure_href,
                exposure_crs="EPSG:4326",
                outline_href=f"app/data/{outline_fname}",
                outline_crs="EPSG:4326",
                run_id=run_id,
                model_version=model_version,
                n_assets=n_assets,
                committed=committed,
                bbox_4326=(minx, miny, maxx, maxy),
                icnf_href=icnf_href,
                icnf_crs=icnf_crs,
                icnf_n_perimeters=icnf_n,
                input_layers=input_layers,
            )
        )
        print(
            f"study area {name!r}: {n_assets} assets, model v{model_version}, "
            f"run {run_id} — {where}"
        )
    return layers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="smoke AOI, outputs/logs only")
    parser.add_argument(
        "--asset-base-url",
        default=_ASSET_BASE_URL,
        help="public base URL (Cloudflare R2) hosting the burn-scar display COG + burns GeoJSON",
    )
    args = parser.parse_args()
    smoke = bool(args.smoke)

    run_id, metrics = select_validated_run(smoke=smoke)
    exposure_pq = _PARQUET_DIR / (
        f"exposure_smoke_{run_id}.parquet" if smoke else f"exposure_{run_id}.parquet"
    )
    if not exposure_pq.exists():
        raise FileNotFoundError(f"validated exposure parquet missing: {exposure_pq}")
    fuel_cog = _latest("fuel_class", _COG_DIR, ".tif", smoke=smoke)
    burn_scar_cog = _latest("burn_scar", _COG_DIR, ".tif", smoke=smoke)
    burns_pq = _latest("icnf_burns", _PARQUET_DIR, ".parquet", smoke=smoke)
    fuel_run_id = fuel_cog.stem.replace("fuel_class_smoke_", "").replace("fuel_class_", "")
    burn_scar_run_id = burn_scar_cog.stem.replace("burn_scar_smoke_", "").replace("burn_scar_", "")
    burns_run_id = burns_pq.stem.replace("icnf_burns_smoke_", "").replace("icnf_burns_", "")

    if smoke:
        site_data = _ROOT / "outputs" / "logs" / "wu9-smoke-site" / "data"
        release_dir = _ROOT / "outputs" / "logs" / "wu9-smoke-site" / "release"
    else:
        site_data = _ROOT / "docs" / "app" / "data"
        release_dir = _ROOT / "outputs" / "geobrowser"
    site_data.mkdir(parents=True, exist_ok=True)
    release_dir.mkdir(parents=True, exist_ok=True)

    n_assets = export_exposure_geojson(exposure_pq, site_data / f"exposure_assets_{run_id}.geojson")
    print(f"exposure GeoJSON: {n_assets} assets (run {run_id})")

    aoi_src = _AOI_SMOKE if smoke else _AOI_PILOT
    aoi = gpd.read_file(aoi_src)
    if aoi.crs is None or aoi.crs.to_epsg() != 4326:
        raise ValueError(f"{aoi_src.name}: expected explicit EPSG:4326, got {aoi.crs}")
    aoi.to_file(site_data / "aoi.geojson", driver="GeoJSON")
    print(f"AOI copy: {aoi_src.name}")

    # Fuel display COG is written to release_dir (NOT committed) because it is
    # served from Cloudflare R2: a same-origin committed COG renders live but
    # stays blank under the local `python -m http.server` preview, which cannot
    # serve the HTTP Range requests geotiff.js needs. Upload this file to
    # r2:wildfire-exposure-eo so the fuel layer renders in local + live.
    fuel_3857 = release_dir / f"fuel_class_3857_{fuel_run_id}.tif"
    warp_to_3857_cog(fuel_cog, fuel_3857)
    print(f"fuel display COG: {fuel_3857.name} ({fuel_3857.stat().st_size / 1e6:.2f} MB)")

    burn_3857 = release_dir / f"burn_scar_3857_{burn_scar_run_id}.tif"
    warp_to_3857_cog(burn_scar_cog, burn_3857)
    print(f"burn-scar display COG: {burn_3857.name} ({burn_3857.stat().st_size / 1e6:.2f} MB)")

    burns_geojson = release_dir / f"icnf_burns_{burns_run_id}.geojson"
    n_burns = export_burns_geojson(burns_pq, burns_geojson)
    print(f"burns GeoJSON: {n_burns} perimeters → {burns_geojson.name}")

    asset_base = args.asset_base_url
    study_areas = build_study_areas(
        site_data=site_data, release_dir=release_dir, asset_base=asset_base, smoke=smoke
    )
    print(f"study areas: {len(study_areas)} wired ({', '.join(s.name for s in study_areas)})")

    # MERGED full-extent (Iberia) exposure layer — the OUTPUT theme. Concatenates
    # the pilot + every study area, each row carrying aoi_name + the cross-AOI
    # impact_severity (score × criticality, normalised across the pooled assets of
    # all AOIs → global max 1). Skipped in smoke (only the smoke tile is scored).
    # Written to release_dir (R2) when it exceeds the committed-file cap — the
    # repo's large-geodata pattern; committed under docs/app/data otherwise.
    merged_href: str | None = None
    merged_n = 0
    if not smoke:
        aoi_parquets = resolve_aoi_parquets(exposure_pq)
        sev_max = pooled_impact_severity_max([p for _name, p in aoi_parquets])
        merged_fname = f"exposure_assets_all_iberia_{run_id}.geojson"
        merged_staged = release_dir / merged_fname
        merged_n = export_merged_iberia_geojson(
            aoi_parquets,
            merged_staged,
            global_sev_max=sev_max,
            coord_precision=_STUDY_AREA_COORD_PRECISION,
        )
        merged_kb = merged_staged.stat().st_size / 1024
        if merged_kb < _COMMITTED_FILE_CAP_KB:
            (site_data / merged_fname).write_bytes(merged_staged.read_bytes())
            merged_staged.unlink()
            merged_href = f"app/data/{merged_fname}"
            print(
                f"merged Iberia exposure: {merged_n} assets across {len(aoi_parquets)} AOIs "
                f"(sev_max {sev_max:.4f}) → committed ({merged_kb:.0f} kB)"
            )
        else:
            merged_href = f"{asset_base}/{merged_fname}"
            print(
                f"merged Iberia exposure: {merged_n} assets across {len(aoi_parquets)} AOIs "
                f"(sev_max {sev_max:.4f}) → R2 ({merged_kb:.0f} kB, "
                f"over {_COMMITTED_FILE_CAP_KB} kB cap)"
            )

    # Pilot model-INPUT display COGs: canopy / slope / NBR-delta (the pilot FUEL
    # input keeps its dedicated artifacts["fuel_class"] entry, so it is NOT
    # duplicated here). Warped to EPSG:3857 for R2 like the study-area inputs.
    pilot_input_layers = (
        []
        if smoke
        else build_input_layers(
            "pilot",
            release_dir=release_dir,
            asset_base=asset_base,
            kinds=("canopy_height", "slope", "nbr_delta"),
        )
    )
    input_ramps = [] if smoke else build_input_ramps()
    print(f"pilot input layers: {len(pilot_input_layers)} wired")

    artifacts: dict[str, GeobrowserArtifact] = {
        "exposure_assets": GeobrowserArtifact(
            href=f"app/data/exposure_assets_{run_id}.geojson",
            crs="EPSG:4326",
            run_id=run_id,
            role="display",
            description=(
                "Scored assets (subset of the ScoredAsset columns; authoritative "
                "GeoParquet with full per-row provenance is the STAC asset)"
            ),
        ),
    }
    # Merged full-extent (Iberia) exposure layer — the OUTPUT theme. Coloured by
    # the cross-AOI impact_severity (score × criticality, normalised across the
    # pooled assets of all AOIs). Present only for the real (non-smoke) bundle.
    if merged_href is not None:
        artifacts["exposure_assets_iberia"] = GeobrowserArtifact(
            href=merged_href,
            crs="EPSG:4326",
            run_id=run_id,
            role="display",
            description=(
                f"Merged full-extent exposure assets across {merged_n} rows / all "
                "study areas; coloured by impact_severity = within-AOI exposure × "
                "asset criticality, normalised across study areas (NOT an absolute "
                "cross-region risk or probability). Per-AOI exposure_rank stays "
                "AOI-relative."
            ),
        )

    style = GeobrowserStyleData(
        generated_by=f"scripts/15_make_geobrowser_data.py at {code_commit_sha(cwd=_ROOT)}",
        code_commit_sha=code_commit_sha(cwd=_ROOT),
        viridis_lut=_lut("viridis"),
        ylorrd_lut=_lut("YlOrRd"),
        fuel_legend=fuel_legend(fuel_cog),
        validation=validation_headline(run_id, metrics),
        artifacts={
            **artifacts,
            "aoi": GeobrowserArtifact(
                href="app/data/aoi.geojson",
                crs="EPSG:4326",
                run_id="frozen",
                role="authoritative",
                description="Pilot AOI boundary (copy of data/aoi/pilot.geojson)",
            ),
            "fuel_class": GeobrowserArtifact(
                # Hosted on Cloudflare R2 (byte-range capable) rather than the
                # committed copy: maplibre-cog-protocol / geotiff.js needs HTTP
                # Range, which the local `python -m http.server` preview does NOT
                # support, so a same-origin committed COG renders live but stays
                # blank in local preview. R2 serves 206 + CORS so the fuel layer
                # renders in BOTH local preview and live. See prompts/_session_log.md.
                href=f"{asset_base}/fuel_class_3857_{fuel_run_id}.tif",
                crs="EPSG:3857",
                run_id=fuel_run_id,
                role="display",
                description=(
                    "Fuel-class COG display copy (warped from the authoritative "
                    "EPSG:32629 STAC asset, NEAREST, no resolution loss)"
                ),
            ),
            "burn_scar": GeobrowserArtifact(
                href=f"{asset_base}/burn_scar_3857_{burn_scar_run_id}.tif",
                crs="EPSG:3857",
                run_id=burn_scar_run_id,
                role="display",
                description=(
                    "Burn-scar inference-probability COG display copy (warped from "
                    "the authoritative EPSG:4326 STAC/R2 asset, NEAREST)"
                ),
            ),
            "icnf_burns": GeobrowserArtifact(
                href=f"{asset_base}/icnf_burns_{burns_run_id}.geojson",
                crs="EPSG:4326",
                run_id=burns_run_id,
                role="display",
                description="ICNF Áreas Ardidas perimeters (1990–2025 vintages inside the AOI)",
            ),
        },
        study_areas=study_areas,
        pilot_input_layers=pilot_input_layers,
        input_ramps=input_ramps,
        fwi_overlay=build_fwi_overlay(_FWI_MANIFEST, asset_base),
    )
    style_path = site_data / "style_data.json"
    style_path.write_text(style.model_dump_json(indent=1) + "\n")
    print(f"style data: {style_path.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
