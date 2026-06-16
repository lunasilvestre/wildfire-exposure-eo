"""Hermetic smoke test for the WU-19 topology audit + feature pipeline (pillar 1).

Builds a tiny OSM asset GeoParquet (power nodes + lines, water plant + reservoir)
in EPSG:4326 — matching the WU-2 contract — in a temp dir, then exercises the
``scripts/19_topology_audit.py`` parquet path and the ``topology.compute_topology_features``
orchestration offline. This is the WU-19 smoke gate: the graph builds and the audit
JSON is produced even on a sparse network, with no Overpass dependency.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point

from wildfire_exposure_eo import topology as topo

SCRIPTS = Path("scripts")

# Metric anchor inside UTM zone 29N; build in 32629 then reproject to 4326.
_X0, _Y0 = 560000.0, 4510000.0


def _load_audit_module():  # dynamic import of the standalone audit script
    spec = importlib.util.spec_from_file_location(
        "wu19_topology_audit", SCRIPTS / "19_topology_audit.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _osm_parquet(path: Path) -> Path:
    """Write a tiny WU-2-shaped OSM asset GeoParquet with power + water networks."""
    p = [(_X0, _Y0), (_X0 + 100.0, _Y0), (_X0 + 200.0, _Y0)]
    rows: list[dict] = []
    for k, (x, y) in enumerate(p):
        rows.append(
            {
                "asset_id": f"osm:node/{k + 1}",
                "asset_class": "power.substation",
                "osm_type": "node",
                "osm_id": k + 1,
                "geometry": Point(x, y),
                "tags": "{}",
            }
        )
    rows.append(
        {
            "asset_id": "osm:way/10",
            "asset_class": "power.transmission_line",
            "osm_type": "way",
            "osm_id": 10,
            "geometry": LineString([p[0], p[1]]),
            "tags": '{"voltage": "150000"}',
        }
    )
    rows.append(
        {
            "asset_id": "osm:way/11",
            "asset_class": "power.distribution_line",
            "osm_type": "way",
            "osm_id": 11,
            "geometry": LineString([p[1], p[2]]),
            "tags": "{}",
        }
    )
    rows.append(
        {
            "asset_id": "osm:node/20",
            "asset_class": "water.treatment_plant",
            "osm_type": "node",
            "osm_id": 20,
            "geometry": Point(_X0 + 50.0, _Y0 + 50.0),
            "tags": "{}",
        }
    )
    rows.append(
        {
            "asset_id": "osm:way/21",
            "asset_class": "water.reservoir",
            "osm_type": "way",
            "osm_id": 21,
            "geometry": Point(_X0 + 300.0, _Y0 + 50.0),  # within 2 km of the plant
            "tags": "{}",
        }
    )
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=topo.ASSET_CRS).to_crs("EPSG:4326")
    gdf.to_parquet(path, compression="snappy", index=False)
    return path


def test_audit_script_parquet_path_writes_json(tmp_path: Path) -> None:
    module = _load_audit_module()
    osm = _osm_parquet(tmp_path / "osm.parquet")
    out = tmp_path / "19_topology_audit.json"

    sys.argv = ["19_topology_audit.py", "--osm-parquet", str(osm), "--out", str(out)]
    assert module.main() == 0

    payload = json.loads(out.read_text())
    assert payload["wu"] == "WU-19"
    assert payload["crs"] == "EPSG:32629"
    # Power: 3 substations, 2 inferred edges, all nodes connected.
    assert payload["power"]["n_nodes"] == 3
    assert payload["power"]["n_edges"] == 2
    assert payload["power"]["node_connectivity_fraction"] == 1.0
    assert payload["power"]["voltage_coverage"]["voltage_tag_fraction"] == 0.5
    assert payload["power"]["line_snap_coverage"]["endpoint_snap_fraction"] == 1.0
    # Water: 1 plant linked to 1 reservoir within the distance proxy.
    assert payload["water"]["n_treatment_plants"] == 1
    assert payload["water"]["n_treatment_plants_linked"] == 1
    # Heuristics are flagged INFERRED on the public diagnostic surface (#1).
    assert "INFERRED" in payload["power"]["topology_method"]


def test_compute_topology_features_offline(tmp_path: Path) -> None:
    from wildfire_exposure_eo.features import load_assets

    osm = _osm_parquet(tmp_path / "osm.parquet")
    assets = load_assets(osm)
    local = pd.Series(
        {
            "osm:node/1": 0.2,
            "osm:node/2": 0.8,
            "osm:node/3": 0.5,
            "osm:node/20": 0.6,
            "osm:way/21": 0.9,
        }
    )
    res = topo.compute_topology_features(assets, local)
    # Power path: substation node/2 sits between two feeders → degree 2.
    assert res.features.loc["osm:node/2", "feeder_count"] == 2.0
    # Water plant blends with its linked reservoir's exposure (alpha=0.5 default).
    assert res.features.loc["osm:node/20", "network_exposure_propagated"] == 0.5 * 0.6 + 0.5 * 0.9
    assert res.provenance.power_node_count == 3
    assert res.provenance.water_node_count == 2
    assert res.provenance.inferred_edge_count == res.power_graph.n_edges + res.water_graph.n_edges
