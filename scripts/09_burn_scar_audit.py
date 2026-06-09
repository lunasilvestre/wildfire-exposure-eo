"""Crosscheck a burn-scar inference COG against ICNF Áreas Ardidas polygons.

Prompt 09 verification step 4: where the COG's trailing S2 window overlaps
the latest ICNF vintage, rank-correlate the per-pixel burn-scar inference
probability with the rasterized ICNF burned mask and report top-decile
capture. Every number in `docs/burn_scar_audit.md` is produced by this
script (CLAUDE.md fact-checking checklist):

    uv run python scripts/09_burn_scar_audit.py \
        --cog outputs/cogs/burn_scar_<run_id>.tif --out docs/burn_scar_audit.md

The COG is self-describing: the overlap window comes from the provenance
tags embedded by `write_burn_scar_cog`, never from CLI guesses. Spearman is
computed with numpy average ranks (no scipy — it is not a pinned dependency).

Terminology guard (CLAUDE.md): both rasters describe burn *scars* of fires
that already happened; the correlation says how well the model's relative
scores rank known burned area, nothing more.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.features import rasterize
from shapely.geometry import shape

ICNF_AREAS_ARDIDAS_MAPSERVER = (
    "https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer"
)
# Layer-id mapping confirmed against the live MapServer in scripts/00_icnf_fetch.sh
# and src/wildfire_exposure_eo/audit.py (ICNF_RECENT_LAYERS).
ICNF_2025_LAYER_ID = 20
USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 burn-scar-audit "
    "(+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)
PAGE_SIZE = 500


def average_ranks(values: np.ndarray) -> np.ndarray:
    """Average (mid) ranks with ties, matching scipy.stats.rankdata('average')."""
    order = np.argsort(values, kind="mergesort")
    _, inverse, counts = np.unique(values[order], return_inverse=True, return_counts=True)
    upper = np.cumsum(counts).astype(np.float64)
    lower = upper - counts + 1
    avg = (lower + upper) / 2.0
    ranks = np.empty(values.size, dtype=np.float64)
    ranks[order] = avg[inverse]
    return ranks


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation = Pearson on average ranks."""
    rx = average_ranks(x)
    ry = average_ranks(y)
    rx -= rx.mean()
    ry -= ry.mean()
    denom = float(np.sqrt((rx**2).sum() * (ry**2).sum()))
    if denom == 0.0:
        raise ValueError("degenerate ranks: one of the inputs is constant")
    return float((rx * ry).sum() / denom)


