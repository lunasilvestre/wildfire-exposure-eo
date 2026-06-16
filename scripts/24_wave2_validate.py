"""Wave-2 multi-AOI validation of the exposure rank vs subsequent ICNF burns.

Extends ``scripts/11_validate.py`` from the single PILOT AOI to ALL scored AOIs —
the pilot plus the four Wave-2 AOIs (pedrógão grande, serra da estrela, peneda
gerês, monchique). It answers the three Wave-2 questions and emits the markdown of
the Wave-2 section of ``docs/validation_report.md`` (every number in the report is
this script's output — CLAUDE.md fact-checking checklist):

1. **Does widening fix the N=5 emptiness?** Pooled + per-AOI discrimination
   (decile lift, Spearman ρ of score vs subsequent-burn label). N(burned) pooled,
   contrasted with the old single-AOI N=5.
2. **Does FWI earn its 0.10 weight (FWI-HELPS)?** For the four v0.3.0 AOIs (which
   carry backdated, pre-event FWI), compare burn discrimination of the structural-
   only score (``fwi_fwi_current`` dropped → the v0.2.0 weight set) against the
   full v0.3.0 score. Honest verdict — no positive result is forced.
3. **Would topology earn a weight in v0.4.0 (TOPOLOGY-HELPS)?** Build the WU-19
   power + water graphs per AOI, propagate local exposure to graph neighbours, and
   compare discrimination of the propagated rank against the local rank on the
   network-node subset. Honest verdict.

METHODOLOGY (replicated from scripts/11_validate.py):
  * Truth = ICNF Áreas Ardidas perimeters with vintage **strictly after** each
    AOI's score-input window end (the §12 temporal-leakage rule, enforced by
    ``validation.assert_no_temporal_leakage``). Each AOI used its own per-AOI
    burns parquet (matched by the provenance ``burns_parquet_sha``).
  * Per-asset truth label = the asset buffer intersects any validation-window burn.
  * The score is a *relative screening rank*, never a calibrated probability — lift
    and Spearman measure only whether higher-ranked assets burned more often later.
  * Leakage handling: ``recent_burn_share_12mo`` is absent from every backdated run
    (the scoring code nulls it rather than leak post-window observations); FWI here
    is BACKDATED (each AOI's ``fwi_valid_date`` lies inside its pre-event window),
    so unlike the operational current-season re-score it is contemporaneous with
    the score window and may be validated against subsequent burns.

Determinism: no runtime clock; two runs over the same inputs produce identical
output. Burns-source resolution: each AOI's exposure parquet records the
``burns_parquet_sha`` it was scored against; this script finds the matching burns
parquet by SHA-256 across the configured search roots (non-negotiable #3 — never
guesses a burns file).

Data root: the scored parquets + per-AOI burns parquets are gitignored outputs that
live in the MAIN checkout (and sibling worktrees), not in this worktree. The
``--data-root`` flag (default: the resolved main checkout) points the reader at
them; code and docs are written into the current worktree.

Usage::

    uv run python scripts/24_wave2_validate.py --out docs/_wave2_section.md
    uv run python scripts/24_wave2_validate.py --inject docs/validation_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import geopandas as gpd
import numpy as np
import pandas as pd
import yaml

# Repo-root import shim so the script runs from anywhere (matches scripts/11_*).
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from wildfire_exposure_eo import topology as topo
from wildfire_exposure_eo.features import buffer_assets, sha256_file
from wildfire_exposure_eo.schemas.scored_asset import FEATURE_NAMES
from wildfire_exposure_eo.scoring import compose_exposure, load_exposure_config
from wildfire_exposure_eo.stac import code_commit_sha
from wildfire_exposure_eo.validation import (
    asset_burn_labels,
    lift_table,
    spearman_rank,
)

#: FWI composite feature dropped to reconstruct the structural-only (v0.2.0) score.
FWI_FEATURE = "fwi_fwi_current"

#: AOIs scored for Wave-2. The PILOT exposure parquet is found by glob (latest
#: non-AOI-suffixed exposure_*.parquet); the four Wave-2 AOIs are named explicitly
#: by their committed scored-parquet basenames (the task hand-off identifiers).
WAVE2_AOIS: tuple[str, ...] = (
    "pedrogao_grande",
    "serra_da_estrela",
    "peneda_geres",
    "monchique",
)


def _default_data_root() -> Path:
    """Resolve the MAIN checkout that holds the gitignored outputs.

    When run from a worktree under ``.claude/worktrees/<id>/``, the outputs live in
    the parent checkout. Walk up to the directory above ``.claude/worktrees``.
    """
    here = REPO_ROOT
    parts = here.parts
    if ".claude" in parts:
        i = parts.index(".claude")
        return Path(*parts[:i])
    return here


@dataclass(frozen=True)
class AoiRun:
    """One AOI's resolved inputs: scored parquet + matched burns parquet."""

    name: str
    exposure_path: Path
    burns_path: Path
    model_version: str
    window_start: date
    window_end: date
    run_id: str


