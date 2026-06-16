"""Network / topology-aware exposure features (WU-19, prompt 19, pillar 1).

The headline differentiator: an asset's exposure should include its *connectivity*,
not just its local buffer. A substation's exposure reflects the lines that feed it;
a water-treatment plant's exposure reflects the reservoir(s) that supply it. We turn
the WU-2 OSM power and water assets into **graphs** and derive transparent,
auditable topology features per node.

Scope guard (CLAUDE.md):

* **No black box.** Graph construction is explicit OSM topology + documented
  heuristics; the propagated feature is a single linear blend a reviewer can
  reproduce by hand. No GNN, no learned propagation (non-negotiable #6 ethos).
* **No invented connectivity** (non-negotiable #1). Edges come from snapping line
  endpoints to nodes within a *documented* tolerance, or from a *documented*
  distance heuristic (water). Every inferred edge / direction assumption is named
  in :data:`POWER_TOPOLOGY_METHOD` / :data:`WATER_TOPOLOGY_METHOD` and surfaced in
  per-asset provenance — never presented as OSM ground truth.
* **Rank, not probability** (non-negotiable #6). ``network_exposure_propagated`` is
  a *relative* blend of within-AOI screening ranks, never a calibrated forecast.
* **CRS explicit** (non-negotiable #2). All snapping / distance work happens in the
  metric grid :data:`~wildfire_exposure_eo.features.ASSET_CRS` (EPSG:32629); the
  input GeoDataFrame is asserted to be EPSG:4326 and reprojected exactly once.
* **Deterministic** (non-negotiable #4). Node ordering is ``(asset_class, osm_type,
  osm_id)``; any tie-break is seeded with :data:`DEFAULT_SEED` (42). The graph build
  is reproducible from OSM + config alone — no RNG is required for the default path,
  but ``seed`` is threaded through so it stays reproducible if one is ever added.

Topology coverage in rural-PT OSM power is incomplete (many substations have no
mapped connecting line). The propagated feature degrades gracefully: an isolated
node keeps its own local exposure (the blend reduces to identity), so a sparse
graph never fabricates connectivity-driven exposure.

Heavy/optional dependencies (geopandas, shapely, scipy) are imported lazily so the
module stays importable in lightweight contexts. Graph algorithms use
``scipy.sparse.csgraph`` + the standard library — no networkx (non-negotiable #8:
prefer the existing stack; scipy is already pinned transitively).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pandas as pd

from wildfire_exposure_eo.features import ASSET_CRS

if TYPE_CHECKING:
    import geopandas as gpd

#: Default deterministic seed (CLAUDE.md non-negotiable #4).
DEFAULT_SEED = 42

#: Power node classes (point-like assets that become graph nodes). Substations and
#: transformers may be areas in OSM; their centroid is used as the node location.
POWER_NODE_CLASSES: tuple[str, ...] = (
    "power.substation",
    "power.transformer",
    "power.tower",
)
#: Power edge classes (line assets whose endpoints are snapped to nearby nodes).
POWER_LINE_CLASSES: tuple[str, ...] = (
    "power.transmission_line",
    "power.distribution_line",
)
#: Water "sink" nodes (consumers of supply) and "source" nodes (suppliers).
WATER_SINK_CLASSES: tuple[str, ...] = ("water.treatment_plant",)
WATER_SOURCE_CLASSES: tuple[str, ...] = ("water.reservoir",)

#: Default endpoint→node snapping tolerance for the power graph, in metres
#: (EPSG:32629). A line endpoint within this distance of a node is treated as
#: physically connected to it. Documented heuristic, not OSM-given topology.
DEFAULT_SNAP_TOLERANCE_M = 50.0
#: Default treatment-plant ↔ reservoir linking distance, in metres (EPSG:32629).
#: A documented proximity proxy for "this reservoir plausibly supplies this plant";
#: OSM rarely records the supply relation explicitly.
DEFAULT_WATER_LINK_DISTANCE_M = 2000.0
#: Default blend weight α for ``network_exposure_propagated``: the share of a node's
#: propagated value taken from its own local exposure; ``1 - α`` is the mean local
#: exposure of its graph neighbours. α = 1 reduces to the local feature (identity).
DEFAULT_PROPAGATION_ALPHA = 0.5

#: Human-readable description of the *power* graph heuristics (goes into provenance
#: so the inferred edges/direction are never mistaken for OSM ground truth).
POWER_TOPOLOGY_METHOD = (
    "power graph: nodes = substations/transformers/towers (centroid in EPSG:32629); "
    "edges = transmission/distribution lines, each line endpoint snapped to the "
    "nearest node within snap_tolerance_m (default 50 m) — INFERRED connectivity, "
    "not OSM-given; the graph is undirected (OSM does not record power-flow "
    "direction, so no upstream/downstream is claimed)."
)
#: Human-readable description of the *water* graph heuristics.
WATER_TOPOLOGY_METHOD = (
    "water graph: nodes = treatment plants + reservoirs (centroid in EPSG:32629); "
    "edges = treatment-plant ↔ reservoir within water_link_distance_m (default "
    "2000 m) — INFERRED proximity proxy for a supply relation, not OSM-given; "
    "same-watercourse linkage is not asserted."
)

#: Canonical topology feature names (added as AVAILABLE features; the normalized
#: score-weight block in config/exposure_score.yaml is NOT changed here — that edit
#: is serialized by the orchestrator, per WU-19 phase 3).
TOPOLOGY_FEATURE_NAMES: tuple[str, ...] = (
    "feeder_count",
    "network_component_size",
    "network_exposure_propagated",
)


# ---------------------------------------------------------------------------
# Graph container
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AssetGraph:
    """An undirected asset graph with deterministic node ordering.

    ``node_ids`` is the ordered list of ``asset_id``s (one per node).
    ``edges`` is the list of ``(i, j)`` index pairs into ``node_ids`` (i < j,
    de-duplicated, sorted). ``xy`` holds the node coordinates in EPSG:32629.
    ``method`` records the documented heuristics used to build the edges.
    ``inferred_edge_count`` is the number of edges produced by a heuristic (all of
    them, currently) — surfaced so a reviewer sees how much connectivity is
    inferred vs. OSM-given (currently: none is OSM-given).
    """

    node_ids: tuple[str, ...]
    edges: tuple[tuple[int, int], ...]
    xy: np.ndarray  # shape (n_nodes, 2), EPSG:32629
    method: str
    crs: str = ASSET_CRS
    inferred_edge_count: int = 0
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def n_nodes(self) -> int:
        return len(self.node_ids)

    @property
    def n_edges(self) -> int:
        return len(self.edges)

    def degree(self) -> dict[str, int]:
        """Node degree keyed by ``asset_id`` (0 for isolated nodes)."""
        deg = dict.fromkeys(self.node_ids, 0)
        for i, j in self.edges:
            deg[self.node_ids[i]] += 1
            deg[self.node_ids[j]] += 1
        return deg

    def neighbours(self) -> dict[str, list[str]]:
        """Adjacency list keyed by ``asset_id`` (deterministically ordered)."""
        adj: dict[str, list[str]] = {nid: [] for nid in self.node_ids}
        for i, j in self.edges:
            adj[self.node_ids[i]].append(self.node_ids[j])
            adj[self.node_ids[j]].append(self.node_ids[i])
        return adj

    def component_labels(self) -> np.ndarray:
        """Connected-component label per node (via ``scipy.sparse.csgraph``)."""
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        n = self.n_nodes
        if n == 0:
            return np.zeros(0, dtype="int64")
        if not self.edges:
            return np.arange(n, dtype="int64")
        rows = np.array([i for i, _ in self.edges] + [j for _, j in self.edges], dtype="int64")
        cols = np.array([j for _, j in self.edges] + [i for i, _ in self.edges], dtype="int64")
        data = np.ones(rows.size, dtype="int8")
        adj = coo_matrix((data, (rows, cols)), shape=(n, n)).tocsr()
        _, labels = connected_components(adj, directed=False)
        return labels.astype("int64")

    def component_size(self) -> dict[str, int]:
        """Size of the connected component each node belongs to, keyed by ``asset_id``."""
        labels = self.component_labels()
        if labels.size == 0:
            return {}
        counts = np.bincount(labels)
        return {nid: int(counts[labels[k]]) for k, nid in enumerate(self.node_ids)}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _endpoints_xy(geom: Any) -> list[tuple[float, float]]:
    """Return the boundary endpoint coordinates of a (multi)line geometry.

    Points/polygons contribute their representative point; this keeps the helper
    total over whatever geometry a line class happens to carry in OSM.
    """
    gtype = geom.geom_type
    if gtype == "LineString":
        coords = list(geom.coords)
        return [coords[0], coords[-1]] if len(coords) >= 2 else list(coords)
    if gtype in ("MultiLineString", "GeometryCollection"):
        out: list[tuple[float, float]] = []
        for part in geom.geoms:
            out.extend(_endpoints_xy(part))
        return out
    pt = geom.representative_point()
    return [(pt.x, pt.y)]


def _nearest_node(pt: tuple[float, float], node_xy: np.ndarray, tolerance_m: float) -> int | None:
    """Index of the nearest node within ``tolerance_m`` of ``pt`` (else ``None``).

    Deterministic: on a distance tie the lowest node index wins (``argmin`` is
    stable, and nodes are pre-sorted by ``(asset_class, osm_type, osm_id)``).
    """
    if node_xy.shape[0] == 0:
        return None
    dx = node_xy[:, 0] - pt[0]
    dy = node_xy[:, 1] - pt[1]
    d2 = dx * dx + dy * dy
    k = int(np.argmin(d2))
    return k if d2[k] <= tolerance_m * tolerance_m else None


def _ordered_nodes(assets: gpd.GeoDataFrame, classes: tuple[str, ...]) -> gpd.GeoDataFrame:
    """Subset ``assets`` to ``classes``, project to EPSG:32629, sort deterministically."""
    sub = cast("gpd.GeoDataFrame", assets[assets["asset_class"].isin(classes)])
    ordered = sub.sort_values(by=["asset_class", "osm_type", "osm_id"]).reset_index(drop=True)
    return cast("gpd.GeoDataFrame", ordered.to_crs(ASSET_CRS))


def _node_xy(nodes: gpd.GeoDataFrame) -> np.ndarray:
    """(n, 2) centroid coordinates in the node frame's CRS (EPSG:32629)."""
    if len(nodes) == 0:
        return np.zeros((0, 2), dtype="float64")
    cent = nodes.geometry.centroid
    return np.column_stack([cent.x.to_numpy(), cent.y.to_numpy()]).astype("float64")


