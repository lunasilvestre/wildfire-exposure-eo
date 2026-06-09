"""Domain-shift sanity check: Prithvi burn-scar inference vs. known ICNF perimeters.

Close-out plan, methodological caveat #2: the BurnScars checkpoint was
trained on US HLS fire scenes; Portuguese eucalyptus/pinus mosaics are
out-of-domain. Before the WU-1 pilot COG is trusted, run inference over 2–3
known ICNF burn perimeters from the most recent published vintage and report
IoU/agreement in the session log. Every reported number comes from this
script (CLAUDE.md fact-checking checklist):

    uv run python scripts/09_burn_scar_sanity.py --objectids 112769 111188 111504

For each ICNF polygon the inference AOI is the perimeter bbox plus a ~1 km
margin (so false positives outside the perimeter count against the score)
and the S2 window runs from ~6 weeks before fire start to ~6 weeks after
fire end. The probability raster is binarised at the configured threshold.

Terminology guard (CLAUDE.md): this measures agreement between the model's
burn-scar inference and a mapped perimeter of a fire that already happened.
It is not calibration, not forecast skill.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import requests
from rasterio.features import rasterize
from shapely.geometry import box, shape

from wildfire_exposure_eo import burn_scar
from wildfire_exposure_eo.stac import code_commit_sha

ICNF_AREAS_ARDIDAS_MAPSERVER = (
    "https://sigservices.icnf.pt/server/rest/services/BDG/areas_ardidas/MapServer"
)
ICNF_2025_LAYER_ID = 20
USER_AGENT = (
    "wildfire-exposure-eo/0.0.1 burn-scar-sanity "
    "(+https://github.com/lunasilvestre/wildfire-exposure-eo)"
)
MARGIN_DEG = 0.01  # ~1 km bbox margin so false positives outside count
WINDOW_PAD_DAYS = 45


def fetch_perimeter(objectid: int, *, layer_id: int) -> dict:
    """One ICNF feature (GeoJSON, WGS84) by OBJECTID."""
    resp = requests.get(
        f"{ICNF_AREAS_ARDIDAS_MAPSERVER}/{layer_id}/query",
        params={
            "where": f"OBJECTID={objectid}",
            "outFields": "OBJECTID,Cod_SGIF,Ano,DH_Inicio,DH_Fim,AreaHaPoly,PI_Conc",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if len(features) != 1:
        raise ValueError(f"OBJECTID {objectid}: expected 1 feature, got {len(features)}")
    return features[0]


def check_one(feature: dict, handle: burn_scar.ModelHandle, cfg: Any) -> dict:
    """Run inference around one perimeter; return agreement metrics."""
    props = feature["properties"]
    perimeter = shape(feature["geometry"])
    started = datetime.fromtimestamp(props["DH_Inicio"] / 1000.0, tz=UTC)
    ended = datetime.fromtimestamp(props["DH_Fim"] / 1000.0, tz=UTC)

    window_end = (ended + timedelta(days=WINDOW_PAD_DAYS)).date()
    window_start = (started - timedelta(days=WINDOW_PAD_DAYS)).date()
    window_months = max(1, round((window_end - window_start).days / 30.44 + 0.5))

    aoi = box(*perimeter.buffer(MARGIN_DEG).bounds)
    items = burn_scar.query_recent_s2(
        aoi,
        window_months,
        max_cloud_cover=cfg.inference.s2_max_cloud_cover,
        window_end=window_end,
    )
    if not items:
        raise ValueError(f"no S2 items for OBJECTID {props['OBJECTID']}")

    da = burn_scar.infer_burn_probability(
        items,
        handle,
        aoi,
        s2_assets=cfg.inference.s2_assets,
        scl_mask_classes=cfg.inference.scl_mask_classes,
        tile_size=cfg.inference.tile_size,
        tile_stride=cfg.inference.tile_stride,
    )

    prob = da.values
    valid = np.isfinite(prob)
    transform = da.rio.transform()
    mask = rasterize(
        [(feature["geometry"], 1)],
        out_shape=prob.shape,
        transform=transform,
        fill=0,
        dtype="uint8",
    )
    assert mask is not None
    burned = mask.astype(bool)

    threshold = cfg.inference.binarisation_threshold
    pred = valid & (prob >= threshold)
    truth = valid & burned
    inter = int((pred & truth).sum())
    union = int((pred | truth).sum())
    iou = inter / union if union else float("nan")
    precision = inter / int(pred.sum()) if pred.sum() else float("nan")
    recall = inter / int(truth.sum()) if truth.sum() else float("nan")
    inside = float(prob[valid & burned].mean()) if (valid & burned).any() else float("nan")
    outside = float(prob[valid & ~burned].mean()) if (valid & ~burned).any() else float("nan")

    return {
        "objectid": props["OBJECTID"],
        "concelho": props.get("PI_Conc", "?"),
        "area_ha": round(float(props["AreaHaPoly"])),
        "fire": f"{started.date()}..{ended.date()}",
        "window": f"{window_start}..{window_end}",
        "scenes": len(items),
        "valid_px": int(valid.sum()),
        "threshold": threshold,
        "iou": iou,
        "precision": precision,
        "recall": recall,
        "mean_prob_inside": inside,
        "mean_prob_outside": outside,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--objectids",
        type=int,
        nargs="+",
        required=True,
        help="ICNF OBJECTIDs from the vintage layer (e.g. 112769 111188 111504)",
    )
    parser.add_argument("--layer-id", type=int, default=ICNF_2025_LAYER_ID)
    parser.add_argument("--device", default=None, help="torch device (default: auto)")
    args = parser.parse_args()

    cfg = burn_scar.load_burn_scar_config()
    handle = burn_scar.resolve_prithvi_burn_scar_model(cfg, device=args.device)

    rows = []
    for objectid in args.objectids:
        feature = fetch_perimeter(objectid, layer_id=args.layer_id)
        print(f"[sanity] OBJECTID {objectid} ...", file=sys.stderr)
        rows.append(check_one(feature, handle, cfg))

    commit = code_commit_sha(cwd=Path.cwd())
    print(f"<!-- generated by: scripts/09_burn_scar_sanity.py at {commit} -->")
    print()
    print(
        "| OBJECTID | concelho | area (ha) | fire | S2 window | scenes "
        f"| IoU@{cfg.inference.binarisation_threshold} | precision | recall | mean prob in/out |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['objectid']} | {r['concelho']} | {r['area_ha']} | {r['fire']} "
            f"| {r['window']} | {r['scenes']} | {r['iou']:.3f} | {r['precision']:.3f} "
            f"| {r['recall']:.3f} | {r['mean_prob_inside']:.3f} / {r['mean_prob_outside']:.3f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