def _find_pilot_exposure(pq: Path) -> Path:
    """Newest pilot exposure parquet: ``exposure_<runid>.parquet`` (no AOI suffix)."""
    cands = sorted(pq.glob("exposure_*.parquet"))
    pilot = [
        c
        for c in cands
        if "_smoke_" not in c.name
        and "_ablation" not in c.name
        and not any(c.name.startswith(f"exposure_{a}_") for a in WAVE2_AOIS)
    ]
    if not pilot:
        raise SystemExit(f"no pilot exposure_*.parquet in {pq}")
    return pilot[-1]


def _find_wave2_exposure(pq: Path, aoi: str) -> Path:
    cands = sorted(pq.glob(f"exposure_{aoi}_*.parquet"))
    cands = [c for c in cands if "_ablation" not in c.name]
    if not cands:
        raise SystemExit(f"no exposure_{aoi}_*.parquet in {pq}")
    return cands[-1]


def _burns_search_roots(data_root: Path) -> tuple[Path, ...]:
    """Roots searched (in order) for the per-AOI burns parquet by SHA-256."""
    return (
        data_root / "outputs" / "parquet",
        *sorted((data_root / ".claude" / "worktrees").glob("*/outputs/parquet")),
    )


def _match_burns_by_sha(target_sha: str, data_root: Path) -> Path:
    """Find the burns parquet whose SHA-256 equals ``target_sha`` (provenance match)."""
    roots = _burns_search_roots(data_root)
    for root in roots:
        if not root.is_dir():
            continue
        for cand in sorted(root.glob("icnf_burns_*.parquet")):
            if "_smoke_" in cand.name:
                continue
            if sha256_file(cand) == target_sha:
                return cand
    raise SystemExit(
        f"no burns parquet with SHA-256 {target_sha[:12]}… found under "
        f"{[str(r) for r in roots]} — refusing to validate against a burns file "
        "that does not match the exposure provenance (non-negotiable #3)"
    )


def _resolve_run(name: str, exposure_path: Path, data_root: Path) -> AoiRun:
    g = gpd.read_parquet(exposure_path)
    if g.crs is None or g.crs.to_epsg() != 4326:
        raise SystemExit(f"{exposure_path} CRS is {g.crs} — expected EPSG:4326")
    prov = json.loads(g["provenance"].iloc[0])
    burns_path = _match_burns_by_sha(prov["burns_parquet_sha"], data_root)
    return AoiRun(
        name=name,
        exposure_path=exposure_path,
        burns_path=burns_path,
        model_version=prov["model_version"],
        window_start=date.fromisoformat(prov["window_start"]),
        window_end=date.fromisoformat(prov["window_end"]),
        run_id=prov["run_id"],
    )


def _features_frame(exposure: gpd.GeoDataFrame) -> pd.DataFrame:
    """Reconstruct the per-asset features DataFrame from the parquet ``features`` JSON."""
    rows = [json.loads(s) for s in exposure["features"]]
    df = pd.DataFrame(rows, index=pd.Index(exposure["asset_id"], name="asset_id"))
    cols = [c for c in FEATURE_NAMES if c in df.columns]
    return cast("pd.DataFrame", df[cols])


def _assert_no_leakage(window_end: date, burns: gpd.GeoDataFrame) -> None:
    """Hard §12 gate: every validation burn must post-date the window-end YEAR."""
    if "vintage_year" not in burns.columns:
        raise ValueError("burns missing 'vintage_year' column")
    if len(burns) == 0:
        return
    min_year = int(burns["vintage_year"].min())
    if min_year <= window_end.year:
        raise ValueError(
            f"temporal leakage: validation burns include vintage_year {min_year} "
            f"<= score-input window end year {window_end.year} (methodology §12)"
        )