def _assert_wgs84(assets: gpd.GeoDataFrame) -> None:
    """Refuse to build a graph from an asset frame without explicit EPSG:4326."""
    if assets.crs is None:
        raise ValueError("asset GeoDataFrame has no CRS — refusing to assume one (#2)")
    if assets.crs.to_epsg() != 4326:
        raise ValueError(f"asset GeoDataFrame CRS is {assets.crs} — expected EPSG:4326 (#2)")


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------
def build_power_graph(
    assets: gpd.GeoDataFrame,
    *,
    snap_tolerance_m: float = DEFAULT_SNAP_TOLERANCE_M,
    seed: int = DEFAULT_SEED,
) -> AssetGraph:
    """Build the undirected power graph from WU-2 OSM power assets.

    Nodes are substations/transformers/towers (centroid in EPSG:32629). For each
    transmission/distribution line, both endpoints are snapped to the nearest node
    within ``snap_tolerance_m``; if both ends resolve to *distinct* nodes, an edge
    is added between them (INFERRED connectivity — see :data:`POWER_TOPOLOGY_METHOD`).
    A line whose endpoints snap to the same node, or whose endpoints do not resolve,
    contributes no edge. The graph is undirected: OSM does not record power-flow
    direction, so no upstream/downstream is claimed.

    Deterministic: nodes are ordered ``(asset_class, osm_type, osm_id)``; lines are
    processed in the same order; ties on the nearest node resolve to the lowest
    node index. ``seed`` is accepted for API symmetry (the default path uses no RNG).
    """
    _assert_wgs84(assets)
    nodes = _ordered_nodes(assets, POWER_NODE_CLASSES)
    node_ids = tuple(nodes["asset_id"].astype(str).tolist())
    node_xy = _node_xy(nodes)

    lines = _ordered_nodes(assets, POWER_LINE_CLASSES)
    edge_set: set[tuple[int, int]] = set()
    for geom in lines.geometry:
        if geom is None or geom.is_empty:
            continue
        ends = _endpoints_xy(geom)
        snapped = [_nearest_node(e, node_xy, snap_tolerance_m) for e in ends]
        resolved = [s for s in snapped if s is not None]
        for a in range(len(resolved)):
            for b in range(a + 1, len(resolved)):
                i, j = resolved[a], resolved[b]
                if i != j:
                    edge_set.add((min(i, j), max(i, j)))
    edges = tuple(sorted(edge_set))
    return AssetGraph(
        node_ids=node_ids,
        edges=edges,
        xy=node_xy,
        method=POWER_TOPOLOGY_METHOD,
        inferred_edge_count=len(edges),
        params={"snap_tolerance_m": float(snap_tolerance_m), "seed": int(seed)},
    )


