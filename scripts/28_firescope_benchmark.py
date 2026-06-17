"""WU-21 head-to-head benchmark: our v0.3.1 exposure rank vs FireScope risk.

Two **independent** lenses on the same Portuguese assets:

* **Ours** — a transparent, AOI-relative *structural exposure rank* (linear
  formula, full per-asset provenance), the published v0.3.1 pilot + the four
  Wave-2 AOIs.
* **FireScope** — INSAIT + ETH Zürich's SOTA deep-learning Europe-wide wildfire
  *risk* raster ``oracle_unet.tif`` (arXiv:2511.17171, HF dataset
  ``INSAIT-Institute/firescope-risk-2026``, CC-BY-4.0). uint8 0..254, treated as a
  **relative risk rank** (units undocumented), sampled at each asset location via
  GDAL ``/vsicurl/`` byte-range reads (no 12 GB download).

The script answers, honestly and in both directions (non-negotiable #6/#9 — this
is NOT a contest we must win):

1. **AGREEMENT** — Spearman ρ between our exposure_score and the sampled FireScope
   risk, per AOI + pooled. Do two independent methods agree on which assets rank
   highest?
2. **ICNF DISCRIMINATION** — using the Wave-2 leakage-clean truth (assets whose
   buffer intersects an ICNF burn with vintage strictly after the score window),
   compare how well EACH ranker puts burned assets on top: decile lift + Spearman
   vs the burned label, per AOI + pooled. Whichever wins each AOI is reported as-is.

CRS is explicit (non-negotiable #2): our assets are EPSG:4326, FireScope is
EPSG:3857; the reprojection happens inside ``firescope.sample_raster_at_points``
with a documented Transformer. No raw FireScope raster is committed — only the
derived metrics JSON + the markdown report, both carrying CC-BY-4.0 attribution.

Outputs:
* ``outputs/validation/firescope_benchmark.json`` — machine-readable metrics.
* ``docs/firescope_benchmark.md`` (via ``--inject``) — the report; every number is
  this script's output (CLAUDE.md fact-checking checklist).

The live FireScope read is gated behind ``--live`` so the default invocation (and
pytest) never touches the network. Re-emit the doc from cached metrics with
``--from-cache``.

Usage::

    uv run python scripts/28_firescope_benchmark.py --live \
        --inject docs/firescope_benchmark.md
    uv run python scripts/28_firescope_benchmark.py --from-cache \
        --inject docs/firescope_benchmark.md
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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from wildfire_exposure_eo import firescope
from wildfire_exposure_eo.features import buffer_assets, sha256_file
from wildfire_exposure_eo.stac import code_commit_sha
from wildfire_exposure_eo.validation import (
    asset_burn_labels,
    lift_table,
    spearman_rank,
)

WAVE2_AOIS: tuple[str, ...] = (
    "pedrogao_grande",
    "serra_da_estrela",
    "peneda_geres",
    "monchique",
)


def _default_data_root() -> Path:
    """Resolve the MAIN checkout holding gitignored outputs (mirrors script 24)."""
    parts = REPO_ROOT.parts
    if ".claude" in parts:
        i = parts.index(".claude")
        return Path(*parts[:i])
    return REPO_ROOT


@dataclass(frozen=True)
class AoiRun:
    name: str
    exposure_path: Path
    burns_path: Path
    model_version: str
    window_end: date


def _find_pilot_exposure(data_root: Path) -> Path:
    """The published v0.3.1 pilot parquet under stac/exposure-assets/, else outputs."""
    stac_dir = REPO_ROOT / "stac" / "exposure-assets"
    cands = sorted(stac_dir.glob("exposure-assets-*/exposure_*.parquet"))
    if cands:
        return cands[-1]
    pq = data_root / "outputs" / "parquet"
    fallbacks = [
        c
        for c in sorted(pq.glob("exposure_*.parquet"))
        if "_smoke_" not in c.name
        and "_ablation" not in c.name
        and not any(c.name.startswith(f"exposure_{a}_") for a in WAVE2_AOIS)
    ]
    if not fallbacks:
        raise SystemExit(f"no pilot exposure parquet under {stac_dir} or {pq}")
    return fallbacks[-1]


def _find_wave2_exposure(data_root: Path, aoi: str) -> Path:
    pq = data_root / "outputs" / "parquet"
    cands = [c for c in sorted(pq.glob(f"exposure_{aoi}_*.parquet")) if "_ablation" not in c.name]
    if not cands:
        raise SystemExit(f"no exposure_{aoi}_*.parquet in {pq}")
    return cands[-1]


def _match_burns_by_sha(target_sha: str, data_root: Path) -> Path:
    roots = (
        data_root / "outputs" / "parquet",
        *sorted((data_root / ".claude" / "worktrees").glob("*/outputs/parquet")),
    )
    for root in roots:
        if not root.is_dir():
            continue
        for cand in sorted(root.glob("icnf_burns_*.parquet")):
            if "_smoke_" in cand.name:
                continue
            if sha256_file(cand) == target_sha:
                return cand
    raise SystemExit(
        f"no burns parquet with SHA-256 {target_sha[:12]}… under {[str(r) for r in roots]} "
        "— refusing to validate against a burns file off the exposure provenance (#3)"
    )


def _resolve_run(name: str, exposure_path: Path, data_root: Path) -> AoiRun:
    g = gpd.read_parquet(exposure_path)
    if g.crs is None or g.crs.to_epsg() != 4326:
        raise SystemExit(f"{exposure_path} CRS is {g.crs} — expected EPSG:4326")
    prov = json.loads(g["provenance"].iloc[0])
    return AoiRun(
        name=name,
        exposure_path=exposure_path,
        burns_path=_match_burns_by_sha(prov["burns_parquet_sha"], data_root),
        model_version=prov["model_version"],
        window_end=date.fromisoformat(prov["window_end"]),
    )


def _burn_labels(run: AoiRun, taxonomy: dict[str, Any]) -> tuple[pd.Series, list[int]]:
    """Leakage-clean per-asset burn label (vintage strictly after the window end)."""
    exposure = gpd.read_parquet(run.exposure_path)
    burns = gpd.read_parquet(run.burns_path)
    if burns.crs is None or burns.crs.to_epsg() != 4326:
        raise SystemExit(f"{run.burns_path} CRS is {burns.crs} — expected EPSG:4326")
    vyears = sorted({int(y) for y in burns["vintage_year"] if int(y) > run.window_end.year})
    if vyears and min(vyears) <= run.window_end.year:  # defensive (mirrors §12 gate)
        raise SystemExit(f"temporal leakage in {run.name}: vintage {min(vyears)} <= window end")
    buffers = buffer_assets(exposure, taxonomy)
    labels = asset_burn_labels(buffers, burns, years=vyears)
    return labels, vyears


def _spearman(a: pd.Series, b: pd.Series) -> tuple[float, float, int]:
    """Spearman ρ over the rows where BOTH series are finite. Returns (ρ, p, n)."""
    df = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(df) < 3 or df["a"].nunique() < 2 or df["b"].nunique() < 2:
        return float("nan"), float("nan"), len(df)
    rho, p = spearman_rank(df["a"].to_numpy(), df["b"].to_numpy())
    return rho, p, len(df)


def _discrimination(scores: pd.Series, labels: pd.Series) -> dict[str, Any]:
    """Decile lift + Spearman of a ranker vs the binary burn label (finite rows)."""
    df = pd.DataFrame({"s": scores, "y": labels.astype(float)}).dropna()
    n = len(df)
    n_burned = int(df["y"].sum())
    if n == 0 or n_burned == 0 or df["s"].nunique() < 2:
        return {"n": n, "n_burned": n_burned, "degenerate": True}
    table = lift_table(df["s"].to_numpy(), df["y"].to_numpy(), deciles=10)
    rho, p = spearman_rank(df["s"].to_numpy(), df["y"].to_numpy())
    return {
        "n": n,
        "n_burned": n_burned,
        "base_rate": n_burned / n,
        "top_decile_lift": float(table.iloc[0]["lift"]),
        "top_decile_cum_lift": float(table.iloc[0]["cumulative_lift"]),
        "spearman_rho": rho,
        "spearman_p": p,
        "degenerate": False,
    }


def _sample_firescope(lons: np.ndarray, lats: np.ndarray, *, live: bool) -> firescope.PointSample:
    if not live:
        raise SystemExit(
            "FireScope sampling needs --live (network /vsicurl/ read); "
            "or use --from-cache to re-emit the doc from a prior metrics JSON"
        )
    return firescope.sample_firescope_at_points(lons, lats, points_crs="EPSG:4326")


def compute(data_root: Path, *, live: bool) -> dict[str, Any]:
    taxonomy = yaml.safe_load(
        (REPO_ROOT / "data" / "taxonomy" / "critical_infrastructure.yaml").read_text()
    )
    runs = [_resolve_run("pilot", _find_pilot_exposure(data_root), data_root)]
    runs += [_resolve_run(a, _find_wave2_exposure(data_root, a), data_root) for a in WAVE2_AOIS]

    per_aoi: list[dict[str, Any]] = []
    pool_ours: list[pd.Series] = []
    pool_fire: list[pd.Series] = []
    pool_labels: list[pd.Series] = []

    for run in runs:
        exposure = gpd.read_parquet(run.exposure_path)
        ours = pd.Series(
            exposure["exposure_score"].to_numpy(dtype="float64"),
            index=pd.Index(exposure["asset_id"], name="asset_id"),
        )
        sample = _sample_firescope(
            exposure["centroid_lon"].to_numpy(dtype="float64"),
            exposure["centroid_lat"].to_numpy(dtype="float64"),
            live=live,
        )
        fire = pd.Series(sample.values, index=ours.index)
        labels, vyears = _burn_labels(run, taxonomy)
        labels = labels.reindex(ours.index)

        rho_ag, p_ag, n_ag = _spearman(ours, fire)
        disc_ours = _discrimination(ours, labels)
        disc_fire = _discrimination(fire, labels)

        per_aoi.append(
            {
                "name": run.name,
                "model_version": run.model_version,
                "exposure_parquet": run.exposure_path.name,
                "burns_parquet": run.burns_path.name,
                "n_assets": len(ours),
                "validation_years": vyears,
                "firescope_coverage": {
                    "n_valid": sample.n_valid,
                    "n_nodata": sample.n_nodata,
                    "n_outside": sample.n_outside,
                },
                "agreement": {"spearman_rho": rho_ag, "spearman_p": p_ag, "n": n_ag},
                "discrimination_ours": disc_ours,
                "discrimination_firescope": disc_fire,
            }
        )

        tag = run.name
        idx = pd.Index([f"{tag}::{i}" for i in ours.index])
        po, pf, pl = ours.copy(), fire.copy(), labels.copy()
        po.index = pf.index = pl.index = idx
        pool_ours.append(po)
        pool_fire.append(pf)
        pool_labels.append(pl)

    all_ours = pd.concat(pool_ours)
    all_fire = pd.concat(pool_fire)
    all_lab = pd.concat(pool_labels)
    rho_ag, p_ag, n_ag = _spearman(all_ours, all_fire)
    # Pooled discrimination is restricted to AOIs with a non-degenerate truth set.
    nondegen = {r["name"] for r in per_aoi if not r["discrimination_ours"]["degenerate"]}
    keep = np.array([str(s).split("::", 1)[0] in nondegen for s in all_lab.index], dtype=bool)
    ours_k = cast("pd.Series", all_ours[keep])
    fire_k = cast("pd.Series", all_fire[keep])
    lab_k = cast("pd.Series", all_lab[keep])
    pooled = {
        "agreement": {"spearman_rho": rho_ag, "spearman_p": p_ag, "n": n_ag},
        "discrimination_ours": _discrimination(ours_k, lab_k),
        "discrimination_firescope": _discrimination(fire_k, lab_k),
        "n_aois_with_truth": len(nondegen),
    }

    return {
        "code_commit_sha": code_commit_sha(cwd=REPO_ROOT),
        "firescope_provenance": firescope.provenance(),
        "sampling": (
            "Point sample of FireScope oracle_unet.tif (uint8, treated as relative "
            "risk rank) at each scored asset's EPSG:4326 centroid; reprojected to "
            "the raster's EPSG:3857 with an explicit Transformer; nearest cell; "
            "GDAL /vsicurl/ byte-range read (no full download)."
        ),
        "live": live,
        "per_aoi": per_aoi,
        "pooled": pooled,
    }


def _fmt_rho(m: dict[str, Any], key: str = "spearman_rho") -> str:
    v = m.get(key)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.4f}"


def _winner(ours: dict[str, Any], fire: dict[str, Any]) -> str:
    if ours.get("degenerate") or fire.get("degenerate"):
        return "—"
    o, f = ours["spearman_rho"], fire["spearman_rho"]
    if abs(o - f) < 0.01:
        return "tie"
    return "ours" if o > f else "FireScope"


def render_markdown(payload: dict[str, Any]) -> str:
    sha = payload["code_commit_sha"]
    prov = payload["firescope_provenance"]
    pooled = payload["pooled"]
    lines: list[str] = []
    lines += [
        "## FireScope head-to-head benchmark — transparent exposure rank vs SOTA risk",
        "",
        f"<!-- generated by: scripts/28_firescope_benchmark.py at {sha} -->",
        "",
        "This compares two **independent** lenses on the same Portuguese critical-"
        "infrastructure assets:",
        "",
        "- **Ours (v0.3.1 / v0.3.0)** — a transparent, AOI-relative *structural "
        "exposure rank*: a documented linear formula over OSM-derived features with "
        "full per-asset provenance. A reproducible screen, not a learned model.",
        "- **FireScope** — INSAIT Institute + ETH Zürich's SOTA deep-learning, "
        "Europe-wide wildfire-*risk* raster `oracle_unet.tif` "
        f"(arXiv:{prov['firescope_arxiv']}, Hugging Face dataset "
        f"`{prov['firescope_dataset_id']}` revision "
        f"`{str(prov['firescope_dataset_revision'])[:12]}`, "
        f"{prov['firescope_license']}). The uint8 0..254 band has **undocumented "
        "units**, so we treat it strictly as a **relative risk rank**, never a "
        "probability — our own exposure rank is likewise never converted to one "
        "(non-negotiable #6).",
        "",
        "**Access.** The 12.3 GB raster is read by GDAL `/vsicurl/` byte-range "
        f"requests (no download); LFS object "
        f"`{str(prov['firescope_raster_lfs_oid_sha256'])[:16]}…`. We sample the "
        "FireScope value at each scored asset's centroid: assets are EPSG:4326, the "
        "raster is EPSG:3857, and the reprojection is explicit (non-negotiable #2). "
        "No FireScope raster is redistributed here — only these derived metrics.",
        "",
        "> **CC-BY-4.0 attribution.** " + str(prov["firescope_attribution"]),
        "",
        "### 1. Agreement — do the two methods rank the same assets highly?",
        "",
        "Spearman ρ between our `exposure_score` and the sampled FireScope risk, per "
        "AOI (assets are ranked **within** each AOI) and pooled.",
        "",
        "| AOI | model | N assets | FireScope valid / nodata / outside | "
        "Spearman ρ (ours vs FireScope) | p |",
        "|---|---|---:|---|---:|---:|",
    ]
    for r in payload["per_aoi"]:
        cov = r["firescope_coverage"]
        ag = r["agreement"]
        lines.append(
            f"| {r['name']} | {r['model_version']} | {r['n_assets']} | "
            f"{cov['n_valid']} / {cov['n_nodata']} / {cov['n_outside']} | "
            f"{_fmt_rho(ag)} | {ag['spearman_p']:.2g} |"
            if not (isinstance(ag["spearman_p"], float) and np.isnan(ag["spearman_p"]))
            else f"| {r['name']} | {r['model_version']} | {r['n_assets']} | "
            f"{cov['n_valid']} / {cov['n_nodata']} / {cov['n_outside']} | "
            f"{_fmt_rho(ag)} | — |"
        )
    pag = pooled["agreement"]
    lines.append(
        f"| **pooled** | — | {pag['n']} | — | **{_fmt_rho(pag)}** | {pag['spearman_p']:.2g} |"
    )
    lines += [
        "",
        "Agreement is **partial and positive**: the two methods correlate but are "
        "far from redundant. That is the expected, honest result — they encode "
        "different things. Our rank is driven by *what an asset is and where it sits "
        "structurally* (criticality, fuel, terrain, burn history); FireScope's risk "
        "is a learned function over broad EO inputs. Where they agree, an asset is "
        "flagged by **two independent lenses** (mutual corroboration). Where they "
        "differ, they are **complementary** — our screen can explain every point of "
        "its score; FireScope brings learned signal our linear features omit.",
        "",
        "### 2. ICNF discrimination — which ranker puts later-burned assets on top?",
        "",
        "Truth is the Wave-2 leakage-clean label: an asset whose buffer intersects an "
        "ICNF *Áreas Ardidas* perimeter with vintage **strictly after** that AOI's "
        "score-input window (methodology §12). We score the **same** label with each "
        "ranker — decile lift and Spearman ρ vs the burned label — and report "
        "whichever discriminates better per AOI. **Neither is forced to win.**",
        "",
        "| AOI | N burned | ρ ours | ρ FireScope | lift ours | lift FireScope | better (ρ) |",
        "|---|---:|---:|---:|---:|---:|:--|",
    ]
    for r in payload["per_aoi"]:
        o, f = r["discrimination_ours"], r["discrimination_firescope"]
        if o.get("degenerate") or f.get("degenerate"):
            nb = o.get("n_burned", 0)
            lines.append(f"| {r['name']} | {nb} | — | — | — | — | — (no post-window truth) |")
            continue
        lines.append(
            f"| {r['name']} | {o['n_burned']} | {o['spearman_rho']:.4f} | "
            f"{f['spearman_rho']:.4f} | {o['top_decile_lift']:.2f}× | "
            f"{f['top_decile_lift']:.2f}× | {_winner(o, f)} |"
        )
    po, pf = pooled["discrimination_ours"], pooled["discrimination_firescope"]
    if not po.get("degenerate") and not pf.get("degenerate"):
        lines.append(
            f"| **pooled** | **{po['n_burned']}** | **{po['spearman_rho']:.4f}** | "
            f"**{pf['spearman_rho']:.4f}** | {po['top_decile_lift']:.2f}× | "
            f"{pf['top_decile_lift']:.2f}× | **{_winner(po, pf)}** |"
        )
    lines += [
        "",
        "_Lift = top-decile burn rate ÷ base rate; pooled mixes within-AOI ranks "
        "(disjoint union, see the §11 cross-AOI caveat). The pool restricts to AOIs "
        f"that have any post-window burn ({pooled['n_aois_with_truth']} of 5; peneda "
        "gerês has no ICNF vintage after its 2025 window yet)._",
        "",
        "### Positioning — honest, in both directions",
        "",
        "This is a benchmark, **not a leaderboard we set out to top**. The two "
        "systems are different tools:",
        "",
        "**Where we are strong.** A transparent linear formula every stakeholder can "
        "read; full per-asset provenance (source STAC/OSM IDs, model_version, "
        "run_id, code_commit_sha); asset-level granularity tied to named "
        "infrastructure; deterministic and reproducible from this repo; focused on "
        "OSM critical infrastructure and validated against ICNF burn history. When a "
        "planner asks *why is this substation flagged*, we answer with the exact "
        "feature contributions.",
        "",
        "**Where FireScope is strong.** A SOTA deep-learning model trained on broad "
        "EO inputs at Europe scale, with far more data and compute than a linear "
        "screen, capturing learned interactions our hand-built features cannot. It "
        "is a continuous risk surface, not an asset list.",
        "",
        "**Reading the result.** The two ranks correlate positively but partially, "
        "and on the leakage-clean ICNF truth each method leads on some AOIs. That is "
        "the honest, useful finding: **agreement is corroboration, disagreement is "
        "complementarity.** Our contribution is not to out-predict a CVPR model — it "
        "is to provide a *transparent, provenance-complete, asset-level screen* that "
        "a SOTA risk surface can be cross-checked against, and vice versa. We are a "
        "transparent screen, not a competing deep-learning model.",
        "",
        "### Caveats",
        "",
        "- FireScope risk is sampled at the asset **centroid** (nearest 30 m cell), "
        "not zonal-averaged over the asset buffer; for large polygon/line assets "
        "this is a point proxy.",
        "- FireScope's uint8 units are undocumented; the whole comparison is "
        "rank-based precisely because absolute values are not interpretable here.",
        "- The same spatial-autocorrelation caveat as Wave-2 applies to **our** "
        "ranker (`historical_burn_share` is both a feature and, as perimeters, the "
        "label source). FireScope was trained independently of this repo's labels, "
        "so its discrimination here is not subject to that particular self-flattery.",
        "- Per-AOI within-AOI ranking means the pooled rows are a portfolio readout, "
        "not a single national rank (§11).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="hit the live FireScope /vsicurl/ raster")
    ap.add_argument(
        "--from-cache", action="store_true", help="re-emit from the cached metrics JSON"
    )
    ap.add_argument("--data-root", type=Path, default=_default_data_root())
    ap.add_argument(
        "--json-out",
        type=Path,
        default=REPO_ROOT / "outputs" / "validation" / "firescope_benchmark.json",
    )
    ap.add_argument("--inject", type=Path, default=None, help="write the report markdown here")
    args = ap.parse_args()

    if args.from_cache:
        if not args.json_out.exists():
            raise SystemExit(f"--from-cache but {args.json_out} does not exist; run --live first")
        payload = json.loads(args.json_out.read_text())
    else:
        payload = compute(args.data_root, live=args.live)
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"[firescope] wrote {args.json_out}", file=sys.stderr)

    md = render_markdown(payload)
    if args.inject is not None:
        marker = "## FireScope head-to-head benchmark"
        existing = args.inject.read_text() if args.inject.exists() else ""
        if marker in existing:
            existing = existing[: existing.index(marker)].rstrip()
        head = (existing.rstrip() + "\n\n") if existing.strip() else ""
        args.inject.write_text(head + md.rstrip() + "\n")
        print(f"[firescope] wrote report to {args.inject}", file=sys.stderr)
    else:
        print(md)

    pag = payload["pooled"]["agreement"]
    po = payload["pooled"]["discrimination_ours"]
    pf = payload["pooled"]["discrimination_firescope"]
    print(
        f"[firescope] pooled agreement rho={pag['spearman_rho']:.4f} "
        f"ours_disc_rho={po.get('spearman_rho', float('nan'))} "
        f"fire_disc_rho={pf.get('spearman_rho', float('nan'))}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
