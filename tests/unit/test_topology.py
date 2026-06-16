"""Unit tests for the network/topology-aware exposure features (WU-19, pillar 1).

Fixtures are built in the metric grid (EPSG:32629) where coordinates are exact,
then reprojected to the public EPSG:4326 contract, so the synthetic topology
(degrees, components, propagated values) is known by construction and snapping is
deterministic.
"""

from __future__ import annotations

from datetime import date

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from wildfire_exposure_eo import topology as topo
from wildfire_exposure_eo.schemas.scored_asset import (
    AssetFeatures,
    ScoredAsset,
    ScoredAssetProvenance,
)

# A metric anchor inside the AOI UTM zone 29N (EPSG:32629), near the smoke tile.
X0, Y0 = 560000.0, 4510000.0


def _wgs84(metric_rows: list[dict]) -> gpd.GeoDataFrame:
    """Build a metric (32629) GeoDataFrame then reproject to the 4326 contract."""
    gdf = gpd.GeoDataFrame(metric_rows, geometry="geometry", crs=topo.ASSET_CRS)
    return gdf.to_crs("EPSG:4326")


def _three_substation_two_line() -> gpd.GeoDataFrame:
    """3 substations on a row 100 m apart; 2 lines: sub0-sub1, sub1-sub2 (a path)."""
    p = [(X0, Y0), (X0 + 100.0, Y0), (X0 + 200.0, Y0)]
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
    return _wgs84(rows)


# ---------------------------------------------------------------------------
# CRS contract (#2)
# ---------------------------------------------------------------------------
def test_build_power_graph_requires_wgs84() -> None:
    metric = gpd.GeoDataFrame(
        {
            "asset_id": ["osm:node/1"],
            "asset_class": ["power.substation"],
            "osm_type": ["node"],
            "osm_id": [1],
            "geometry": [Point(X0, Y0)],
        },
        crs=topo.ASSET_CRS,
    )
    with pytest.raises(ValueError, match="EPSG:4326"):
        topo.build_power_graph(metric)


def test_graph_xy_is_metric_crs() -> None:
    g = topo.build_power_graph(_three_substation_two_line())
    assert g.crs == "EPSG:32629"
    # Nodes were placed 100 m apart in the metric grid — round-trips within ~1 m.
    d = float(np.hypot(*(g.xy[1] - g.xy[0])))
    assert d == pytest.approx(100.0, abs=1.0)


# ---------------------------------------------------------------------------
# Graph construction: known topology → known degrees / components
# ---------------------------------------------------------------------------
def test_power_graph_known_degrees_and_order() -> None:
    g = topo.build_power_graph(_three_substation_two_line())
    assert g.node_ids == ("osm:node/1", "osm:node/2", "osm:node/3")
    assert g.n_edges == 2
    assert g.edges == ((0, 1), (1, 2))
    assert g.degree() == {"osm:node/1": 1, "osm:node/2": 2, "osm:node/3": 1}
    # One connected component of size 3.
    assert g.component_size() == {"osm:node/1": 3, "osm:node/2": 3, "osm:node/3": 3}
    assert g.inferred_edge_count == 2


def test_snapping_tolerance_is_respected() -> None:
    # A line whose endpoints sit just outside the tolerance of any node → no edge.
    p = [(X0, Y0), (X0 + 100.0, Y0)]
    rows: list[dict] = [
        {
            "asset_id": "osm:node/1",
            "asset_class": "power.substation",
            "osm_type": "node",
            "osm_id": 1,
            "geometry": Point(p[0]),
            "tags": "{}",
        },
        {
            "asset_id": "osm:node/2",
            "asset_class": "power.substation",
            "osm_type": "node",
            "osm_id": 2,
            "geometry": Point(p[1]),
            "tags": "{}",
        },
        {
            "asset_id": "osm:way/9",
            "asset_class": "power.transmission_line",
            "osm_type": "way",
            "osm_id": 9,
            # endpoints 60 m from each node — outside the default 50 m tolerance.
            "geometry": LineString([(X0, Y0 + 60.0), (X0 + 100.0, Y0 + 60.0)]),
            "tags": "{}",
        },
    ]
    gdf = _wgs84(rows)
    assert topo.build_power_graph(gdf, snap_tolerance_m=50.0).n_edges == 0
    # Widen the tolerance past 60 m and the edge appears — deterministically.
    assert topo.build_power_graph(gdf, snap_tolerance_m=70.0).n_edges == 1


def test_snapping_is_deterministic_across_calls() -> None:
    gdf = _three_substation_two_line()
    a = topo.build_power_graph(gdf, seed=42)
    b = topo.build_power_graph(gdf, seed=42)
    assert a.edges == b.edges
    assert a.node_ids == b.node_ids


def test_isolated_node_when_no_lines() -> None:
    rows = [
        {
            "asset_id": "osm:node/1",
            "asset_class": "power.substation",
            "osm_type": "node",
            "osm_id": 1,
            "geometry": Point(X0, Y0),
            "tags": "{}",
        }
    ]
    g = topo.build_power_graph(_wgs84(rows))
    assert g.n_edges == 0
    assert g.degree() == {"osm:node/1": 0}
    assert g.component_size() == {"osm:node/1": 1}


