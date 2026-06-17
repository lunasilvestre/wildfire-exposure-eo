"""Generate the static geodata + style bundle for the Pages geobrowser (WU-9, prompt 15).

Everything the `docs/` site renders is emitted here from the authoritative
pipeline artefacts — nothing hand-made:

* ``docs/app/data/exposure_assets_<run_id>.geojson`` — scored assets
  (EPSG:4326, full coordinate precision). Every source row is validated
  against the ``ScoredAsset`` schema before export; feature properties follow
  ``ExposureFeatureProperties``.
* ``docs/app/data/aoi.geojson`` — AOI boundary copy (EPSG:4326).
* ``docs/app/data/fuel_class_3857_<run_id>.tif`` — display copy of the fuel
  COG warped to EPSG:3857 / GoogleMapsCompatible tiling, NEAREST resampling,
  ``ZOOM_LEVEL_STRATEGY=UPPER`` (no resolution loss). Required because
  maplibre-cog-protocol renders EPSG:3857 COGs only — the authoritative
  EPSG:32629 COG stays the STAC asset.
* ``outputs/geobrowser/burn_scar_3857_<run_id>.tif`` — same warp for the
  burn-scar COG (authoritative CRS EPSG:4326); uploaded to Cloudflare R2,
  too large to commit.
* ``outputs/geobrowser/icnf_burns_<run_id>.geojson`` — ICNF perimeter display
  copy (EPSG:4326, full precision); uploaded to Cloudflare R2 (7.8 MB,
  over the repo's 2 000 kB committed-file cap).
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
    ScoredAsset,
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

#: Manifest written by scripts/23_make_fwi_cogs.py (current-season EWDS FWI COGs).
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
_EXPORT_PROPS = [
    "asset_id",
    "osm_type",
    "osm_id",
    "asset_class",
    "criticality_weight",
    "exposure_score",
    "exposure_rank",
]


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


def export_exposure_geojson(parquet_path: Path, out_path: Path) -> int:
    """Scored parquet → display GeoJSON; every row schema-validated first."""
    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{parquet_path.name}: expected explicit EPSG:4326, got {gdf.crs}")
    for row in gdf.drop(columns="geometry").to_dict(orient="records"):
        ScoredAsset.model_validate(row)
    # GeoDataFrame wrap keeps the type (and the geometry column's CRS) explicit
    # after column subsetting, which otherwise degrades to a DataFrame for the
    # type checker.
    out = gpd.GeoDataFrame(gdf.sort_values("exposure_rank")[[*_EXPORT_PROPS, "geometry"]])
    for props in out.drop(columns="geometry").to_dict(orient="records"):
        ExposureFeatureProperties.model_validate(props)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(out_path, driver="GeoJSON")
    return len(out)


def export_burns_geojson(parquet_path: Path, out_path: Path) -> int:
    """ICNF burns parquet → display GeoJSON (feature_id, vintage_year, area_ha)."""
    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"{parquet_path.name}: expected explicit EPSG:4326, got {gdf.crs}")
    out = gpd.GeoDataFrame(
        gdf.sort_values(["vintage_year", "feature_id"])[
            ["feature_id", "vintage_year", "area_ha", "geometry"]
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(out_path, driver="GeoJSON")
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

    The manifest is written by ``scripts/23_make_fwi_cogs.py`` (the live EWDS
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

    fuel_3857 = site_data / f"fuel_class_3857_{fuel_run_id}.tif"
    warp_to_3857_cog(fuel_cog, fuel_3857)
    print(f"fuel display COG: {fuel_3857.name} ({fuel_3857.stat().st_size / 1e6:.2f} MB)")

    burn_3857 = release_dir / f"burn_scar_3857_{burn_scar_run_id}.tif"
    warp_to_3857_cog(burn_scar_cog, burn_3857)
    print(f"burn-scar display COG: {burn_3857.name} ({burn_3857.stat().st_size / 1e6:.2f} MB)")

    burns_geojson = release_dir / f"icnf_burns_{burns_run_id}.geojson"
    n_burns = export_burns_geojson(burns_pq, burns_geojson)
    print(f"burns GeoJSON: {n_burns} perimeters → {burns_geojson.name}")

    asset_base = args.asset_base_url
    style = GeobrowserStyleData(
        generated_by=f"scripts/15_make_geobrowser_data.py at {code_commit_sha(cwd=_ROOT)}",
        code_commit_sha=code_commit_sha(cwd=_ROOT),
        viridis_lut=_lut("viridis"),
        ylorrd_lut=_lut("YlOrRd"),
        fuel_legend=fuel_legend(fuel_cog),
        validation=validation_headline(run_id, metrics),
        artifacts={
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
            "aoi": GeobrowserArtifact(
                href="app/data/aoi.geojson",
                crs="EPSG:4326",
                run_id="frozen",
                role="authoritative",
                description="Pilot AOI boundary (copy of data/aoi/pilot.geojson)",
            ),
            "fuel_class": GeobrowserArtifact(
                href=f"app/data/fuel_class_3857_{fuel_run_id}.tif",
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
        fwi_overlay=build_fwi_overlay(_FWI_MANIFEST, asset_base),
    )
    style_path = site_data / "style_data.json"
    style_path.write_text(style.model_dump_json(indent=1) + "\n")
    print(f"style data: {style_path.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
