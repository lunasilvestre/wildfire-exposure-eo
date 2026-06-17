"""Generate current-season EWDS FWI display COGs for the geobrowser overlay.

The geobrowser's second axis is CURRENT OBSERVED fire weather alongside the
VALIDATED STRUCTURAL exposure rank. This script pulls the latest available CEMS
EWDS ``cems-fire-historical-v1`` reanalysis day (intermediate_dataset, ~2-day
lag) for the Canadian Fire Weather Index system — FWI + FFMC/DMC/DC/ISI/BUI —
over a BROAD extent (the union of all canonical AOIs + a 0.5° margin), so the
coarse 0.25° grid shows real spatial variation across mainland Portugal.

Each component is reprojected EPSG:4326 -> EPSG:3857 (BILINEAR; continuous
danger indices) and written as ``fwi_<comp>_3857_<validdate>.tif`` under
``outputs/geobrowser/`` (uploaded to Cloudflare R2 by the operator / wiring
step; too large/transient to commit).

Terminology guard (CLAUDE.md non-negotiable #6): these are OBSERVED REANALYSIS
danger *indices* (relative regional context, ~2-day lag, 0.25° grid), NEVER a
forecast or a probability of fire. CRS (#2): explicit at every step. AOI (#10):
the extent is the union of ``data/aoi/*.geojson`` read at runtime, never
hardcoded. Credentials (security): the EWDS key is read from ``CDSAPI_KEY`` or
``~/.cdsapirc`` and is NEVER printed or written to any artefact.

Usage::

    uv run python scripts/25_make_fwi_cogs.py                 # live pull, latest date
    uv run python scripts/25_make_fwi_cogs.py --date 2026-06-12
    uv run python scripts/25_make_fwi_cogs.py --smoke         # offline config check, exit 0
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from shapely.geometry import box
from shapely.ops import unary_union

# Repo-root import shim so the script runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wildfire_exposure_eo.fire_weather import (
    EwdsFwiSurface,
    build_ewds_fwi_surface,
    ewds_fwi_provenance,
    fwi_component_value_range,
    load_ewds_fwi_config,
    load_ewds_key,
    write_fwi_component_cog,
)
from wildfire_exposure_eo.stac import load_aoi_geometry

_ROOT = Path(__file__).resolve().parents[1]
_AOI_DIR = _ROOT / "data" / "aoi"
_CONFIG_PATH = _ROOT / "config" / "fire_weather.yaml"
_OUT_DIR = _ROOT / "outputs" / "geobrowser"

#: Canonical source AOIs (pilot + the four Wave-2 validation AOIs). The ``alt_``
#: and ``smoke_`` variants are working copies, not extent definitions, so they
#: are excluded — the broad overlay extent is the union of these five.
_CANONICAL_AOIS = (
    "pilot",
    "monchique",
    "pedrogao_grande",
    "peneda_geres",
    "serra_da_estrela",
)

#: Margin (degrees) added around the AOI union so the 0.25° EWDS grid shows
#: regional variation, not a single near-uniform cell over each AOI.
_MARGIN_DEG = 0.5

#: Geobrowser overlay components, in display order: the headline FWI plus its
#: five Canadian-system sub-components. Maps the feature column (config) to the
#: short COG token used in the filename ``fwi_<token>_3857_<validdate>.tif``.
_OVERLAY_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("fwi_fwi_current", "fwi"),
    ("fwi_ffmc_current", "ffmc"),
    ("fwi_dmc_current", "dmc"),
    ("fwi_dc_current", "dc"),
    ("fwi_isi_current", "isi"),
    ("fwi_bui_current", "bui"),
)

#: How many days back from ``--date`` (or today) to try before giving up when
#: the most recent day is not yet published (EWDS lags ~2 days, sometimes more).
_MAX_LOOKBACK_DAYS = 8


def union_bbox_with_margin() -> tuple[float, float, float, float]:
    """Union bbox of the canonical AOIs, expanded by :data:`_MARGIN_DEG`.

    Reads each ``data/aoi/<name>.geojson`` at runtime (non-negotiable #10 — no
    hardcoded coordinates). Returns ``(minlon, minlat, maxlon, maxlat)``.
    """
    geoms = []
    for name in _CANONICAL_AOIS:
        geom, _ = load_aoi_geometry(_AOI_DIR / f"{name}.geojson")
        geoms.append(geom)
    union = unary_union(geoms)
    minx, miny, maxx, maxy = union.bounds
    return (
        float(minx) - _MARGIN_DEG,
        float(miny) - _MARGIN_DEG,
        float(maxx) + _MARGIN_DEG,
        float(maxy) + _MARGIN_DEG,
    )


def latest_available_surface(
    bbox: tuple[float, float, float, float],
    start: date,
    config_path: Path,
    *,
    key: str,
) -> EwdsFwiSurface:
    """Pull the most recent published EWDS FWI day at or before ``start``.

    Walks back up to :data:`_MAX_LOOKBACK_DAYS` days; the first request that
    returns a non-null surface wins (EWDS raises for an unpublished day, and a
    published-but-empty day is flagged ``is_null``). The AOI extent is a box, so
    a generous request envelope covers the whole union.
    """
    config = load_ewds_fwi_config(config_path)
    envelope = box(*bbox)
    last_error: Exception | None = None
    for back in range(_MAX_LOOKBACK_DAYS + 1):
        when = start - timedelta(days=back)
        try:
            surface = build_ewds_fwi_surface(envelope, when, config, key=key)
        except Exception as exc:  # EWDS raises a 4xx for an unpublished day
            last_error = exc
            print(
                f"[fwi-cogs] {when.isoformat()}: not available ({exc}); trying earlier",
                file=sys.stderr,
            )
            continue
        if surface.is_null:
            print(
                f"[fwi-cogs] {when.isoformat()}: published but all-null; trying earlier",
                file=sys.stderr,
            )
            continue
        return surface
    raise RuntimeError(
        f"no published EWDS FWI day in {start.isoformat()}..-{_MAX_LOOKBACK_DAYS}d; "
        f"last error: {last_error}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=str,
        default=date.today().isoformat(),
        help="latest date to try (ISO); walks back to the newest published day",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=_OUT_DIR, help="directory for the FWI display COGs"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=_OUT_DIR / "fwi_overlay_manifest.json",
        help="JSON manifest of the written COGs (component, href token, value range, valid date)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="offline: validate config + component list only, no network/key; always exit 0",
    )
    args = parser.parse_args()

    config = load_ewds_fwi_config(_CONFIG_PATH)
    available = {v.feature_name for v in config.variables}
    missing = [feat for feat, _ in _OVERLAY_COMPONENTS if feat not in available]
    if missing:
        raise ValueError(f"overlay components missing from ewds config: {missing}")

    if args.smoke:
        bbox = union_bbox_with_margin()
        print(
            "[smoke] ewds overlay OK: components="
            + ",".join(tok for _, tok in _OVERLAY_COMPONENTS),
            file=sys.stderr,
        )
        print(
            f"[smoke] union bbox (minlon,minlat,maxlon,maxlat) = "
            f"{tuple(round(c, 4) for c in bbox)}",
            file=sys.stderr,
        )
        return 0

    bbox = union_bbox_with_margin()
    print(
        f"[fwi-cogs] union+{_MARGIN_DEG}deg bbox = {tuple(round(c, 4) for c in bbox)}",
        file=sys.stderr,
    )
    key = load_ewds_key()  # CDSAPI_KEY env or ~/.cdsapirc; never printed
    surface = latest_available_surface(bbox, date.fromisoformat(args.date), _CONFIG_PATH, key=key)
    valid = surface.valid_date.isoformat()
    print(f"[fwi-cogs] FWI valid date: {valid}", file=sys.stderr)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, object]] = []
    for feature_name, token in _OVERLAY_COMPONENTS:
        vmin, vmax = fwi_component_value_range(surface, feature_name)
        dst = args.out_dir / f"fwi_{token}_3857_{valid}.tif"
        write_fwi_component_cog(surface, feature_name, dst)
        size_mb = dst.stat().st_size / 1e6
        print(
            f"[fwi-cogs]   {token:5s} -> {dst.name} ({size_mb:.3f} MB; "
            f"range {vmin:.2f}..{vmax:.2f})",
            file=sys.stderr,
        )
        written.append(
            {
                "component": token,
                "feature_name": feature_name,
                "filename": dst.name,
                "value_min": round(vmin, 4),
                "value_max": round(vmax, 4),
            }
        )

    provenance = ewds_fwi_provenance(config, surface)  # no key in here
    manifest = {
        "fwi_valid_date": valid,
        "requested_date": surface.requested_date.isoformat(),
        "extent_bbox_4326": [round(c, 6) for c in bbox],
        "extent_source_aois": list(_CANONICAL_AOIS),
        "extent_margin_deg": _MARGIN_DEG,
        "display_crs": "EPSG:3857",
        "components": written,
        "provenance": provenance,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[fwi-cogs] wrote {len(written)} COGs + manifest {args.manifest.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