def test_substation_polygon_uses_centroid() -> None:
    # A substation mapped as an area: its centroid becomes the node.
    poly = Polygon([(X0, Y0), (X0 + 20, Y0), (X0 + 20, Y0 + 20), (X0, Y0 + 20)])
    rows = [
        {
            "asset_id": "osm:way/1",
            "asset_class": "power.substation",
            "osm_type": "way",
            "osm_id": 1,
            "geometry": poly,
            "tags": "{}",
        }
    ]
    g = topo.build_power_graph(_wgs84(rows))
    assert g.n_nodes == 1
    assert g.xy[0, 0] == pytest.approx(X0 + 10.0, abs=1.0)


# ---------------------------------------------------------------------------
# Water graph
# ---------------------------------------------------------------------------
def test_water_graph_links_plant_to_reservoir_within_distance() -> None:
    rows = [
        {
            "asset_id": "osm:node/1",
            "asset_class": "water.treatment_plant",
            "osm_type": "node",
            "osm_id": 1,
            "geometry": Point(X0, Y0),
            "tags": "{}",
        },
        {
            "asset_id": "osm:way/2",
            "asset_class": "water.reservoir",
            "osm_type": "way",
            "osm_id": 2,
            "geometry": Point(X0 + 500.0, Y0),  # 500 m away — inside 2000 m default
            "tags": "{}",
        },
        {
            "asset_id": "osm:way/3",
            "asset_class": "water.reservoir",
            "osm_type": "way",
            "osm_id": 3,
            "geometry": Point(X0 + 5000.0, Y0),  # 5 km away — outside
            "tags": "{}",
        },
    ]
    g = topo.build_water_graph(_wgs84(rows))
    assert g.n_nodes == 3
    assert g.n_edges == 1
    deg = g.degree()
    assert deg["osm:node/1"] == 1  # plant linked to exactly one reservoir
    assert deg["osm:way/3"] == 0  # far reservoir is isolated


def test_water_graph_does_not_link_reservoir_to_reservoir() -> None:
    rows = [
        {
            "asset_id": "osm:way/2",
            "asset_class": "water.reservoir",
            "osm_type": "way",
            "osm_id": 2,
            "geometry": Point(X0, Y0),
            "tags": "{}",
        },
        {
            "asset_id": "osm:way/3",
            "asset_class": "water.reservoir",
            "osm_type": "way",
            "osm_id": 3,
            "geometry": Point(X0 + 100.0, Y0),
            "tags": "{}",
        },
    ]
    g = topo.build_water_graph(_wgs84(rows))
    assert g.n_edges == 0  # the proxy only stands for plant↔reservoir


# ---------------------------------------------------------------------------
# Topology features
# ---------------------------------------------------------------------------
def test_feeder_count_and_component_size_features() -> None:
    g = topo.build_power_graph(_three_substation_two_line())
    deg = topo.feeder_count(g)
    comp = topo.network_component_size(g)
    assert deg.to_dict() == {"osm:node/1": 1.0, "osm:node/2": 2.0, "osm:node/3": 1.0}
    assert comp.to_dict() == {"osm:node/1": 3.0, "osm:node/2": 3.0, "osm:node/3": 3.0}


def test_propagate_exposure_known_blend() -> None:
    g = topo.build_power_graph(_three_substation_two_line())
    local = pd.Series(
        {"osm:node/1": 0.2, "osm:node/2": 0.8, "osm:node/3": 0.5}, name="exposure_score"
    )
    prop = topo.propagate_exposure(g, local, alpha=0.5)
    # node1: 0.5*0.2 + 0.5*0.8                = 0.50
    # node2: 0.5*0.8 + 0.5*mean(0.2, 0.5)     = 0.575
    # node3: 0.5*0.5 + 0.5*0.8                = 0.65
    assert prop["osm:node/1"] == pytest.approx(0.50)
    assert prop["osm:node/2"] == pytest.approx(0.575)
    assert prop["osm:node/3"] == pytest.approx(0.65)


def test_propagate_alpha_one_is_identity() -> None:
    g = topo.build_power_graph(_three_substation_two_line())
    local = pd.Series({"osm:node/1": 0.2, "osm:node/2": 0.8, "osm:node/3": 0.5})
    prop = topo.propagate_exposure(g, local, alpha=1.0)
    assert prop.to_dict() == {"osm:node/1": 0.2, "osm:node/2": 0.8, "osm:node/3": 0.5}


def test_propagate_isolated_node_keeps_local() -> None:
    rows = [
        {
            "asset_id": "osm:node/1",
            "asset_class": "power.substation",
            "osm_type": "node",
            "osm_id": 1,
            "geometry": Point(X0, Y0),
            "tags": "{}",
        }
    ]
    g = topo.build_power_graph(_wgs84(rows))
    prop = topo.propagate_exposure(g, pd.Series({"osm:node/1": 0.42}))
    assert prop["osm:node/1"] == pytest.approx(0.42)