def build_water_graph(
    assets: gpd.GeoDataFrame,
    *,
    link_distance_m: float = DEFAULT_WATER_LINK_DISTANCE_M,
    seed: int = DEFAULT_SEED,
) -> AssetGraph:
    """Build the undirected water graph from WU-2 OSM water assets.

    Nodes are treatment plants + reservoirs (centroid in EPSG:32629). A
    treatment-plant ↔ reservoir edge is added when their centroids are within
    ``link_distance_m`` (INFERRED proximity proxy — see
    :data:`WATER_TOPOLOGY_METHOD`). Plant-plant and reservoir-reservoir pairs are
    never linked: the proxy stands only for the supply relation between the two
    classes. The graph is undirected; "served area" is left as the documented
    proxy and is not asserted from OSM.

    Deterministic: nodes are ordered ``(asset_class, osm_type, osm_id)``; the
    pairwise scan visits sinks then sources in that order. ``seed`` is accepted for
    API symmetry (the default path uses no RNG).
    """
    _assert_wgs84(assets)
    nodes = _ordered_nodes(assets, WATER_SINK_CLASSES + WATER_SOURCE_CLASSES)
    node_ids = tuple(nodes["asset_id"].astype(str).tolist())
    node_xy = _node_xy(nodes)
    classes = nodes["asset_class"].astype(str).to_numpy()

    sink_idx = [k for k, c in enumerate(classes) if c in WATER_SINK_CLASSES]
    source_idx = [k for k, c in enumerate(classes) if c in WATER_SOURCE_CLASSES]
    edge_set: set[tuple[int, int]] = set()
    thresh2 = link_distance_m * link_distance_m
    for si in sink_idx:
        for ri in source_idx:
            dx = node_xy[si, 0] - node_xy[ri, 0]
            dy = node_xy[si, 1] - node_xy[ri, 1]
            if dx * dx + dy * dy <= thresh2:
                edge_set.add((min(si, ri), max(si, ri)))
    edges = tuple(sorted(edge_set))
    return AssetGraph(
        node_ids=node_ids,
        edges=edges,
        xy=node_xy,
        method=WATER_TOPOLOGY_METHOD,
        inferred_edge_count=len(edges),
        params={"link_distance_m": float(link_distance_m), "seed": int(seed)},
    )