def fetch_icnf_features(
    bbox: tuple[float, float, float, float],
    *,
    layer_id: int,
) -> list[dict]:
    """All ICNF features for `layer_id` intersecting `bbox` (WGS84), paged."""
    url = f"{ICNF_AREAS_ARDIDAS_MAPSERVER}/{layer_id}/query"
    features: list[dict] = []
    offset = 0
    while True:
        resp = requests.get(
            url,
            params={
                "where": "1=1",
                "geometry": ",".join(str(v) for v in bbox),
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "OBJECTID,Cod_SGIF,Ano,DH_Inicio,AreaHaPoly",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": str(offset),
                "resultRecordCount": str(PAGE_SIZE),
                "f": "geojson",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
        page = payload.get("features", [])
        features.extend(page)
        if len(page) < PAGE_SIZE:
            return features
        offset += PAGE_SIZE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cog", type=Path, required=True, help="burn-scar inference COG")
    parser.add_argument(
        "--layer-id",
        type=int,
        default=ICNF_2025_LAYER_ID,
        help="ICNF MapServer layer id of the latest vintage (default: 2025)",
    )
    parser.add_argument(
        "--vintage-end",
        default="2025-12-31",
        help="last day covered by the ICNF vintage (ISO date)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the markdown here")
    args = parser.parse_args()

    with rasterio.open(args.cog) as src:
        prob = src.read(1)
        nodata = src.nodata
        transform = src.transform
        crs = src.crs
        bounds = src.bounds
        tags = src.tags()
    assert crs is not None and crs.to_epsg() == 4326, "burn-scar COG must be EPSG:4326"
    provenance = json.loads(tags["WILDFIRE_EXPOSURE_EO_PROVENANCE"])

    window_start = datetime.fromisoformat(provenance["window_start"]).replace(tzinfo=UTC)
    window_end = datetime.fromisoformat(provenance["window_end"]).replace(tzinfo=UTC)
    vintage_end = datetime.fromisoformat(args.vintage_end).replace(tzinfo=UTC)
    overlap_end = min(window_end, vintage_end)
    if overlap_end <= window_start:
        print("no overlap between the COG window and the ICNF vintage", file=sys.stderr)
        return 2

    bbox = (bounds.left, bounds.bottom, bounds.right, bounds.top)
    features = fetch_icnf_features(bbox, layer_id=args.layer_id)
    in_window = []
    for feat in features:
        raw = feat.get("properties", {}).get("DH_Inicio")
        if raw is None:
            continue
        started = datetime.fromtimestamp(raw / 1000.0, tz=UTC)
        if window_start <= started <= overlap_end:
            in_window.append(feat)
    print(
        f"[icnf] layer {args.layer_id}: {len(features)} feature(s) intersect the COG bbox, "
        f"{len(in_window)} start within {window_start.date()}..{overlap_end.date()}",
        file=sys.stderr,
    )

    valid = prob != nodata if nodata is not None else np.isfinite(prob)
    if in_window:
        mask = rasterize(
            [(shape(f["geometry"]), 1) for f in in_window],
            out_shape=prob.shape,
            transform=transform,
            fill=0,
            dtype="uint8",
        )
        assert mask is not None  # rasterize only returns None when out= is passed
        burned = mask.astype(bool)
    else:
        burned = np.zeros(prob.shape, dtype=bool)

    p = prob[valid].astype(np.float64)
    b = burned[valid].astype(np.float64)
    n_valid = int(p.size)
    n_burned = int(b.sum())
    base_rate = n_burned / n_valid if n_valid else float("nan")

    if n_burned == 0:
        rho = None
        lift = None
    else:
        rho = spearman(p, b)
        decile_cut = np.quantile(p, 0.9)
        top = p >= decile_cut
        top_rate = float(b[top].mean())
        lift = top_rate / base_rate if base_rate > 0 else float("nan")

    run_id = provenance["run_id"]
    commit = provenance["code_commit_sha"]
    lines = [
        "# Burn-scar inference vs. ICNF Áreas Ardidas — overlap crosscheck",
        "",
        f"<!-- generated by: scripts/09_burn_scar_audit.py at {commit} -->",
        "",
        f"COG: `{args.cog}` (run `{run_id}`, model `{provenance['model_id']}` @ "
        f"`{provenance['hf_revision_sha'][:8]}`).",
        f"Overlap window: **{window_start.date()} .. {overlap_end.date()}** "
        f"(COG trailing window ∩ ICNF vintage, MapServer layer {args.layer_id}).",
        "",
        "| metric | value |",
        "|---|---|",
        f"| valid pixels | {n_valid} |",
        f"| ICNF polygons in overlap window | {len(in_window)} |",
        f"| burned pixels (rasterized) | {n_burned} |",
        f"| burned base rate | {base_rate:.5f} |",
    ]
    if rho is None:
        lines += [
            "| Spearman ρ (probability vs. burned mask) | n/a — no ICNF burn in window |",
            "| top-decile lift | n/a — no ICNF burn in window |",
            "",
            "No ICNF-recorded fire started inside the AOI during the overlap window, "
            "so the rank crosscheck is undefined for this run. The comparison must be "
            "re-run after the next ICNF vintage lands, or on an AOI/window with "
            "recorded burns.",
        ]
    else:
        lines += [
            f"| Spearman ρ (probability vs. burned mask) | {rho:.4f} |",
            f"| top-decile lift (burned rate in top 10% ÷ base rate) | {lift:.2f}× |",
        ]
    lines += [
        "",
        "Both rasters describe burn scars of fires that already happened; the",
        "correlation measures how well the model's *relative* scores rank known",
        "burned area within this AOI and window. It is not a calibration claim",
        "and not a forecast skill claim.",
        "",
    ]
    text = "\n".join(lines)
    if args.out:
        args.out.write_text(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
