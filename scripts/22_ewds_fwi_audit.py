"""Live verification of the EWDS current-season FWI source (CEMS EWDS).

A SECOND fire-weather source alongside the GWIS backtest probed by
``scripts/17_fire_weather_audit.py``. This one pulls the FULL Canadian Fire
Weather Index *system* (FWI + its five components + the U.S. NFDRS burning
index) from the CEMS Early Warning Data Store ``cems-fire-historical-v1``
reanalysis (daily-updated, ~2-day lag) over the pilot AOI for one recent date,
and reports per-component min/median/max, the request->netCDF variable map, the
netCDF ``valid_time``, the asserted CRS, and that longitude was normalised.

    uv run python scripts/22_ewds_fwi_audit.py                 # live pull
    uv run python scripts/22_ewds_fwi_audit.py --date 2026-06-10
    uv run python scripts/22_ewds_fwi_audit.py --smoke         # offline, exit 0

Identity rule (CLAUDE.md non-negotiable #1): no invented identifiers — the
request combo, dataset, and variable map are read from a real download and
pinned in ``config/fire_weather.yaml``. Terminology guard (#6): these are
observed reanalysis danger *indices*, a relative input each, never a forecast or
probability of fire. Credentials (security): the EWDS key is read from
``CDSAPI_KEY`` or ``~/.cdsapirc`` and is NEVER printed. CRS (#2): the netCDF is
EPSG:4326 and the 0..360 longitude is normalised to -180..180; both asserted.
AOI (#10): the bbox is always read from the GeoJSON, never hardcoded.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

from wildfire_exposure_eo.fire_weather import (
    build_ewds_fwi_surface,
    load_ewds_fwi_config,
    load_ewds_key,
)
from wildfire_exposure_eo.stac import load_aoi_geometry

CONFIG_PATH = Path("config/fire_weather.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aoi", type=Path, default=Path("data/aoi/pilot.geojson"), help="AOI GeoJSON for the pull"
    )
    parser.add_argument(
        "--date", type=str, default="2026-06-10", help="requested date (ISO); EWDS lags ~2 days"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("outputs/diagnostics/22_ewds_fwi_audit.json"),
        help="verdict JSON output path",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="offline: validate config + variable map only, no network/key; always exit 0",
    )
    args = parser.parse_args()

    cfg = load_ewds_fwi_config(CONFIG_PATH)

    if args.smoke:
        print(
            f"[smoke] ewds config OK: dataset={cfg.dataset!r} dataset_type={cfg.dataset_type!r} "
            f"system_version={cfg.system_version!r} doi={cfg.doi!r} crs={cfg.crs!r}",
            file=sys.stderr,
        )
        print(
            "[smoke] variable map: "
            + ", ".join(
                f"{v.request_name}->{v.netcdf_var}({v.feature_name})" for v in cfg.variables
            ),
            file=sys.stderr,
        )
        return 0

    geom, _ = load_aoi_geometry(args.aoi)
    when = date.fromisoformat(args.date)
    key = load_ewds_key()  # CDSAPI_KEY env or ~/.cdsapirc; never printed
    surface = build_ewds_fwi_surface(geom, when, cfg, key=key)

    stats: dict[str, dict[str, float]] = {}
    for var in cfg.variables:
        da = surface.components[var.feature_name]
        vals = np.asarray(da.values, dtype="float64")
        vals = vals[np.isfinite(vals)]
        stats[var.feature_name] = {
            "min": float(np.min(vals)),
            "median": float(np.median(vals)),
            "max": float(np.max(vals)),
        }

    a_da = surface.components[cfg.variables[0].feature_name]
    epsg = a_da.rio.crs.to_epsg() if a_da.rio.crs is not None else None
    lon = np.asarray(a_da["x"].values, dtype="float64")
    lon_normalized = bool(np.all(lon <= 180.0) and np.any(lon < 0.0))

    report = {
        "aoi": str(args.aoi),
        "requested_date": when.isoformat(),
        "fwi_valid_date": surface.valid_date.isoformat(),
        "dataset": cfg.dataset,
        "dataset_type": cfg.dataset_type,
        "system_version": cfg.system_version,
        "doi": cfg.doi,
        "crs_epsg": epsg,
        "lon_normalized_to_180": lon_normalized,
        "is_null": surface.is_null,
        "variable_map": {v.request_name: v.netcdf_var for v in cfg.variables},
        "stats": stats,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))

    print(f"fwi_valid_date: {surface.valid_date.isoformat()}", file=sys.stderr)
    print(
        f"crs: EPSG:{epsg}  lon_normalized: {lon_normalized}  is_null: {surface.is_null}",
        file=sys.stderr,
    )
    for var in cfg.variables:
        s = stats[var.feature_name]
        print(
            f"  {var.request_name:28s} {var.netcdf_var:9s} "
            f"min={s['min']:8.3f} median={s['median']:8.3f} max={s['max']:8.3f}",
            file=sys.stderr,
        )
    print(f"wrote {args.out}", file=sys.stderr)

    if epsg != 4326 or not lon_normalized or surface.is_null:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