def test_propagate_missing_local_is_nan_never_imputed() -> None:
    g = topo.build_power_graph(_three_substation_two_line())
    local = pd.Series({"osm:node/2": 0.8, "osm:node/3": 0.5})  # node/1 absent
    prop = topo.propagate_exposure(g, local, alpha=0.5)
    assert bool(pd.isna(prop["osm:node/1"]))
    # node2 still computes from its present neighbour(s) — node/1 dropped from mean.
    assert prop["osm:node/2"] == pytest.approx(0.5 * 0.8 + 0.5 * 0.5)


def test_propagate_rejects_alpha_out_of_range() -> None:
    g = topo.build_power_graph(_three_substation_two_line())
    with pytest.raises(ValueError, match="alpha"):
        topo.propagate_exposure(g, pd.Series({"osm:node/1": 0.1}), alpha=1.5)


# ---------------------------------------------------------------------------
# Orchestration + provenance (flagged heuristics; #1/#3)
# ---------------------------------------------------------------------------
def test_compute_topology_features_columns_and_provenance() -> None:
    gdf = _three_substation_two_line()
    local = pd.Series({"osm:node/1": 0.2, "osm:node/2": 0.8, "osm:node/3": 0.5})
    res = topo.compute_topology_features(gdf, local)
    assert list(res.features.columns) == list(topo.TOPOLOGY_FEATURE_NAMES)
    assert set(res.features.index) == {"osm:node/1", "osm:node/2", "osm:node/3"}
    prov = res.provenance.as_dict()
    assert prov["seed"] == 42
    assert prov["crs"] == "EPSG:32629"
    assert prov["power_node_count"] == 3
    assert prov["power_edge_count"] == 2
    assert prov["inferred_edge_count"] == 2
    # The heuristic is named and flagged as INFERRED (non-negotiable #1).
    assert "INFERRED" in prov["power_topology_method"]
    assert "INFERRED" in prov["water_topology_method"]


def test_compute_topology_without_local_skips_propagation() -> None:
    res = topo.compute_topology_features(_three_substation_two_line(), None)
    assert bool(res.features["network_exposure_propagated"].isna().all())
    assert bool(res.features["feeder_count"].notna().all())


def test_compute_topology_empty_assets_is_graceful() -> None:
    empty = gpd.GeoDataFrame(
        {
            "asset_id": pd.Series([], dtype="object"),
            "asset_class": pd.Series([], dtype="object"),
            "osm_type": pd.Series([], dtype="object"),
            "osm_id": pd.Series([], dtype="int64"),
            "geometry": gpd.GeoSeries([], crs="EPSG:4326"),
        },
        geometry="geometry",
        crs="EPSG:4326",
    )
    res = topo.compute_topology_features(empty, None)
    assert bool(res.features.empty)
    assert list(res.features.columns) == list(topo.TOPOLOGY_FEATURE_NAMES)
    assert res.provenance.power_node_count == 0


# ---------------------------------------------------------------------------
# Schema acceptance: ScoredAsset carries the new topology fields + provenance
# ---------------------------------------------------------------------------
def test_scored_asset_accepts_topology_fields() -> None:
    feats = AssetFeatures(
        fuel_class_severity_weight=0.4,
        feeder_count=2.0,
        network_component_size=3.0,
        network_exposure_propagated=0.575,
    )
    assert feats.feeder_count == 2.0
    assert feats.network_component_size == 3.0
    assert feats.network_exposure_propagated == pytest.approx(0.575)


def test_scored_asset_full_row_with_topology() -> None:
    sha = "a" * 64
    prov = ScoredAssetProvenance(
        model_version="0.2.0",
        config_sha=sha,
        crosswalk_sha=sha,
        run_id="wu19-test",
        code_commit_sha="deadbeef",
        aoi_path="data/aoi/smoke.geojson",
        aoi_geometry_sha=sha,
        window_start=date(2025, 6, 16),
        window_end=date(2026, 6, 16),
        osm_parquet_sha=sha,
        burns_parquet_sha=sha,
        fuel_cog_sha=sha,
        gch_cache_sha=sha,
        burn_share_threshold=0.5,
    )
    row = ScoredAsset(
        asset_id="osm:node/2",
        osm_type="node",
        osm_id=2,
        asset_class="power.substation",
        criticality_weight=0.95,
        centroid_lon=-8.36,
        centroid_lat=40.735,
        geometry_wkb=Point(-8.36, 40.735).wkb,
        features=AssetFeatures(feeder_count=2.0, network_exposure_propagated=0.575),
        features_present=("feeder_count", "network_exposure_propagated"),
        exposure_score=0.575,
        exposure_rank=1,
        provenance=prov,
    )
    assert row.features.feeder_count == 2.0
    assert "network_exposure_propagated" in row.features_present
