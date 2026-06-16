# Prompt 19 — Network / topology exposure (Pillar 1)

> **DRAFT skeleton (2026-06-16).** The headline differentiator — none of the
> competitors (FireScope / MEJOR-LUCI / aMinhaTerra) model infrastructure
> connectivity. Read end-to-end, confirm prerequisites, execute in phase order,
> leave a `prompts/_session_log.md` entry. Deviations → `prompts/_HIL.md`.
> Long-lead engineering; start early (Wave 1). Buildable in ~1–2 sessions for a
> first transparent version.

## Mission

Model power and water infrastructure as **graphs**, so an asset's exposure
includes its **connectivity**, not just its local buffer:

- A **substation**'s exposure reflects its feeder lines + downstream served load.
- A **water-treatment plant**'s exposure reflects its reservoirs / served area.

The OSM assets are already fetched (`power.transmission_line`,
`power.distribution_line`, `power.substation`, `power.transformer`, `power.tower`;
`water.treatment_plant`, `water.reservoir`). This WU turns those points/lines into
a graph and derives topology-aware exposure feature(s).

**Scope guard.** Transparent, auditable graph construction — no black-box GNN, no
learned propagation, consistent with the "auditable in 5 lines of YAML" ethos.
Still *exposure rank*, never probability (#6). **No invented connectivity** (#1):
edges come from OSM topology + documented heuristics; any inferred edge is flagged
in provenance.

## Phase 0 — Topology audit (what does OSM actually give us?)

Before building the graph, measure connectivity coverage on the **pilot AOI**:
how many substations have at least one connecting line in OSM? how many lines
terminate at a node vs float? what fraction of `power=line` carry `voltage`?
Water: how many treatment plants link to a reservoir/water body? Log the honest
coverage — OSM power topology in rural PT is incomplete, and the feature's
reliability caveat depends on this.

Deliverable: `scripts/19_topology_audit.py` → `outputs/diagnostics/19_topology_audit.json`.

## Phase 1 — Graph construction

- New module `src/wildfire_exposure_eo/topology.py`:
  - **Power graph:** nodes = substations/transformers/towers; edges = lines
    (snap line endpoints to nodes within a documented tolerance; CRS explicit,
    #2). Direction/served-load is generally not in OSM — use a documented
    heuristic (e.g. downstream = lower-voltage neighbours; served load ∝ degree
    or ∝ count of downstream distribution endpoints). **Flag every heuristic** in
    a `topology_method` field; never present inferred direction as ground truth.
  - **Water graph:** treatment plant ↔ reservoir(s) within a documented distance /
    same-watercourse heuristic; "served area" as a documented proxy.
  - Use `networkx` (justify per #8 if not already a dep; pinned).
- Determinism: any tie-break seeded 42 (#4). Build is reproducible from OSM +
  config alone.

## Phase 2 — Topology-aware feature(s)

- Define the connectivity feature(s), e.g.:
  - `network_exposure_propagated` — an asset's exposure blended with a documented
    aggregation of its graph neighbours' local exposure (e.g. feeders' buffer
    exposure flows into the substation; reservoirs' exposure flows into the
    plant). Keep the aggregation **linear and documented**.
  - `downstream_served_count` / `feeder_count` — degree-style structural features.
- Wire into `features.py` as new columns; extend the `ScoredAsset` schema with
  the new fields + provenance (`topology_method`, tolerance, seed).

## Phase 3 — Score integration (SERIALIZED weight edit — see operationalization §4)

- Add the topology feature to `config/exposure_score.yaml`, re-normalise weights
  to sum 1.0, bump version, changelog cites this WU. **Land this edit alone** —
  it is the coordination point with pillars 0 and 4.
- Alternatively (decide + document): ship topology as a *reported* secondary
  feature first (in the parquet + a figure) and integrate into the weighted score
  only after the ablation in pillar 2 shows it carries signal. Either path is
  fine; state which and why.

## Verify-then-act

Smoke AOI first (the 1 km tile has few network assets — confirm the graph builds
and the feature computes even on a sparse graph), then pilot. Log node/edge counts
before computing features.

## Tests required

- Unit: graph construction on a synthetic 3-substation / 2-line fixture (known
  topology → known degrees, known propagated value).
- Unit: snapping tolerance + endpoint matching is deterministic.
- Schema: `ScoredAsset` accepts the new topology fields + provenance.
- Smoke: `scripts/19_topology_audit.py --smoke` exits 0.

## Gates (all must pass)

```bash
uv run ruff check . && uv run ruff format --check . \
  && uv run pyright src tests scripts && uv run pytest
```

New dep (`networkx`): #8 justification + pin, surfaced to the human first.

## Done-when

- Phase-0 topology coverage logged honestly.
- `topology.py` builds reproducible power + water graphs with explicit CRS and
  flagged heuristics.
- Topology feature(s) computed per asset, schema-versioned with provenance.
- Score integration decision made + documented (weighted now, or reported-first).
- Tests + four gates green; session-log entry with node/edge counts + coverage.

## HIL flags

- **FLAG (data contract):** new schema fields + (if integrated) a score-weight
  change → if re-publishing, a re-score follow-on (don't regenerate the published
  parquet here without sign-off).
- **FLAG (#1 / honesty):** the served-load / direction heuristics are inferred,
  not OSM-given. Confirm the chosen heuristic + its caveat wording with the human
  before it appears on the public surface.