# ---------------------------------------------------------------------------
# Topology-aware features
# ---------------------------------------------------------------------------
def feeder_count(graph: AssetGraph) -> pd.Series:
    """Node degree (number of incident edges) keyed by ``asset_id``.

    For a substation this is its feeder count under the inferred topology; for a
    water-treatment plant, the number of linked reservoirs. A structural feature,
    not a probability. Isolated nodes get 0.
    """
    deg = graph.degree()
    series = pd.Series({nid: float(deg[nid]) for nid in graph.node_ids}, dtype="float64")
    series.index.name = "asset_id"
    series.name = "feeder_count"
    return series


def network_component_size(graph: AssetGraph) -> pd.Series:
    """Connected-component size each node belongs to, keyed by ``asset_id``.

    A larger component means the node is embedded in a more extensive sub-network
    (more co-exposed infrastructure). Isolated nodes get 1. Structural, not a
    probability.
    """
    sizes = graph.component_size()
    series = pd.Series({nid: float(sizes.get(nid, 1)) for nid in graph.node_ids}, dtype="float64")
    series.index.name = "asset_id"
    series.name = "network_component_size"
    return series


def propagate_exposure(
    graph: AssetGraph,
    local_exposure: pd.Series,
    *,
    alpha: float = DEFAULT_PROPAGATION_ALPHA,
) -> pd.Series:
    """Blend each node's local exposure with the mean of its neighbours' local exposure.

    ``network_exposure_propagated[v] = α · local[v] + (1 - α) · mean(local[u]
    for u in neighbours(v))``. A single, linear, documented aggregation a reviewer
    can reproduce by hand (non-negotiable #6 ethos): a substation's exposure picks
    up the exposure of the feeders/nodes it connects to; a plant's picks up its
    reservoirs'. ``alpha`` ∈ [0, 1] is the self-weight; ``alpha = 1`` reduces to the
    local feature (identity).

    An **isolated** node (no neighbours) keeps its own local exposure exactly — the
    blend never fabricates connectivity-driven exposure on a sparse graph. Neighbours
    whose ``local_exposure`` is ``NaN`` are dropped from the mean; if *all* neighbours
    are NaN the node again reduces to its own local value. A node missing from
    ``local_exposure`` yields ``NaN`` (never imputed).

    ``local_exposure`` is a *relative within-AOI screening rank*, so the propagated
    value is too — never a calibrated probability or forecast.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha}")
    adj = graph.neighbours()
    # Materialise into a plain dict so lookups are unambiguous floats (NaN when absent).
    local: dict[str, float] = {
        str(k): float(v) for k, v in local_exposure.astype("float64").items()
    }
    out: dict[str, float] = {}
    for nid in graph.node_ids:
        self_val = local.get(nid, float("nan"))
        nbr_vals = [local[u] for u in adj[nid] if u in local and not np.isnan(local[u])]
        if np.isnan(self_val):
            out[nid] = float("nan")
        elif not nbr_vals:
            out[nid] = self_val
        else:
            out[nid] = alpha * self_val + (1.0 - alpha) * (sum(nbr_vals) / len(nbr_vals))
    series = pd.Series(out, dtype="float64")
    series.index.name = "asset_id"
    series.name = "network_exposure_propagated"
    return series


# ---------------------------------------------------------------------------
# Orchestration: assemble all topology features over the power + water graphs.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TopologyProvenance:
    """Provenance for a topology-feature computation (flagged heuristics; #1, #3)."""

    power_topology_method: str
    water_topology_method: str
    snap_tolerance_m: float
    water_link_distance_m: float
    propagation_alpha: float
    seed: int
    crs: str
    power_node_count: int
    power_edge_count: int
    water_node_count: int
    water_edge_count: int
    inferred_edge_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "power_topology_method": self.power_topology_method,
            "water_topology_method": self.water_topology_method,
            "snap_tolerance_m": self.snap_tolerance_m,
            "water_link_distance_m": self.water_link_distance_m,
            "propagation_alpha": self.propagation_alpha,
            "seed": self.seed,
            "crs": self.crs,
            "power_node_count": self.power_node_count,
            "power_edge_count": self.power_edge_count,
            "water_node_count": self.water_node_count,
            "water_edge_count": self.water_edge_count,
            "inferred_edge_count": self.inferred_edge_count,
        }


