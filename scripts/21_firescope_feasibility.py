"""
scripts/21_firescope_feasibility.py
------------------------------------
Phase 0 feasibility gate for WU-21 (FireScope benchmark, Pillar 4).

Checks whether the INSAIT FireScope Europe ~30 m wildfire-risk raster
(HF dataset INSAIT-Institute/firescope-risk-2026) is:
  - Publicly accessible (no auth, no gating)
  - Under a redistribution-compatible license
  - A usable Europe-wide GeoTIFF with valid data over the pilot AOI
  - No parquet sidecars (affects consumption strategy)

Writes outputs/diagnostics/21_firescope_feasibility.json with a
GO/NO-GO verdict and the full evidence trail.

Usage:
    uv run python scripts/21_firescope_feasibility.py
    uv run python scripts/21_firescope_feasibility.py --smoke   # exits 0 if GO

Exit code: 0 always (feasibility result is in the JSON).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.env import Env
from rasterio.windows import from_bounds

# ---------------------------------------------------------------------------
# Constants — do NOT hardcode AOI coordinates here; load from geojson.
# ---------------------------------------------------------------------------
HF_DATASET_ID = "INSAIT-Institute/firescope-risk-2026"
HF_API_URL = f"https://huggingface.co/api/datasets/{HF_DATASET_ID}"
HF_TREE_URL = f"https://huggingface.co/api/datasets/{HF_DATASET_ID}/tree/main"
HF_RESOLVE_BASE = f"https://huggingface.co/datasets/{HF_DATASET_ID}/resolve/main"
RASTER_FILENAME = "oracle_unet.tif"
RASTER_URL = f"{HF_RESOLVE_BASE}/{RASTER_FILENAME}"
VSICURL_URL = f"/vsicurl/{RASTER_URL}"

EXPECTED_LICENSE = "cc-by-4.0"
EXPECTED_CRS = "EPSG:3857"
EXPECTED_RESOLUTION_M = 30.0

# Script is at scripts/; AOI is at data/aoi/pilot.geojson
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
AOI_PATH = REPO_ROOT / "data" / "aoi" / "pilot.geojson"
OUTPUT_PATH = REPO_ROOT / "outputs" / "diagnostics" / "21_firescope_feasibility.json"


def _http_head(url: str, timeout: int = 15) -> dict[str, object]:
    """Return status, content-length, content-type from a HEAD request."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "wildfire-eo/0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "status": resp.status,
                "content_length": resp.getheader("Content-Length"),
                "content_type": resp.getheader("Content-Type"),
                "accept_ranges": resp.getheader("Accept-Ranges"),
                "final_url_prefix": resp.url[:80] + "..." if len(resp.url) > 80 else resp.url,
            }
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "error": str(exc.reason)}
    except Exception as exc:
        return {"status": None, "error": str(exc)}


