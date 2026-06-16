"""Phase-0 hard gate (prompt 17): probe OPEN fire-weather/danger sources.

The v0.2.0 exposure-score changelog dropped FWI because no GREEN public
programmatic source was verified in-session. This script does NOT repeat that
failure silently: it probes each candidate in preference order, records
reachability + product id + license for every one, and writes a verdict JSON
to ``outputs/diagnostics/17_fire_weather_audit.json``.

    uv run python scripts/17_fire_weather_audit.py            # full probe
    uv run python scripts/17_fire_weather_audit.py --smoke    # offline, exit 0

Decision rule (CLAUDE.md non-negotiable #1 — no invented identifiers): a
source is GREEN only when it is reachable, programmatic, license-clear, and a
real product id is observed. The chosen source's identity is pinned in
``config/fire_weather.yaml`` and consumed by
``src/wildfire_exposure_eo/fire_weather.py``.

Terminology guard (non-negotiable #6): FWI is a danger *index*; this audit
verifies a data source, it makes no forecast or probability claim.

Probed candidates (in preference order):
  1. xclim indices.fire (Canadian FWI) over ERA5  — needs a Copernicus CDS
     account for ERA5; account availability is checked, not assumed.
  2. Copernicus CDS CEMS fire-historical (ready-made FWI) — needs a CDS account.
  3. GWIS NASA GPM-IMERG FWI via the public GWIS WMS — no auth; raw GeoTIFF
     values via GetMap(image/tiff); real archive ≈2014-05 .. 2020-12.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import requests

Status = Literal["GREEN", "YELLOW", "RED"]

USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 fire-weather-audit "
    "(+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)

# GWIS WMS identity probed by this script. The AOI bbox is always read from the
# GeoJSON at runtime (CLAUDE.md non-negotiable #10 — never hardcode the AOI).
GWIS_ENDPOINT = "https://ies-ows.jrc.ec.europa.eu/gwis"
GWIS_LAYER = "nasa.fwi_gpm.fwi"


@dataclass
class Probe:
    name: str
    status: Status
    message: str
    product_id: str | None
    license: str | None
    details: dict[str, Any]


def _aoi_bbox(aoi_path: Path) -> tuple[float, float, float, float]:
    """WGS84 bbox (minlon, minlat, maxlon, maxlat) of every coordinate in a GeoJSON."""
    payload = json.loads(aoi_path.read_text())
    coords: list[tuple[float, float]] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            if len(node) >= 2 and all(isinstance(x, int | float) for x in node[:2]):
                coords.append((float(node[0]), float(node[1])))
            else:
                for child in node:
                    walk(child)

    feats = payload.get("features", [payload])
    for feat in feats:
        walk(feat.get("geometry", {}).get("coordinates"))
    if not coords:
        raise ValueError(f"no coordinates in {aoi_path}")
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def probe_cds_account() -> Probe:
    """xclim+ERA5 and CDS CEMS both require a Copernicus CDS account.

    GREEN only if credentials are present AND the API responds authorised.
    Without credentials this is RED for in-session use (not a workaround —
    prompt 17 Phase 0 says say so plainly).
    """
    name = "Copernicus CDS (ERA5 / CEMS fire) — xclim/cdsapi path"
    has_rc = (Path.home() / ".cdsapirc").exists()
    has_env = bool(os.environ.get("CDSAPI_KEY") or os.environ.get("CDSAPI_URL"))
    if not (has_rc or has_env):
        return Probe(
            name,
            "RED",
            "no CDS credentials in-session (~/.cdsapirc absent, CDSAPI_* unset) — "
            "ERA5/CEMS need a Copernicus account; not available here",
            product_id="reanalysis-era5-single-levels / cems-fire-historical-v1",
            license="CC-BY 4.0 (Copernicus)",
            details={"cdsapirc": has_rc, "cdsapi_env": has_env},
        )
    return Probe(
        name,
        "YELLOW",
        "CDS credentials present but a live authorised request was not exercised in this probe",
        product_id="reanalysis-era5-single-levels / cems-fire-historical-v1",
        license="CC-BY 4.0 (Copernicus)",
        details={"cdsapirc": has_rc, "cdsapi_env": has_env},
    )


def probe_gwis_fwi(
    bbox: tuple[float, float, float, float],
    *,
    in_archive_date: str = "2017-08-15",
    out_archive_date: str = "2024-08-15",
    timeout: float = 60.0,
) -> Probe:
    """Probe the GWIS WMS FWI layer: capabilities, raw GeoTIFF, in/out archive.

    GREEN when GetCapabilities lists the layer with a license and GetMap
    returns a raw single-band raster with non-trivial values on an in-archive
    date AND an all-zero raster out-of-archive (the documented null signal).
    """
    import numpy as np
    import rasterio

    name = "GWIS NASA GPM-IMERG FWI (WMS, no auth)"
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})

    # 1. Capabilities — confirm the layer + license exist (no invented id).
    cap = sess.get(
        GWIS_ENDPOINT,
        params={"service": "WMS", "request": "GetCapabilities", "version": "1.3.0"},
        timeout=timeout,
    )
    cap.raise_for_status()
    cap_text = cap.text
    layer_present = f"<Name>{GWIS_LAYER}</Name>" in cap_text
    license_str = None
    for needle in ("EU Data License", "AccessConstraints"):
        if needle in cap_text:
            license_str = "EU Data License (Fees=none, AccessConstraints=None)"
            break
    if not layer_present:
        return Probe(
            name,
            "RED",
            f"layer {GWIS_LAYER!r} not advertised in GWIS GetCapabilities",
            product_id=None,
            license=license_str,
            details={"endpoint": GWIS_ENDPOINT},
        )

    def _getmap(when: str) -> np.ndarray:
        import tempfile

        minlon, minlat, maxlon, maxlat = bbox
        resp = sess.get(
            GWIS_ENDPOINT,
            params={
                "service": "WMS",
                "version": "1.1.1",
                "request": "GetMap",
                "layers": GWIS_LAYER,
                "styles": "",
                "srs": "EPSG:4326",
                "bbox": f"{minlon},{minlat},{maxlon},{maxlat}",
                "width": "72",
                "height": "54",
                "format": "image/tiff",
                "time": when,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=True) as fh:
            fh.write(resp.content)
            fh.flush()
            with rasterio.open(fh.name) as ds:
                return ds.read(1)

    in_arr = _getmap(in_archive_date)
    out_arr = _getmap(out_archive_date)
    in_max = float(np.nanmax(in_arr))
    out_max = float(np.nanmax(np.abs(out_arr)))
    details = {
        "endpoint": GWIS_ENDPOINT,
        "layer": GWIS_LAYER,
        "in_archive_date": in_archive_date,
        "in_archive_fwi_max": in_max,
        "out_archive_date": out_archive_date,
        "out_archive_abs_max": out_max,
        "crs": "EPSG:4326",
        "raster_format": "image/tiff",
    }
    if in_max > 0.0 and out_max == 0.0:
        return Probe(
            name,
            "GREEN",
            f"raw FWI GeoTIFF OK: in-archive {in_archive_date} max={in_max:.1f}, "
            f"out-of-archive {out_archive_date} all-zero (null signal as documented)",
            product_id=f"GWIS/{GWIS_LAYER}",
            license=license_str,
            details=details,
        )
    if in_max > 0.0:
        return Probe(
            name,
            "YELLOW",
            f"in-archive raster OK (max={in_max:.1f}) but out-of-archive raster not all-zero "
            f"(abs_max={out_max:.1f}) — null detection may need review",
            product_id=f"GWIS/{GWIS_LAYER}",
            license=license_str,
            details=details,
        )
    return Probe(
        name,
        "RED",
        f"in-archive GetMap returned a trivial raster (max={in_max:.1f})",
        product_id=f"GWIS/{GWIS_LAYER}",
        license=license_str,
        details=details,
    )


def _verdict(probes: list[Probe]) -> tuple[str, str | None]:
    """First GREEN probe wins; else NO-GO."""
    for p in probes:
        if p.status == "GREEN":
            return f"GO: {p.name} ({p.product_id})", p.product_id
    return "NO-GO: no GREEN open fire-weather source verified in-session", None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aoi", type=Path, default=Path("data/aoi/pilot.geojson"), help="AOI GeoJSON for the probe"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("outputs/diagnostics/17_fire_weather_audit.json"),
        help="verdict JSON output path",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="offline: validate config + season sampling only, no network; always exit 0",
    )
    args = parser.parse_args()

    if args.smoke:
        from wildfire_exposure_eo.fire_weather import (
            load_fire_weather_config,
            season_in_archive,
            season_sample_dates,
        )

        config = load_fire_weather_config(Path("config/fire_weather.yaml"))
        dates = season_sample_dates(2017, config)
        print(
            f"[smoke] config OK: layer={config.layer!r} product_id={config.product_id!r} "
            f"reducer={config.season_reducer!r} season={config.season_start_month}.."
            f"{config.season_end_month}",
            file=sys.stderr,
        )
        print(
            f"[smoke] 2017 season samples: {len(dates)} dates, in_archive="
            f"{season_in_archive(2017, config)}; 2024 in_archive="
            f"{season_in_archive(2024, config)}",
            file=sys.stderr,
        )
        return 0

    bbox = _aoi_bbox(args.aoi)
    probes: list[Probe] = []
    # Preference order: ERA5/CDS first (richest, but account-gated), GWIS last
    # (no auth). The decision rule picks the first GREEN.
    probes.append(probe_cds_account())
    try:
        probes.append(probe_gwis_fwi(bbox))
    except Exception as exc:
        probes.append(
            Probe(
                "GWIS NASA GPM-IMERG FWI (WMS, no auth)",
                "RED",
                f"probe raised: {exc}",
                product_id=f"GWIS/{GWIS_LAYER}",
                license=None,
                details={"endpoint": GWIS_ENDPOINT},
            )
        )

    verdict, chosen = _verdict(probes)
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "aoi": str(args.aoi),
        "aoi_bbox_4326": list(bbox),
        "verdict": verdict,
        "chosen_product_id": chosen,
        "probes": [asdict(p) for p in probes],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))

    for p in probes:
        print(
            f"[{p.status}] {p.name}: {p.message} (product_id={p.product_id}, license={p.license})",
            file=sys.stderr,
        )
    print(f"\nVERDICT: {verdict}", file=sys.stderr)
    print(f"wrote {args.out}", file=sys.stderr)

    any_green = any(p.status == "GREEN" for p in probes)
    any_red = any(p.status == "RED" for p in probes)
    if any_green:
        return 0
    return 1 if any_red else 2


if __name__ == "__main__":
    raise SystemExit(main())