@dataclass(frozen=True)
class TopologyResult:
    """Per-asset topology features plus the graphs and flagged-heuristic provenance."""

    features: pd.DataFrame  # indexed by asset_id; TOPOLOGY_FEATURE_NAMES columns
    power_graph: AssetGraph
    water_graph: AssetGraph
    provenance: TopologyProvenance


def compute_topology_features(
    assets: gpd.GeoDataFrame,
    local_exposure: pd.Series | None = None,
    *,
    snap_tolerance_m: float = DEFAULT_SNAP_TOLERANCE_M,
    water_link_distance_m: float = DEFAULT_WATER_LINK_DISTANCE_M,
    alpha: float = DEFAULT_PROPAGATION_ALPHA,
    seed: int = DEFAULT_SEED,
) -> TopologyResult:
    """Build the power + water graphs and compute the topology features per asset.

    ``assets`` is the WU-2 OSM asset GeoDataFrame in EPSG:4326. ``local_exposure``
    is a per-``asset_id`` Series of within-AOI screening ranks (e.g. the WU-6
    ``exposure_score``); when ``None``, ``network_exposure_propagated`` is left
    absent (NaN) — the structural features (``feeder_count``,
    ``network_component_size``) still compute. The output is indexed by every
    *node* ``asset_id`` across both graphs (non-network assets are not included —
    callers reindex onto the full asset set and leave NaN where topology does not
    apply, never imputing).

    Returns a :class:`TopologyResult` carrying the features, both graphs (with node/
    edge counts for the session log), and provenance naming every heuristic (#1/#3).
    """
    power = build_power_graph(assets, snap_tolerance_m=snap_tolerance_m, seed=seed)
    water = build_water_graph(assets, link_distance_m=water_link_distance_m, seed=seed)

    frames: list[pd.DataFrame] = []
    for graph in (power, water):
        if graph.n_nodes == 0:
            continue
        cols: dict[str, pd.Series] = {
            "feeder_count": feeder_count(graph),
            "network_component_size": network_component_size(graph),
        }
        if local_exposure is not None:
            cols["network_exposure_propagated"] = propagate_exposure(
                graph, local_exposure, alpha=alpha
            )
        frames.append(pd.DataFrame(cols))

    if frames:
        features = cast("pd.DataFrame", pd.concat(frames, axis=0)).sort_index()
    else:
        features = pd.DataFrame(columns=pd.Index(list(TOPOLOGY_FEATURE_NAMES)))
        features.index.name = "asset_id"
    # Keep a stable column order even when propagation was skipped.
    for col in TOPOLOGY_FEATURE_NAMES:
        if col not in features.columns:
            features[col] = np.nan
    features = cast("pd.DataFrame", features[list(TOPOLOGY_FEATURE_NAMES)])

    provenance = TopologyProvenance(
        power_topology_method=POWER_TOPOLOGY_METHOD,
        water_topology_method=WATER_TOPOLOGY_METHOD,
        snap_tolerance_m=float(snap_tolerance_m),
        water_link_distance_m=float(water_link_distance_m),
        propagation_alpha=float(alpha),
        seed=int(seed),
        crs=ASSET_CRS,
        power_node_count=power.n_nodes,
        power_edge_count=power.n_edges,
        water_node_count=water.n_nodes,
        water_edge_count=water.n_edges,
        inferred_edge_count=power.inferred_edge_count + water.inferred_edge_count,
    )
    return TopologyResult(
        features=features,
        power_graph=power,
        water_graph=water,
        provenance=provenance,
    )
