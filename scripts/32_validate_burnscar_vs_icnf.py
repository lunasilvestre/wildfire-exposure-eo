"""Validate the de-gridded burn-scar COGs against temporally-matched ICNF truth.

Why this exists
---------------
The geobrowser's "ICNF burns for this AOI" layer is 35 YEARS of cumulative
burned area (1990-2025), covering 43-83% of each AOI. The Prithvi burn-scar
layer can only detect scars still spectrally visible in recent imagery (the
last fire seasons). Eyeballing the two side by side reads as "the model grossly
underestimates the burns" — but that is a temporal-scope mismatch, not a model
failure: you cannot detect a 1995 burn that has fully revegetated.

This script makes the comparison honest and quantitative. For each AOI it:

* quantifies the painted-layer "wash" at several score thresholds;
* rasterises ICNF perimeters filtered to vintage cuts (all / 2017+ / 2023+ /
  2024+) and computes precision, recall and LIFT (precision / truth-coverage;
  lift ~= 1.0 means hits land on burned ground only at the base rate, i.e. no
  spatial skill) of the painted detections against each cut;
* renders a 4-panel overlay (raw field / painted >=0.40 / >=0.40+recent ICNF /
  >=0.60+recent ICNF) for eyes-on validation.

Findings (run 2026-06-23) that drove the geobrowser changes:

* Against CUMULATIVE ICNF, lift ~= 1.0-1.3 everywhere — but that is the
  inflated-precision-against-a-huge-base-rate artifact, not skill.
* Against RECENT (2023-25) ICNF, Pedrogao/Serra/Peneda show real skill
  (lift 1.5-2.8, recall 0.6-0.8); raising the paint floor 0.40 -> 0.60 cuts
  coverage from 29-43% to 9-23% and roughly doubles precision.
* Monchique is a diffuse wash (precision vs recent ICNF ~0.13 even at >=0.60;
  genuine 2024-25 burn ~64 ha) -> withheld from the mosaic alongside the pilot.

Inputs (not committed; fetch before running):
* burn-scar de-gridded EPSG:4326 COGs in ``outputs/cogs/`` (the inference run).
* ICNF per-AOI GeoJSON from Cloudflare R2
  (``r2:wildfire-exposure-eo/icnf_burns_<aoi>_<run>.geojson``), placed at
  ``ICNF_DIR/icnf_<aoi>.geojson``.

Usage:
    uv run python scripts/32_validate_burnscar_vs_icnf.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.plot import plotting_extent

_ROOT = Path(__file__).resolve().parents[1]
_COG_DIR = _ROOT / "outputs" / "cogs"
_ICNF_DIR = Path(os.environ.get("ICNF_DIR", "/tmp"))
_OUT_DIR = Path(os.environ.get("BSVAL_OUT", "/tmp"))

#: De-gridded EPSG:4326 burn-scar COGs per AOI (the inference run; non-negotiable
#: #1: exact published filenames, never fabricated).
_COGS: dict[str, str] = {
    "pedrogao_grande": "burn_scar_pedrogao_grande_degrid_20260620T185302Z.tif",
    "serra_da_estrela": "burn_scar_serra_da_estrela_degrid_20260622T123744Z.tif",
    "peneda_geres": "burn_scar_peneda_geres_degrid_20260622T131029Z.tif",
    "monchique": "burn_scar_monchique_degrid_20260622T133547Z.tif",
    "pilot": "burn_scar_pilot_degrid_20260622T135327Z.tif",
}
_YR = "vintage_year"
_DISP = 0.40  # legacy alpha-ramp floor
_HI = 0.60  # validated alpha-ramp floor (suppresses the wash)
_VINTAGE_CUTS = {"all_1990": 1990, "y2017": 2017, "y2023": 2023, "y2024": 2024}


def _frac(arr: np.ndarray, nvalid: int, thr: float) -> float:
    return round(float(np.nansum(arr >= thr) / nvalid), 4)


def validate() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for aoi, cog_name in _COGS.items():
        cog = _COG_DIR / cog_name
        with rasterio.open(cog) as d:
            a = d.read(1).astype("float32")
            nd = d.nodata
            ext = plotting_extent(d)
            crs = d.crs  # explicit CRS (non-negotiable #2)
            transform = d.transform
            shape = (d.height, d.width)
        valid = np.isfinite(a) & (a != nd)
        a = np.where(valid, a, np.nan)
        nvalid = int(valid.sum())

        icnf_path = _ICNF_DIR / f"icnf_{aoi}.geojson"
        g = gpd.read_file(icnf_path).to_crs(crs) if icnf_path.exists() else None

        rec: dict = {
            "valid_px": nvalid,
            "wash_ge_010": _frac(a, nvalid, 0.10),
            "disp_ge_040": _frac(a, nvalid, _DISP),
            "ge_055": _frac(a, nvalid, 0.55),
            "ge_060": _frac(a, nvalid, _HI),
            "ge_080": _frac(a, nvalid, 0.80),
        }

        if g is not None:
            det = valid & (a >= _DISP)
            det_hi = valid & (a >= _HI)
            ndet, ndet_hi = int(det.sum()), int(det_hi.sum())
            rec["icnf"] = {}
            for name, yr in _VINTAGE_CUTS.items():
                sub = g[g[_YR] >= yr]
                if len(sub):
                    m = rasterize(
                        ((geom, 1) for geom in sub.geometry),
                        out_shape=shape,
                        transform=transform,
                        fill=0,
                        dtype="uint8",
                    )
                    m = (m == 1) & valid
                else:
                    m = np.zeros(shape, dtype=bool)
                base = float(m.sum() / nvalid)
                prec = float((det & m).sum() / ndet) if ndet else 0.0
                prec_hi = float((det_hi & m).sum() / ndet_hi) if ndet_hi else 0.0
                recall = float((det & m).sum() / m.sum()) if m.sum() else 0.0
                rec["icnf"][name] = {
                    "truth_cover": round(base, 4),
                    "precision@0.40": round(prec, 4),
                    "precision@0.60": round(prec_hi, 4),
                    "recall@0.40": round(recall, 4),
                    "lift@0.40": round(prec / base, 2) if base else 0.0,
                }
        out[aoi] = rec

        _render(aoi, a, ext, g, rec)
    return out


def _render(aoi: str, a: np.ndarray, ext, g, rec: dict) -> None:
    _fig, ax = plt.subplots(1, 4, figsize=(26, 8))
    ax[0].imshow(a, extent=ext, cmap="viridis", vmin=0, vmax=0.6)
    ax[0].set_title(f"{aoi}: RAW field (>=0.10 = {rec['wash_ge_010'] * 100:.0f}%)")
    disp = np.where(a >= _DISP, a, np.nan)
    ax[1].imshow(disp, extent=ext, cmap="YlOrRd", vmin=_DISP, vmax=0.8)
    ax[1].set_title(f"painted >=0.40 ({rec['disp_ge_040'] * 100:.0f}% of AOI)")
    ax[2].imshow(disp, extent=ext, cmap="YlOrRd", vmin=_DISP, vmax=0.8)
    hi = np.where(a >= _HI, a, np.nan)
    ax[3].imshow(hi, extent=ext, cmap="YlOrRd", vmin=_HI, vmax=0.9)
    ax[3].set_title(f">=0.60 only ({rec['ge_060'] * 100:.0f}% of AOI)")
    if g is not None:
        recent = g[g[_YR] >= 2017]
        recent.boundary.plot(ax=ax[2], color="blue", lw=0.6)
        recent.boundary.plot(ax=ax[3], color="blue", lw=0.6)
        ax[2].set_title("painted >=0.40 + ICNF 2017-25 (blue)")
        ax[3].set_title(">=0.60 + ICNF 2017-25 (blue)")
    for x in ax:
        x.set_xlim(ext[0], ext[1])
        x.set_ylim(ext[2], ext[3])
        x.axis("off")
    plt.tight_layout()
    dest = _OUT_DIR / f"bsval_{aoi}.png"
    plt.savefig(dest, dpi=88)
    plt.close()
    print(f"rendered {dest}")


if __name__ == "__main__":
    metrics = validate()
    dest = _OUT_DIR / "bsval.json"
    dest.write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))
    print(f"\nmetrics -> {dest}")