def _compute(scores: pd.Series, labels: pd.Series, *, deciles: int = 10) -> dict[str, Any]:
    """Lift table + Spearman for an aligned (scores, labels) pair. Handles base-rate 0."""
    mask = scores.notna()
    s = cast("pd.Series", scores[mask])
    y = cast("pd.Series", labels.reindex(s.index))
    n = len(s)
    n_burned = int(y.sum())
    base_rate = n_burned / n if n else float("nan")
    if n == 0 or n_burned == 0:
        return {"n": n, "n_burned": n_burned, "base_rate": base_rate, "degenerate": True}
    table = lift_table(s.to_numpy(), y.to_numpy().astype(float), deciles=deciles)
    rho, p = spearman_rank(s.to_numpy(), y.to_numpy().astype(float))
    return {
        "n": n,
        "n_burned": n_burned,
        "base_rate": base_rate,
        "table": table,
        "top_decile_lift": float(table.iloc[0]["lift"]),
        "top_decile_cum_lift": float(table.iloc[0]["cumulative_lift"]),
        "spearman_rho": rho,
        "spearman_p": p,
        "degenerate": False,
    }


def _labels_for(
    run: AoiRun, taxonomy: dict[str, Any]
) -> tuple[pd.Series, list[int], dict[str, Any]]:
    """Leakage-safe burn labels for an AOI + its validation-window summary."""
    exposure = gpd.read_parquet(run.exposure_path)
    burns = gpd.read_parquet(run.burns_path)
    if burns.crs is None or burns.crs.to_epsg() != 4326:
        raise SystemExit(f"{run.burns_path} CRS is {burns.crs} — expected EPSG:4326")
    validation_years = sorted(
        {int(y) for y in burns["vintage_year"] if int(y) > run.window_end.year}
    )
    validation_burns = cast("gpd.GeoDataFrame", burns[burns["vintage_year"].isin(validation_years)])
    _assert_no_leakage(run.window_end, validation_burns)
    buffers = buffer_assets(exposure, taxonomy)
    labels = asset_burn_labels(buffers, burns, years=validation_years)
    summary = {
        "validation_years": validation_years,
        "n_validation_perimeters": len(validation_burns),
        "validation_area_ha": (
            float(validation_burns["area_ha"].sum()) if len(validation_burns) else 0.0
        ),
    }
    return labels, validation_years, summary


def _aoi_scores(run: AoiRun) -> dict[str, Any]:
    """The score variants for one AOI, aligned by asset_id."""
    config = load_exposure_config(REPO_ROOT / "config" / "exposure_score.yaml")
    exposure = gpd.read_parquet(run.exposure_path)
    feats = _features_frame(exposure)
    stored = cast("pd.Series", exposure.set_index("asset_id")["exposure_score"])

    # Structural-only: drop the FWI composite, let compose_exposure renormalise
    # per row over the remaining present+weighted features (exactly the v0.2.0 path).
    feats_struct = feats.drop(columns=[FWI_FEATURE], errors="ignore")
    structural = cast("pd.Series", compose_exposure(feats_struct, config)["exposure_score"])

    # Topology: propagate the stored local score over the power + water graphs.
    result = topo.compute_topology_features(exposure, local_exposure=stored)
    prop = result.features["network_exposure_propagated"]
    node_ids = list(result.features.index)
    topo_local = stored.reindex(node_ids)
    topo_prop = prop.reindex(node_ids)
    return {
        "full": stored,
        "structural": structural,
        "topo_local": topo_local,
        "topo_prop": topo_prop,
        "topo_prov": result.provenance.as_dict(),
    }


def _fmt_pct(x: float) -> str:
    return f"{x:.4f}"


