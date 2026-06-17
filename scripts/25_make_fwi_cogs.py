"""Generate current-season EWDS FWI display COGs for the geobrowser overlay.

The geobrowser's second axis is CURRENT OBSERVED fire weather alongside the
VALIDATED STRUCTURAL exposure rank. This script pulls the latest available CEMS
EWDS ``cems-fire-historical-v1`` reanalysis day (intermediate_dataset, ~2-day
lag) for the Canadian Fire Weather Index system — FWI + FFMC/DMC/DC/ISI/BUI —
over the WHOLE IBERIAN PENINSULA (mainland Portugal + Spain; see
:data:`IBERIA_BBOX`), so the overlay reads as honest coarse REGIONAL CONTEXT for
the study areas rather than a tight strip clipped to the AOIs. The grid is
genuinely 0.25° (~28 km cells, ~56×32 over Iberia); it is shown as discrete
cells (NEAREST, see ``write_fwi_component_cog``), never blurred to look finer
than it is, and the client paints ocean / no-coverage cells fully transparent.

Each component is reprojected EPSG:4326 -> EPSG:3857 (NEAREST; discrete 0.25°
cells — the field is genuinely coarse and is shown as such, never interpolated
to look finer) and written as ``fwi_<comp>_3857_<validdate>.tif`` under
``outputs/geobrowser/`` (uploaded to Cloudflare R2 by the operator / wiring
step; too large/transient to commit).

Terminology guard (CLAUDE.md non-negotiable #6): these are OBSERVED REANALYSIS
danger *indices* (relative regional context, ~2-day lag, 0.25° grid), NEVER a
forecast or a probability of fire. CRS (#2): explicit at every step. AOI (#10):
:data:`IBERIA_BBOX` is the FWI regional-context DISPLAY extent, NOT an AOI — the
frozen pilot/validation AOIs in ``data/aoi/*.geojson`` are unchanged and still
govern all scoring; a context-layer display bbox is a documented design choice,
not a magic number. Credentials (security): the EWDS key is read from
``CDSAPI_KEY`` or ``~/.cdsapirc`` and is NEVER printed or written to any
artefact.

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

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _ROOT / "config" / "fire_weather.yaml"
_OUT_DIR = _ROOT / "outputs" / "geobrowser"

#: FWI regional-context DISPLAY extent — the WHOLE Iberian Peninsula (mainland
#: Portugal + Spain), as ``(minlon, minlat, maxlon, maxlat)`` in EPSG:4326.
#:
#: PROVENANCE / non-negotiable #10: this is the FWI overlay's display bbox, NOT
#: an AOI. The frozen pilot + validation AOIs in ``data/aoi/*.geojson`` are
#: unchanged and still govern all scoring; this constant only sets how wide a
#: regional fire-weather backdrop is drawn behind them. It is a deliberate,
#: documented context-layer extent (not a magic number): mainland-Iberia bounds
#: rounded to the 0.25° EWDS grid — westernmost Cabo da Roca ≈ -9.5°E, easternmost
#: Cap de Creus ≈ 3.3°E, Punta de Tarifa ≈ 36.0°N, Punta de Estaca de Bares
#: ≈ 43.8°N — padded a little so coastal cells are not clipped. At 0.25° this is
#: ~56×32 cells, a small request. Atlantic / Mediterranean / no-coverage cells
#: render fully transparent client-side (NaN nodata; see docs/app/app.js).
IBERIA_BBOX: tuple[float, float, float, float] = (-9.8, 35.9, 3.5, 44.0)

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
        print(
            "[smoke] ewds overlay OK: components="
            + ",".join(tok for _, tok in _OVERLAY_COMPONENTS),
            file=sys.stderr,
        )
        print(
            f"[smoke] iberia display bbox (minlon,minlat,maxlon,maxlat) = {IBERIA_BBOX}",
            file=sys.stderr,
        )
        return 0

    bbox = IBERIA_BBOX
    print(
        f"[fwi-cogs] iberia display bbox = {bbox}",
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
        "extent_kind": "iberia_regional_context_display",
        "extent_note": (
            "FWI regional-context display extent over mainland Iberia (PT+Spain); "
            "NOT an AOI — frozen AOIs in data/aoi/ govern scoring (non-negotiable #10)"
        ),
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