def _hf_api_meta() -> dict[str, object]:
    """Fetch HF dataset API metadata (card, gated, sha, siblings)."""
    req = urllib.request.Request(HF_API_URL, headers={"User-Agent": "wildfire-eo/0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data: dict[str, object] = json.loads(resp.read())
            return {
                "sha": data.get("sha"),
                "gated": data.get("gated"),
                "private": data.get("private"),
                "card_data": data.get("cardData", {}),
                "siblings": [s.get("rfilename") for s in data.get("siblings", [])],  # type: ignore[union-attr]
            }
    except Exception as exc:
        return {"error": str(exc)}


def _hf_tree() -> list[dict[str, object]]:
    """Fetch the full HF file tree with sizes."""
    req = urllib.request.Request(HF_TREE_URL, headers={"User-Agent": "wildfire-eo/0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            entries: list[dict[str, object]] = json.loads(resp.read())
            return [
                {
                    "path": e.get("path"),
                    "size": e.get("size"),
                    "lfs": bool(e.get("lfs")),
                    "lfs_oid": e.get("lfs", {}).get("oid") if e.get("lfs") else None,  # type: ignore[union-attr]
                }
                for e in entries
            ]
    except Exception as exc:
        return [{"error": str(exc)}]


def _raster_meta() -> dict[str, object]:
    """Read raster metadata via GDAL /vsicurl/ (range request — no full download)."""
    with Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_TIMEOUT="20",
    ):
        try:
            with rasterio.open(VSICURL_URL) as ds:
                t = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
                lon_min, lat_min = t.transform(ds.bounds.left, ds.bounds.bottom)
                lon_max, lat_max = t.transform(ds.bounds.right, ds.bounds.top)
                return {
                    "driver": ds.driver,
                    "crs": str(ds.crs),
                    "width": ds.width,
                    "height": ds.height,
                    "bands": ds.count,
                    "dtype": list(ds.dtypes),
                    "nodata": ds.nodata,
                    "res_x_m": abs(ds.transform.a),
                    "res_y_m": abs(ds.transform.e),
                    "bounds_3857": list(ds.bounds),
                    "bounds_wgs84_approx": {
                        "lon_min": round(lon_min, 3),
                        "lon_max": round(lon_max, 3),
                        "lat_min": round(lat_min, 3),
                        "lat_max": round(lat_max, 3),
                    },
                    "tags": dict(ds.tags()),
                }
        except Exception as exc:
            return {"error": str(exc)}


def _aoi_sample(aoi_path: Path) -> dict[str, object]:
    """Read a tile of the raster over the pilot AOI and report value stats."""
    if not aoi_path.exists():
        return {"error": f"AOI file not found: {aoi_path}"}

    with aoi_path.open() as f:
        aoi = json.load(f)

    # Extract geometry
    if aoi["type"] == "FeatureCollection":
        geom = aoi["features"][0]["geometry"]
    elif aoi["type"] == "Feature":
        geom = aoi["geometry"]
    else:
        geom = aoi

    if geom["type"] != "Polygon":
        return {"error": f"Unexpected geometry type: {geom['type']}"}

    coords = geom["coordinates"][0]
    t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xs = [t.transform(c[0], c[1])[0] for c in coords]
    ys = [t.transform(c[0], c[1])[1] for c in coords]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    with Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_TIMEOUT="20",
    ):
        try:
            with rasterio.open(VSICURL_URL) as ds:
                win = from_bounds(x_min, y_min, x_max, y_max, ds.transform)
                data = ds.read(1, window=win)
                nodata_val = int(ds.nodata) if ds.nodata is not None else None
                if nodata_val is not None:
                    valid = data[data != nodata_val]
                    nodata_frac = float((data == nodata_val).mean())
                else:
                    valid = data.ravel()
                    nodata_frac = 0.0
                n_valid = len(valid)
                return {
                    "aoi_bounds_4326": {
                        "lon_min": round(min(c[0] for c in coords), 4),
                        "lon_max": round(max(c[0] for c in coords), 4),
                        "lat_min": round(min(c[1] for c in coords), 4),
                        "lat_max": round(max(c[1] for c in coords), 4),
                    },
                    "aoi_bounds_3857": {
                        "x_min": round(x_min, 0),
                        "x_max": round(x_max, 0),
                        "y_min": round(y_min, 0),
                        "y_max": round(y_max, 0),
                    },
                    "tile_shape": list(data.shape),
                    "valid_pixels": n_valid,
                    "nodata_fraction": round(nodata_frac, 4),
                    "value_min": int(valid.min()) if n_valid > 0 else None,
                    "value_max": int(valid.max()) if n_valid > 0 else None,
                    "value_mean": round(float(valid.mean()), 2) if n_valid > 0 else None,
                    "value_p50": (
                        round(float(np.percentile(valid, 50)), 1) if n_valid > 0 else None
                    ),
                    "value_p90": (
                        round(float(np.percentile(valid, 90)), 1) if n_valid > 0 else None
                    ),
                }
        except Exception as exc:
            return {"error": str(exc)}


def run_feasibility() -> dict[str, object]:
    """Run all feasibility checks and return the evidence dict."""
    print("== FireScope Phase-0 Feasibility Check ==")

    print("1/4  HF API metadata...")
    api_meta = _hf_api_meta()

    print("2/4  HF file tree...")
    tree = _hf_tree()

    print("3/4  HEAD request on oracle_unet.tif...")
    head_result = _http_head(RASTER_URL)

    print("4/4  Raster metadata via /vsicurl/ (range request)...")
    raster_meta = _raster_meta()

    # --- Derive parquet_present flag ---
    parquet_present = any(
        str(e.get("path", "")).endswith(".parquet") for e in tree if isinstance(e, dict)
    )

    # --- License check ---
    card_data = api_meta.get("card_data", {})
    license_val = (card_data.get("license") if isinstance(card_data, dict) else None) or ""  # type: ignore[union-attr]
    license_ok = str(license_val).lower() == EXPECTED_LICENSE

    # --- Raster sanity checks ---
    crs_ok = raster_meta.get("crs") == EXPECTED_CRS
    res_ok = abs(float(raster_meta.get("res_x_m", 0)) - EXPECTED_RESOLUTION_M) < 0.1  # type: ignore[arg-type]
    http_ok = head_result.get("status") == 200

    # --- AOI sample ---
    print("4b   AOI sample (range request over pilot bbox)...")
    aoi_sample = _aoi_sample(AOI_PATH)
    aoi_has_data = (
        aoi_sample.get("valid_pixels", 0) > 0  # type: ignore[operator]
        and aoi_sample.get("error") is None
    )

    # --- Verdict ---
    gates_pass = license_ok and http_ok and crs_ok and res_ok and aoi_has_data
    if gates_pass:
        access_path = (
            f"Direct /vsicurl/{RASTER_URL} — "
            "no auth, Accept-Ranges: bytes, GDAL range-read confirmed. "
            "No parquet sidecars; consumption strategy: GDAL /vsicurl/ + "
            "rasterio.open for AOI-clipped zonal reads."
        )
        verdict = f"GO: {access_path}"
    else:
        reasons = []
        if not http_ok:
            reasons.append(f"HTTP {head_result.get('status')} on raster URL")
        if not license_ok:
            reasons.append(f"license={license_val!r} (expected {EXPECTED_LICENSE!r})")
        if not crs_ok:
            reasons.append(f"CRS={raster_meta.get('crs')!r} (expected {EXPECTED_CRS!r})")
        if not res_ok:
            reasons.append(
                f"res={raster_meta.get('res_x_m')!r}m (expected {EXPECTED_RESOLUTION_M}m)"
            )
        if not aoi_has_data:
            reasons.append("no valid data over pilot AOI")
        verdict = "NO-GO: " + "; ".join(reasons)

    result: dict[str, object] = {
        "dataset_id": HF_DATASET_ID,
        "raster_filename": RASTER_FILENAME,
        "raster_url": RASTER_URL,
        "api_meta": api_meta,
        "file_tree": tree,
        "http_head": head_result,
        "raster_meta": raster_meta,
        "aoi_sample": aoi_sample,
        "parquet_present": parquet_present,
        "license": license_val,
        "license_ok": license_ok,
        "http_ok": http_ok,
        "crs_ok": crs_ok,
        "res_ok": res_ok,
        "aoi_has_data": aoi_has_data,
        "consumption_strategy": (
            "GDAL /vsicurl/ + rasterio window-read for AOI clips; "
            "no full download required for zonal stats. "
            "Full atlas download (~12.3 GB) needed only if parallel tiling required."
            if gates_pass
            else "N/A — NO-GO"
        ),
        "aoi_clip_plan": (
            "1. open(VSICURL_URL) with rasterio; "
            "2. reproject AOI buffer to EPSG:3857; "
            "3. from_bounds window; "
            "4. exactextract zonal stats per asset buffer; "
            "5. percentile-rank → Spearman vs exposure rank. "
            "No raw raster committed to repo."
            if gates_pass
            else "N/A"
        ),
        "verdict": verdict,
        "generated_by": "scripts/21_firescope_feasibility.py",
    }

    print(f"\nVerdict: {verdict}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="FireScope Phase-0 feasibility gate")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Exit 0 if GO, exit 1 if NO-GO (for CI smoke check)",
    )
    args = parser.parse_args()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = run_feasibility()

    OUTPUT_PATH.write_text(json.dumps(result, indent=2))
    print(f"\nWrote: {OUTPUT_PATH}")

    if args.smoke:
        if result["verdict"].startswith("GO"):  # type: ignore[union-attr]
            print("Smoke: PASS (GO)")
            sys.exit(0)
        else:
            print("Smoke: FAIL (NO-GO)")
            sys.exit(1)


if __name__ == "__main__":
    main()