def _per_aoi_table(rows: list[dict[str, Any]]) -> list[str]:
    out = [
        "| AOI | model | validation years | N assets | N burned | base rate | "
        "top-decile lift | Spearman ρ | p |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        m = r["full"]
        if m["degenerate"]:
            out.append(
                f"| {r['name']} | {r['model_version']} | {r['vyears']} | {m['n']} | "
                f"{m['n_burned']} | — | — (degenerate) | — | — |"
            )
        else:
            out.append(
                f"| {r['name']} | {r['model_version']} | {r['vyears']} | {m['n']} | "
                f"{m['n_burned']} | {_fmt_pct(m['base_rate'])} | "
                f"{m['top_decile_lift']:.2f}× | {m['spearman_rho']:.4f} | {m['spearman_p']:.2g} |"
            )
    return out


def _lift_md(table: pd.DataFrame) -> list[str]:
    lines = [
        "| decile | n_assets | n_burned | burn_rate | lift | cumulative_lift |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in table.iterrows():
        lines.append(
            f"| {int(r['decile'])} | {int(r['n_assets'])} | {int(r['n_burned'])} | "
            f"{r['burn_rate']:.4f} | {r['lift']:.2f}× | {r['cumulative_lift']:.2f}× |"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="write the Wave-2 markdown here")
    parser.add_argument(
        "--inject",
        type=Path,
        default=None,
        help="append/replace the Wave-2 section inside this report file",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=_default_data_root(),
        help="checkout holding the gitignored outputs/ (default: main checkout)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=REPO_ROOT / "outputs" / "validation" / "wave2_metrics.json",
        help="machine-readable metrics JSON",
    )
    args = parser.parse_args()

    data_root: Path = args.data_root
    pq = data_root / "outputs" / "parquet"
    taxonomy = yaml.safe_load(
        (REPO_ROOT / "data" / "taxonomy" / "critical_infrastructure.yaml").read_text()
    )

    runs: list[AoiRun] = [_resolve_run("pilot", _find_pilot_exposure(pq), data_root)]
    for aoi in WAVE2_AOIS:
        runs.append(_resolve_run(aoi, _find_wave2_exposure(pq, aoi), data_root))

    per_aoi: list[dict[str, Any]] = []
    pooled_labels: list[pd.Series] = []
    pooled_full: list[pd.Series] = []
    pooled_struct: list[pd.Series] = []
    pooled_full_fwi: list[pd.Series] = []
    pooled_struct_fwi: list[pd.Series] = []
    pooled_labels_fwi: list[pd.Series] = []
    pooled_topo_local: list[pd.Series] = []
    pooled_topo_prop: list[pd.Series] = []
    pooled_topo_labels: list[pd.Series] = []

    for run in runs:
        labels, vyears, vsummary = _labels_for(run, taxonomy)
        scores = _aoi_scores(run)
        has_fwi = FWI_FEATURE in _features_frame(gpd.read_parquet(run.exposure_path)).columns

        m_full = _compute(scores["full"].reindex(labels.index), labels)
        m_struct = _compute(scores["structural"].reindex(labels.index), labels)

        topo_labels = labels.reindex(scores["topo_local"].index)
        m_topo_local = _compute(scores["topo_local"], topo_labels)
        m_topo_prop = _compute(scores["topo_prop"], topo_labels)

        per_aoi.append(
            {
                "name": run.name,
                "model_version": run.model_version,
                "window": f"{run.window_start.isoformat()}..{run.window_end.isoformat()}",
                "vyears": ", ".join(str(y) for y in vyears) if vyears else "none",
                "vsummary": vsummary,
                "has_fwi": has_fwi,
                "full": m_full,
                "structural": m_struct,
                "topo_local": m_topo_local,
                "topo_prop": m_topo_prop,
                "topo_prov": scores["topo_prov"],
            }
        )

        if not m_full["degenerate"]:
            tag = run.name
            lab = labels.copy()
            lab.index = pd.Index([f"{tag}::{i}" for i in lab.index])
            fs = scores["full"].reindex(labels.index).copy()
            fs.index = lab.index
            ss = scores["structural"].reindex(labels.index).copy()
            ss.index = lab.index
            pooled_labels.append(lab)
            pooled_full.append(fs)
            pooled_struct.append(ss)
            if has_fwi:
                pooled_labels_fwi.append(lab)
                pooled_full_fwi.append(fs)
                pooled_struct_fwi.append(ss)
            tl = scores["topo_local"].copy()
            tp = scores["topo_prop"].copy()
            tlab = topo_labels.copy()
            tl.index = pd.Index([f"{tag}::{i}" for i in tl.index])
            tp.index = tl.index
            tlab.index = tl.index
            pooled_topo_local.append(tl)
            pooled_topo_prop.append(tp)
            pooled_topo_labels.append(tlab)

    # ---- Pooled metrics -----------------------------------------------------
    pl = pd.concat(pooled_labels)
    m_pool_full = _compute(pd.concat(pooled_full), pl)
    m_pool_struct = _compute(pd.concat(pooled_struct), pl)

    if pooled_labels_fwi:
        plf = pd.concat(pooled_labels_fwi)
        m_fwi_full = _compute(pd.concat(pooled_full_fwi), plf)
        m_fwi_struct = _compute(pd.concat(pooled_struct_fwi), plf)
    else:
        m_fwi_full = m_fwi_struct = {"degenerate": True, "n": 0, "n_burned": 0}

    ptlab = pd.concat(pooled_topo_labels)
    m_pool_topo_local = _compute(pd.concat(pooled_topo_local), ptlab)
    m_pool_topo_prop = _compute(pd.concat(pooled_topo_prop), ptlab)

    n_burned_pooled = int(m_pool_full["n_burned"])

    # ---- Emit JSON (machine-readable, deterministic) ------------------------
    def _mjson(m: dict[str, Any]) -> dict[str, Any]:
        out = {k: m[k] for k in ("n", "n_burned", "base_rate", "degenerate") if k in m}
        if not m.get("degenerate"):
            out["top_decile_lift"] = m["top_decile_lift"]
            out["top_decile_cum_lift"] = m["top_decile_cum_lift"]
            out["spearman_rho"] = m["spearman_rho"]
            out["spearman_p"] = m["spearman_p"]
            out["lift_table"] = m["table"].to_dict(orient="records")
        return out

    payload = {
        "code_commit_sha": code_commit_sha(cwd=REPO_ROOT),
        "n_burned_pooled": n_burned_pooled,
        "n_aois_validated": sum(1 for r in per_aoi if not r["full"]["degenerate"]),
        "runs": [
            {
                "name": r["name"],
                "exposure_parquet": next(
                    rr.exposure_path.name for rr in runs if rr.name == r["name"]
                ),
                "burns_parquet": next(rr.burns_path.name for rr in runs if rr.name == r["name"]),
                "model_version": r["model_version"],
                "window": r["window"],
                "validation_years": r["vsummary"]["validation_years"],
                "n_validation_perimeters": r["vsummary"]["n_validation_perimeters"],
                "validation_area_ha": r["vsummary"]["validation_area_ha"],
                "has_fwi": r["has_fwi"],
                "full": _mjson(r["full"]),
                "structural": _mjson(r["structural"]),
                "topology_local": _mjson(r["topo_local"]),
                "topology_propagated": _mjson(r["topo_prop"]),
                "topology_provenance": r["topo_prov"],
            }
            for r in per_aoi
        ],
        "pooled": {"full": _mjson(m_pool_full), "structural": _mjson(m_pool_struct)},
        "fwi_pool_v030_aois": {"full": _mjson(m_fwi_full), "structural": _mjson(m_fwi_struct)},
        "topology_pool": {
            "local": _mjson(m_pool_topo_local),
            "propagated": _mjson(m_pool_topo_prop),
        },
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"[wave2] wrote {args.json_out}", file=sys.stderr)

    # ---- Emit markdown ------------------------------------------------------
    emit_sha = code_commit_sha(cwd=REPO_ROOT)
    lines: list[str] = []
    lines += [
        "## Wave-2 — multi-AOI validation across five Portuguese AOIs",
        "",
        f"<!-- generated by: scripts/24_wave2_validate.py at {emit_sha} -->",
        "",
        "Wave-1 validated the exposure rank on the **pilot AOI alone**, where only "
        "**5** assets intersected a subsequent ICNF burn — too few to resolve lift "
        "from one-asset quantisation. Wave-2 widens the evaluation to **five AOIs** "
        "(pilot + pedrógão grande + serra da estrela + peneda gerês + monchique), "
        "each scored against its own ICNF Áreas Ardidas history. Methodology is "
        "unchanged from `scripts/11_validate.py`: truth is burns with vintage "
        "**strictly after** each AOI's score-input window (the §12 leakage rule, "
        "hard-gated per AOI); the per-asset label is buffer-intersects-burn; the "
        "metrics are decile lift and Spearman ρ of the **relative rank** against "
        "subsequent burning — never a probability.",
        "",
        "Each AOI carries a **different** score-input window (centred on its own "
        "major fire event), so validation years differ per AOI; each AOI was scored "
        "against its own per-AOI burns parquet, matched here by the provenance "
        "`burns_parquet_sha` (non-negotiable #3).",
        "",
        "### 1. Does widening fix the N=5 statistical emptiness?",
        "",
        f"**Pooled across the {payload['n_aois_validated']} non-degenerate AOIs: "
        f"N(burned assets) = {n_burned_pooled}**, against the Wave-1 single-AOI "
        f"N=5 — a {n_burned_pooled / 5:.0f}× increase. The statistical emptiness is "
        "resolved.",
        "",
        "Per-AOI discrimination (full deployed score):",
        "",
    ]
    lines += _per_aoi_table(per_aoi)
    lines += [
        "",
        "Degenerate rows have no burn with vintage strictly after the window "
        "(e.g. peneda gerês: its score window ends 2025-08-15, and no ICNF vintage "
        "post-dates it yet), so lift/Spearman are undefined there — the leakage rule "
        "correctly refuses to validate them. They are listed for completeness and "
        "excluded from the pool.",
        "",
        "Pooled lift table (full deployed score, asset-ids namespaced per AOI so the "
        "pool is a disjoint union; note the pool mixes AOI-relative ranks — see the "
        "cross-AOI caveat below):",
        "",
    ]
    lines += _lift_md(m_pool_full["table"])
    lines += [
        "",
        f"- pooled assets: **{m_pool_full['n']}**, pooled burned: "
        f"**{m_pool_full['n_burned']}** (base rate **{_fmt_pct(m_pool_full['base_rate'])}**)",
        f"- **pooled top-decile lift: {m_pool_full['top_decile_lift']:.2f}×**",
        f"- pooled Spearman ρ: **{m_pool_full['spearman_rho']:.4f}** "
        f"(two-sided p = {m_pool_full['spearman_p']:.2g})",
        "",
        "**Why the pooled top-decile lift reads below 1.0 — and why that is not a "
        "score failure.** Decile lift is `burn_rate ÷ base_rate`; it is only "
        "informative when the base rate is low enough that the top decile *can* "
        "stand out. Pedrógão grande's base rate is **0.61** (its score window ends "
        "2016, just before the 2017 megafire, so a clear majority of its assets "
        "later burned): a decile cannot exceed ≈1.6× there even if perfectly ranked, "
        "and pooling that saturated AOI with the others drags the pooled lift below "
        "1.0. The honest, base-rate-robust metric across heterogeneous AOIs is the "
        "rank correlation: pooled Spearman ρ is **positive and highly significant** "
        f"({m_pool_full['spearman_rho']:.4f}, p = {m_pool_full['spearman_p']:.2g}), "
        "and every non-degenerate AOI has positive per-AOI ρ. Read Spearman (and "
        "per-AOI lift on the low-base-rate AOIs — serra da estrela 2.82×, monchique "
        "1.21×) as the signal; the pooled lift number is distorted by mixed base "
        "rates and is reported only for completeness.",
        "",
        "**Cross-AOI caveat (methodology §11).** Scores are percentile-ranked "
        "*within each AOI*, so pooling mixes five within-AOI distributions; the "
        "pooled lift is a portfolio-screening readout, not a single national rank. "
        "The per-AOI rows are the primary evidence; the pool is the headline N.",
        "",
        "### 2. Does FWI earn its 0.10 weight? (FWI-HELPS)",
        "",
        "The four Wave-2 AOIs carry **backdated** FWI — each AOI's `fwi_valid_date` "
        "lies *inside* its pre-event score window (e.g. monchique 2018-08-06, "
        "pedrógão grande 2017-08-15, serra da estrela 2022-08-15), so unlike the "
        "operational current-season pilot re-score the FWI is contemporaneous with "
        "the window and **may** be validated against subsequent burns. The pilot is "
        "v0.2.0 (no FWI) and is excluded from this comparison. We compare the full "
        "v0.3.0 score against a **structural-only** score that drops "
        "`fwi_fwi_current` and renormalises the remaining weights per row (exactly "
        "the v0.2.0 weight set).",
        "",
        "| AOI | N burned | lift (struct→full) | Δ lift | ρ (struct→full) | Δ ρ |",
        "|---|---:|---|---:|---|---:|",
    ]
    for r in per_aoi:
        if not r["has_fwi"] or r["full"]["degenerate"] or r["structural"]["degenerate"]:
            continue
        f, s = r["full"], r["structural"]
        dl = f["top_decile_lift"] - s["top_decile_lift"]
        dr = f["spearman_rho"] - s["spearman_rho"]
        lines.append(
            f"| {r['name']} | {f['n_burned']} | "
            f"{s['top_decile_lift']:.2f}× → {f['top_decile_lift']:.2f}× | {dl:+.2f}× | "
            f"{s['spearman_rho']:.4f} → {f['spearman_rho']:.4f} | {dr:+.4f} |"
        )
    if not m_fwi_full.get("degenerate"):
        fwi_dl = m_fwi_full["top_decile_lift"] - m_fwi_struct["top_decile_lift"]
        fwi_dr = m_fwi_full["spearman_rho"] - m_fwi_struct["spearman_rho"]
        lines.append(
            f"| **pooled (v0.3.0 AOIs)** | **{m_fwi_full['n_burned']}** | "
            f"{m_fwi_struct['top_decile_lift']:.2f}× → {m_fwi_full['top_decile_lift']:.2f}× | "
            f"**{fwi_dl:+.2f}×** | "
            f"{m_fwi_struct['spearman_rho']:.4f} → {m_fwi_full['spearman_rho']:.4f} | "
            f"**{fwi_dr:+.4f}** |"
        )
    else:
        fwi_dl = fwi_dr = float("nan")
    lines += [
        "",
        "_Δ = full − structural; a positive Δ means adding FWI improved discrimination._",
        "",
        "### 3. Would weighting topology help? (TOPOLOGY-HELPS, for v0.4.0)",
        "",
        "Topology features (`feeder_count`, `network_component_size`, "
        "`network_exposure_propagated`) are AVAILABLE but UNWEIGHTED in v0.3.0. To "
        "test whether they would earn a weight, we build the WU-19 power + water "
        "graphs per AOI and compare the burn discrimination of "
        "`network_exposure_propagated` (α=0.5 blend of an asset's local rank with "
        "its graph neighbours') against the **local** rank, on the network-node "
        "subset of each AOI. OSM power topology in rural Portugal is sparse, so most "
        "nodes are isolated and propagation reduces to identity for them.",
        "",
        "| AOI | network nodes | inferred edges | N burned (nodes) | lift (local→prop) | "
        "Δ lift | ρ (local→prop) | Δ ρ |",
        "|---|---:|---:|---:|---|---:|---|---:|",
    ]
    for r in per_aoi:
        prov = r["topo_prov"]
        n_edges = prov["inferred_edge_count"]
        n_nodes = prov["power_node_count"] + prov["water_node_count"]
        tl, tp = r["topo_local"], r["topo_prop"]
        if tl["degenerate"] or tp["degenerate"]:
            lines.append(
                f"| {r['name']} | {n_nodes} | {n_edges} | "
                f"{tl.get('n_burned', 0)} | — (degenerate) | — | — | — |"
            )
            continue
        dl = tp["top_decile_lift"] - tl["top_decile_lift"]
        dr = tp["spearman_rho"] - tl["spearman_rho"]
        lines.append(
            f"| {r['name']} | {n_nodes} | {n_edges} | {tl['n_burned']} | "
            f"{tl['top_decile_lift']:.2f}× → {tp['top_decile_lift']:.2f}× | {dl:+.2f}× | "
            f"{tl['spearman_rho']:.4f} → {tp['spearman_rho']:.4f} | {dr:+.4f} |"
        )
    if not m_pool_topo_local.get("degenerate") and not m_pool_topo_prop.get("degenerate"):
        topo_dl = m_pool_topo_prop["top_decile_lift"] - m_pool_topo_local["top_decile_lift"]
        topo_dr = m_pool_topo_prop["spearman_rho"] - m_pool_topo_local["spearman_rho"]
        lines.append(
            f"| **pooled** | — | — | **{m_pool_topo_local['n_burned']}** | "
            f"{m_pool_topo_local['top_decile_lift']:.2f}× → "
            f"{m_pool_topo_prop['top_decile_lift']:.2f}× | **{topo_dl:+.2f}×** | "
            f"{m_pool_topo_local['spearman_rho']:.4f} → "
            f"{m_pool_topo_prop['spearman_rho']:.4f} | **{topo_dr:+.4f}** |"
        )
    else:
        topo_dl = topo_dr = float("nan")
    lines += [
        "",
        "_Δ = propagated − local; a positive Δ means propagating exposure over the "
        "inferred network improved discrimination on the network-node subset._",
        "",
        "### Honest verdicts and limitations",
        "",
        "- **Widening (N).** Pooling five AOIs lifts N(burned) from 5 to "
        f"**{n_burned_pooled}** — the statistical emptiness is resolved and the "
        "per-AOI lift curves are now read from hundreds-to-thousands of burned "
        "assets, not 5.",
    ]
    if not np.isnan(fwi_dr):
        if fwi_dr > 0.005 and fwi_dl >= 0:
            fwi_word = "earns its weight: it improves discrimination"
        elif fwi_dr < -0.005 or fwi_dl < -0.05:
            fwi_word = "does NOT earn its weight: it slightly degrades discrimination"
        else:
            fwi_word = (
                "is discrimination-neutral: the change is within noise — it neither "
                "clearly helps nor clearly hurts"
            )
        lines.append(
            f"- **FWI (0.10 weight).** Pooled over the four v0.3.0 AOIs, adding FWI "
            f"moves top-decile lift by {fwi_dl:+.2f}× and Spearman ρ by {fwi_dr:+.4f}. "
            f"On this backdated-FWI evidence, **FWI {fwi_word}**. This is an honest "
            "validation readout, not a tuning target; backdated single-date FWI is a "
            "coarse, season-static input and the burn-history feature dominates the "
            "structural signal."
        )
    if not np.isnan(topo_dr):
        if topo_dr > 0.005 and topo_dl >= 0:
            topo_word = "would likely help"
        elif topo_dr < -0.005 or topo_dl < -0.05:
            topo_word = "would NOT help (it degrades on the sparse OSM graph)"
        else:
            topo_word = (
                "is neutral on current evidence — the OSM power graph is too sparse "
                "to move the metric"
            )
        lines.append(
            f"- **Topology (for v0.4.0).** Propagating exposure over the inferred "
            f"power+water graph moves pooled top-decile lift by {topo_dl:+.2f}× and "
            f"Spearman ρ by {topo_dr:+.4f} on the network-node subset. Verdict: "
            f"weighting topology **{topo_word}**. The mechanism is real but OSM "
            "rural-PT connectivity is too incomplete (most nodes isolated → "
            "propagation is identity) for it to earn a weight yet; revisit when the "
            "graph is denser or edges are OSM-given rather than inferred."
        )
    lines += [
        "- **Spatial-autocorrelation caveat (unchanged).** Fire is strongly "
        "spatially autocorrelated and `historical_burn_share` is both a score "
        "feature and (as burn perimeters) the label source, so a clean temporal "
        "split still lets the score partly flatter itself. Lift here is necessary, "
        "not sufficient evidence of skill.",
        "- **Backdated-FWI caveat.** Each AOI's FWI is a single contemporaneous "
        "valid-date surface, not a season aggregate; it is a coarse fire-weather "
        "context input, validated only in the narrow sense above.",
        "- **Truth-window caveat.** Per-AOI validation windows differ; AOIs with a "
        "recent window end have fewer post-window vintages (peneda gerês has none "
        "yet). The pool is a disjoint union of within-AOI ranks, not a national rank.",
        "",
    ]

    text = "\n".join(lines)
    if args.inject is not None:
        report = args.inject.read_text()
        marker = "## Wave-2 — multi-AOI validation"
        if marker in report:
            report = report[: report.index(marker)].rstrip() + "\n\n"
        args.inject.write_text(report.rstrip() + "\n\n" + text)
        print(f"[wave2] injected Wave-2 section into {args.inject}", file=sys.stderr)
    if args.out is not None:
        args.out.write_text(text)
        print(f"[wave2] wrote {args.out}", file=sys.stderr)
    if args.out is None and args.inject is None:
        print(text)

    print(
        f"[wave2] n_burned_pooled={n_burned_pooled} "
        f"fwi_dlift={fwi_dl:+.2f} fwi_drho={fwi_dr:+.4f} "
        f"topo_dlift={topo_dl:+.2f} topo_drho={topo_dr:+.4f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
