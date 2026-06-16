"""Phase 0 topology audit for WU-19 (network/topology exposure, pillar 1).

Before building the graph, measure how much connectivity OSM actually gives us on
the AOI — OSM power topology in rural Portugal is incomplete, and the topology
feature's reliability caveat depends on this honest coverage measurement:

  * how many power nodes (substations/transformers/towers) connect to ≥1 line?
  * how many lines' endpoints snap to a node vs. float free?
  * what fraction of ``power=line`` carry an explicit ``voltage`` tag?
  * water: how many treatment plants link to ≥1 reservoir under the proxy?

The numbers are produced ONLY by this script (CLAUDE.md fact-checking checklist):

    uv run python scripts/19_topology_audit.py \
        --osm-parquet outputs/parquet/osm_assets_<run>.parquet \
        --out outputs/diagnostics/19_topology_audit.json

    uv run python scripts/19_topology_audit.py --smoke   # exits 0 (fetch smoke AOI)

If ``--osm-parquet`` is omitted the script fetches OSM over the AOI (default the
pilot AOI; ``--smoke`` switches to the 1 km smoke tile and asserts the graph
builds even on a sparse network). The AOI is always read from a geojson — no
hardcoded coordinates (non-negotiable #10). CRS is explicit throughout (#2).

Terminology guard (CLAUDE.md non-negotiable #6): connectivity feeds a *relative*
exposure rank, never a probability or forecast. Edges are INFERRED from a
documented snapping/proximity heuristic, never presented as OSM ground truth (#1).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Repo-root import shim so the script runs from anywhere (matches scripts/09_*, 11_*).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wildfire_exposure_eo import topology as topo
from wildfire_exposure_eo.features import ASSET_CRS, load_assets
from wildfire_exposure_eo.stac import code_commit_sha

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PILOT_AOI = REPO_ROOT / "data" / "aoi" / "pilot.geojson"
SMOKE_AOI = REPO_ROOT / "data" / "aoi" / "smoke.geojson"
TAXONOMY = REPO_ROOT / "data" / "taxonomy" / "critical_infrastructure.yaml"
DEFAULT_OUT = REPO_ROOT / "outputs" / "diagnostics" / "19_topology_audit.json"


def _voltage_coverage(assets: Any) -> dict[str, Any]:
    """Fraction of power-line assets carrying an explicit ``voltage`` tag.

    The line classes are distinguished by the taxonomy's voltage regex at fetch
    time, so the *distribution* class also contains untagged lines; this measures
    how reliable that split is. ``tags`` is a JSON string per row (see osm.py).
    """
    lines = assets[assets["asset_class"].isin(topo.POWER_LINE_CLASSES)]
    n_lines = len(lines)
    if n_lines == 0:
        return {"n_lines": 0, "n_with_voltage": 0, "voltage_tag_fraction": None}
    n_voltage = 0
    for raw in lines["tags"]:
        try:
            tags = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError):
            tags = {}
        if tags.get("voltage"):
            n_voltage += 1
    return {
        "n_lines": n_lines,
        "n_with_voltage": n_voltage,
        "voltage_tag_fraction": round(n_voltage / n_lines, 6),
    }


def _line_snap_coverage(assets: Any, *, snap_tolerance_m: float) -> dict[str, Any]:
    """How many power-line endpoints snap to a node vs. float free.

    Reuses the production snapping helper so the audit reflects what the graph
    builder will actually do. Geometry is reprojected to EPSG:32629 (#2).
    """
    nodes = topo._ordered_nodes(assets, topo.POWER_NODE_CLASSES)
    node_xy = topo._node_xy(nodes)
    lines = topo._ordered_nodes(assets, topo.POWER_LINE_CLASSES)
    n_endpoints = 0
    n_snapped = 0
    n_lines_both_ends = 0
    n_lines_floating = 0
    for geom in lines.geometry:
        if geom is None or geom.is_empty:
            continue
        ends = topo._endpoints_xy(geom)
        snapped = [topo._nearest_node(e, node_xy, snap_tolerance_m) is not None for e in ends]
        n_endpoints += len(ends)
        n_snapped += sum(snapped)
        if ends and all(snapped):
            n_lines_both_ends += 1
        if not any(snapped):
            n_lines_floating += 1
    return {
        "n_line_endpoints": n_endpoints,
        "n_endpoints_snapped": n_snapped,
        "endpoint_snap_fraction": round(n_snapped / n_endpoints, 6) if n_endpoints else None,
        "n_lines_both_ends_snapped": n_lines_both_ends,
        "n_lines_floating": n_lines_floating,
    }


def audit_topology(
    assets: Any, *, snap_tolerance_m: float, water_link_distance_m: float
) -> dict[str, Any]:
    """Compute the honest connectivity-coverage report on the loaded OSM assets."""
    power = topo.build_power_graph(assets, snap_tolerance_m=snap_tolerance_m)
    water = topo.build_water_graph(assets, link_distance_m=water_link_distance_m)

    power_deg = power.degree()
    n_power_connected = sum(1 for d in power_deg.values() if d > 0)
    water_deg = water.degree()
    # Sinks (treatment plants) connected to ≥1 source (reservoir).
    water_class = assets.set_index("asset_id").reindex(water.node_ids)["asset_class"]
    sink_ids = [
        nid
        for nid, cls in zip(water.node_ids, water_class, strict=False)
        if cls in topo.WATER_SINK_CLASSES
    ]
    n_plants_linked = sum(1 for nid in sink_ids if water_deg.get(nid, 0) > 0)

    return {
        "power": {
            "n_nodes": power.n_nodes,
            "n_edges": power.n_edges,
            "n_nodes_with_at_least_one_line": n_power_connected,
            "node_connectivity_fraction": (
                round(n_power_connected / power.n_nodes, 6) if power.n_nodes else None
            ),
            "n_connected_components": len(set(power.component_labels().tolist())),
            "voltage_coverage": _voltage_coverage(assets),
            "line_snap_coverage": _line_snap_coverage(assets, snap_tolerance_m=snap_tolerance_m),
            "topology_method": topo.POWER_TOPOLOGY_METHOD,
        },
        "water": {
            "n_nodes": water.n_nodes,
            "n_edges": water.n_edges,
            "n_treatment_plants": len(sink_ids),
            "n_treatment_plants_linked": n_plants_linked,
            "plant_link_fraction": (
                round(n_plants_linked / len(sink_ids), 6) if sink_ids else None
            ),
            "topology_method": topo.WATER_TOPOLOGY_METHOD,
        },
    }


def _resolve_assets(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """Load the OSM assets either from a parquet or by fetching over the AOI."""
    if args.osm_parquet is not None:
        assets = load_assets(args.osm_parquet)
        return assets, {"osm_source": "parquet", "osm_parquet": str(args.osm_parquet)}

    # Fetch path: stays here (heavy import) so the parquet path is network-free.
    import tempfile

    from wildfire_exposure_eo.osm import fetch_osm
    from wildfire_exposure_eo.stac import load_aoi_geometry

    aoi_path = SMOKE_AOI if args.smoke else (args.aoi or PILOT_AOI)
    _, aoi_sha = load_aoi_geometry(aoi_path)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "osm_assets.parquet"
        fetch_osm(
            aoi_path,
            TAXONOMY,
            out,
            run_id="wu19-topology-audit",
            code_commit_sha=code_commit_sha(),
            aoi_geometry_sha=aoi_sha,
        )
        assets = load_assets(out)
    return assets, {"osm_source": "overpass_fetch", "aoi_path": str(aoi_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm-parquet", type=Path, default=None, help="WU-2 OSM asset GeoParquet")
    parser.add_argument("--aoi", type=Path, default=None, help="AOI geojson (default: pilot)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="diagnostics JSON path")
    parser.add_argument(
        "--snap-tolerance-m",
        type=float,
        default=topo.DEFAULT_SNAP_TOLERANCE_M,
        help="power line endpoint→node snapping tolerance (m, EPSG:32629)",
    )
    parser.add_argument(
        "--water-link-distance-m",
        type=float,
        default=topo.DEFAULT_WATER_LINK_DISTANCE_M,
        help="treatment-plant↔reservoir linking distance (m, EPSG:32629)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="fetch the 1 km smoke AOI; assert the graph builds even when sparse; exit 0",
    )
    args = parser.parse_args()

    assets, source = _resolve_assets(args)
    report = audit_topology(
        assets,
        snap_tolerance_m=args.snap_tolerance_m,
        water_link_distance_m=args.water_link_distance_m,
    )
    payload: dict[str, Any] = {
        "wu": "WU-19",
        "code_commit_sha": code_commit_sha(),
        "crs": ASSET_CRS,
        "snap_tolerance_m": args.snap_tolerance_m,
        "water_link_distance_m": args.water_link_distance_m,
        "n_assets_total": len(assets),
        **source,
        **report,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"[wu19] wrote topology audit → {args.out}", file=sys.stderr)
    print(json.dumps(report, indent=2), file=sys.stderr)

    if args.smoke:
        # The smoke tile may have few/zero network assets; the contract is only
        # that the graph BUILDS and the audit completes without raising.
        print("[wu19] smoke: topology audit completed (graph built)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
