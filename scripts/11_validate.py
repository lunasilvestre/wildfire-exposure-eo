"""Validate the exposure rank against subsequent ICNF burns (WU-7, prompt 11).

Runs the full evaluation and **emits the markdown** of ``docs/validation_report.md``
verbatim — every number in the report is this script's output (CLAUDE.md
fact-checking checklist). The WU-1 audit-script pattern:

    uv run python scripts/11_validate.py \
        --exposure outputs/parquet/exposure_<backdated_run>.parquet \
        --out docs/validation_report.md

The exposure parquet is **self-describing**: the score-input window, model
version, run id and commit come from the per-row provenance struct, never from
CLI guesses. Validation years are every ICNF vintage strictly after the window
end (the methodology §12 temporal-leakage rule, enforced by
``validation.assert_no_temporal_leakage``).

The score is a *relative screening rank*, never a calibrated probability — lift
and Spearman measure only whether higher-ranked assets burned more often in
later years. The mandatory burn-history ablation (plan caveat #1) recomputes the
identical evaluation with the burn-history features removed, so the report can
say plainly which features carry the signal. Determinism: the script embeds no
runtime clock; two runs over the same inputs produce byte-identical output.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, cast

import geopandas as gpd
import pandas as pd

# Repo-root import shim so the script runs from anywhere (matches scripts/09_*).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wildfire_exposure_eo.features import DateRange, buffer_assets
from wildfire_exposure_eo.schemas.scored_asset import FEATURE_NAMES
from wildfire_exposure_eo.scoring import compose_exposure, load_exposure_config
from wildfire_exposure_eo.validation import (
    assert_no_temporal_leakage,
    asset_burn_labels,
    lift_table,
    spearman_rank,
)

#: Burn-history features removed in the ablation variant (plan caveat #1).
BURN_HISTORY_FEATURES = ("historical_burn_share", "recent_burn_share_12mo")


def _latest(folder: Path, prefix: str, suffix: str, *, smoke: bool) -> Path:
    """Newest ``{prefix}[_smoke]_*{suffix}`` in ``folder`` (timestamps sort lexically)."""
    pattern = f"{prefix}_smoke_*{suffix}" if smoke else f"{prefix}_*{suffix}"
    cands = sorted(folder.glob(pattern))
    if not smoke:
        cands = [c for c in cands if "_smoke_" not in c.name]
    if not cands:
        raise SystemExit(f"no {prefix}*{suffix} artefact in {folder} (run the relevant WU first)")
    return cands[-1]


def _features_frame(exposure: gpd.GeoDataFrame) -> pd.DataFrame:
    """Reconstruct the per-asset features DataFrame from the parquet ``features`` JSON."""
    rows = [json.loads(s) for s in exposure["features"]]
    df = pd.DataFrame(rows, index=pd.Index(exposure["asset_id"], name="asset_id"))
    # Keep only known feature columns, in canonical order, NaN where absent/null.
    cols = [c for c in FEATURE_NAMES if c in df.columns]
    return cast("pd.DataFrame", df[cols])


def _compute(scores: pd.Series, labels: pd.Series) -> dict[str, Any]:
    """Lift table + Spearman for an aligned (scores, labels) pair. Handles base-rate 0."""
    n = len(scores)
    n_burned = int(labels.sum())
    base_rate = n_burned / n if n else float("nan")
    if n_burned == 0:
        return {"n": n, "n_burned": 0, "base_rate": base_rate, "degenerate": True}
    table = lift_table(scores.to_numpy(), labels.to_numpy().astype(float), deciles=10)
    rho, p = spearman_rank(scores.to_numpy(), labels.to_numpy().astype(float))
    return {
        "n": n,
        "n_burned": n_burned,
        "base_rate": base_rate,
        "table": table,
        "top_decile_lift": float(table.iloc[0]["lift"]),
        "spearman_rho": rho,
        "spearman_p": p,
        "degenerate": False,
    }


def _lift_table_md(table: pd.DataFrame) -> list[str]:
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


def _metrics_block(title: str, m: dict[str, Any], features: list[str]) -> list[str]:
    feats = ", ".join(f"`{f}`" for f in features) if features else "(none)"
    lines = [f"### {title}", "", f"Active features: {feats}.", ""]
    if m["degenerate"]:
        lines += [
            f"- assets evaluated: **{m['n']}**",
            f"- burned (validation years): **{m['n_burned']}**",
            f"- base rate: **{m['base_rate']:.5f}**",
            "",
            "No validation-year ICNF burn intersects any asset buffer in this AOI, so "
            "lift and Spearman are undefined. Re-run on the pilot AOI (where subsequent "
            "burns exist) for the load-bearing numbers; the smoke AOI exercises the "
            "evaluation path only.",
            "",
        ]
        return lines
    lines += [
        f"- assets evaluated: **{m['n']}**",
        f"- burned within validation years: **{m['n_burned']}**  "
        f"(base rate **{m['base_rate']:.5f}**)",
        f"- **top-decile lift: {m['top_decile_lift']:.2f}×**",
        f"- Spearman ρ (rank vs. subsequent burning): **{m['spearman_rho']:.4f}** "
        f"(two-sided p = {m['spearman_p']:.3g})",
        "",
        *_lift_table_md(m["table"]),
        "",
    ]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exposure", type=Path, default=None, help="backdated exposure parquet")
    parser.add_argument("--burns", type=Path, default=None, help="ICNF burns parquet")
    parser.add_argument("--fuel-cog", type=Path, default=None, help="WU-5 fuel COG (for sidecar)")
    parser.add_argument(
        "--taxonomy", type=Path, default=Path("data/taxonomy/critical_infrastructure.yaml")
    )
    parser.add_argument("--exposure-config", type=Path, default=Path("config/exposure_score.yaml"))
    parser.add_argument("--out", type=Path, default=None, help="write the markdown here")
    parser.add_argument("--smoke", action="store_true", help="use smoke-scale default artefacts")
    args = parser.parse_args()

    pq = Path("outputs/parquet")
    cogs = Path("outputs/cogs")
    exposure_path = args.exposure or _latest(pq, "exposure", ".parquet", smoke=args.smoke)
    burns_path = args.burns or _latest(pq, "icnf_burns", ".parquet", smoke=args.smoke)
    fuel_path = args.fuel_cog or _latest(cogs, "fuel_class", ".tif", smoke=args.smoke)

    import yaml

    taxonomy = yaml.safe_load(args.taxonomy.read_text())
    config = load_exposure_config(args.exposure_config)

    exposure = gpd.read_parquet(exposure_path)
    if exposure.crs is None or exposure.crs.to_epsg() != 4326:
        raise SystemExit(f"{exposure_path} CRS is {exposure.crs} — expected EPSG:4326")
    prov = json.loads(exposure["provenance"].iloc[0])
    score_window = DateRange(
        date.fromisoformat(prov["window_start"]), date.fromisoformat(prov["window_end"])
    )
    model_version = prov["model_version"]
    run_id = prov["run_id"]
    commit = prov["code_commit_sha"]

    burns = gpd.read_parquet(burns_path)
    if burns.crs is None or burns.crs.to_epsg() != 4326:
        raise SystemExit(f"{burns_path} CRS is {burns.crs} — expected EPSG:4326")
    validation_years = sorted(
        {int(y) for y in burns["vintage_year"] if int(y) > score_window.end.year}
    )
    validation_burns = cast("gpd.GeoDataFrame", burns[burns["vintage_year"].isin(validation_years)])

    # Hard leakage gate (§12): every validation burn strictly after the window.
    assert_no_temporal_leakage(score_window, validation_burns)

    # Buffer assets once (EPSG:32629), label by overlay against validation-year burns.
    buffers = buffer_assets(exposure, taxonomy)
    labels = asset_burn_labels(buffers, burns, years=validation_years)

    # Align product scores (from the parquet) to labels by asset_id.
    scores_full = cast(
        "pd.Series", exposure.set_index("asset_id")["exposure_score"].reindex(labels.index)
    )

    # Ablation: recompute the rank with the burn-history features removed.
    feats = _features_frame(exposure)
    present_full = [c for c in FEATURE_NAMES if c in feats.columns]
    feats_ablation = feats.drop(columns=[c for c in BURN_HISTORY_FEATURES if c in feats.columns])
    present_ablation = [c for c in FEATURE_NAMES if c in feats_ablation.columns]
    composed_ablation = compose_exposure(feats_ablation, config)
    scores_ablation = cast("pd.Series", composed_ablation["exposure_score"].reindex(labels.index))

    # The ablation parquet is an analysis artefact only — outputs/ (gitignored),
    # never a product output. Deterministic name from the source run id.
    tag = "_smoke" if args.smoke else ""
    abl_path = pq / f"exposure_ablation{tag}_{run_id}.parquet"
    composed_ablation.reset_index()[["asset_id", "exposure_score", "exposure_rank"]].to_parquet(
        abl_path, compression="snappy", index=False
    )

    m_full = _compute(scores_full, labels)
    m_ablation = _compute(scores_ablation, labels)

    # Fuel scale-mismatch caveat (#3): read the effective EFFIS resolution honestly.
    fuel_sidecar = json.loads(fuel_path.with_suffix(".json").read_text())
    effis_res = fuel_sidecar.get("effis_native_res_m")
    effis_vintage = fuel_sidecar.get("effis_vintage")
    cosc_res = fuel_sidecar.get("cosc_native_res_m")

    print(
        f"[validate] exposure={exposure_path.name} window={score_window.start}..{score_window.end} "
        f"validation_years={validation_years} n={m_full['n']} burned={m_full['n_burned']}",
        file=sys.stderr,
    )

    lines: list[str] = [
        "# Exposure-rank validation against subsequent ICNF burns",
        "",
        f"<!-- generated by: scripts/11_validate.py at {commit} -->",
        "",
        "This report validates a **relative screening rank**, not a probability of "
        "fire. The exposure score orders assets within the evaluated AOI by modelled "
        "wildfire exposure; the question here is the narrow, falsifiable one: did "
        "higher-ranked assets burn more often in years **strictly after** the "
        "score-input window? Lift and Spearman answer that and nothing more — there "
        "is no calibration claim and no forecast-skill claim.",
        "",
        "## Temporal-leakage rule (methodology §12)",
        "",
        f"Score-input window: inputs **≤ {score_window.end.isoformat()}** "
        f"(trailing window {score_window.start.isoformat()} .. {score_window.end.isoformat()}). "
        f"Validation labels are ICNF Áreas Ardidas perimeters with vintage strictly "
        f"after the window end — vintage year(s) **{validation_years or 'none'}**. The "
        "hard gate `validation.assert_no_temporal_leakage` raises unless every "
        "validation burn post-dates the window; it passed for this run.",
        "",
        f"Configuration `exposure_score.yaml` v{model_version} (run `{run_id}`). Active "
        f"features in the validated run: {', '.join(f'`{c}`' for c in present_full)}. "
        "Note `recent_burn_share_12mo` is **absent**: the fixed-window Prithvi "
        "burn-scar COG composites scenes from the recent season only, which does not "
        "overlap a backdated score window without leaking post-window observations — "
        "so the feature is correctly dropped for this backdated run (the scoring code "
        "nulls it rather than leak). `fwi_p95_recent_season` was dropped project-wide "
        "in WU-6 (no GREEN public programmatic FWI source verifiable in-session; see "
        "the `exposure_score.yaml` v0.2.0 changelog).",
        "",
        "## Results",
        "",
    ]
    lines += _metrics_block("Full validated configuration", m_full, present_full)
    lines += _metrics_block(
        "Ablation — burn-history features removed", m_ablation, present_ablation
    )

    lines += [
        "## Which features carry the signal (plan caveat #1)",
        "",
    ]
    if m_full["degenerate"] or m_ablation["degenerate"]:
        lines += [
            "Undefined for this run — no validation-year burn intersects an asset "
            "buffer (see above). The ablation comparison is reportable only on an AOI "
            "with subsequent burns.",
            "",
        ]
    else:
        d_lift = m_full["top_decile_lift"] - m_ablation["top_decile_lift"]
        d_rho = m_full["spearman_rho"] - m_ablation["spearman_rho"]
        carries = (
            "the burn-history features" if d_lift > 0 or d_rho > 0 else "the non-burn features"
        )
        lines += [
            f"Removing the burn-history features changes top-decile lift by "
            f"**{d_lift:+.2f}×** ({m_full['top_decile_lift']:.2f}× → "
            f"{m_ablation['top_decile_lift']:.2f}×) and Spearman ρ by **{d_rho:+.4f}** "
            f"({m_full['spearman_rho']:.4f} → {m_ablation['spearman_rho']:.4f}). The "
            f"signal is carried substantially by {carries}.",
            "",
            "**This is necessary, not sufficient.** Fire is strongly spatially "
            "autocorrelated, so a clean temporal split still lets the score flatter "
            "itself: an asset near a past burn is more likely to sit near a future one "
            "regardless of the score. `historical_burn_share` is a score feature *and* "
            "the validation labels are burn perimeters, so any lift it produces is "
            "partly mechanical. The ablation row is the honest number — read it first.",
            "",
        ]

    if not isinstance(effis_res, int | float) or not isinstance(cosc_res, int | float):
        raise SystemExit(
            f"{fuel_path.with_suffix('.json')} missing effis_native_res_m/cosc_native_res_m "
            "— refusing to invent resolutions for the scale-mismatch caveat"
        )
    res_txt = f"{effis_res:.0f} m"
    cosc_txt = f"{cosc_res:.0f} m"
    lines += [
        "## Fuel-map scale mismatch (plan caveat #3)",
        "",
        f"The fuel layer's EFFIS source (vintage {effis_vintage}) has an effective "
        f"native resolution of **{res_txt}** (from the WU-5 fuel COG sidecar), "
        f"resampled onto the 10 m working grid and refined by DGT COSc land cover at "
        f"{cosc_txt}. EFFIS fuel classes are therefore coarse relative to the 20–100 m "
        "asset buffers: the fuel feature reflects a neighbourhood fuel character, "
        "**not parcel-level fuel precision**. COSc refinement only partially "
        "compensates. Treat `fuel_class_severity_weight` as a coarse contextual input.",
        "",
        "## Scope boundary (methodology §11)",
        "",
        "Scores are **AOI-relative**: each feature is percentile-ranked within the "
        'evaluated AOI before weighting, so an exposure score answers "where does this '
        "asset sit in *this AOI's* distribution?\" It is **not comparable across "
        "AOIs**. A national rollout would need a national reference distribution. This "
        "is a documented scope boundary, not a defect.",
        "",
        "## On calibration and the Brier score",
        "",
        "`exposure_score.yaml` sets `calibration.method: report_only` — we report "
        "validation against historical burns and make no calibrated-probability "
        "promise. The Brier score listed in that file's `metrics` is **deliberately "
        "omitted here**: Brier presumes a probability forecast, and the exposure score "
        "is a relative rank. Reporting it would imply a calibration claim this project "
        "does not make (CLAUDE.md non-negotiable #6). The metric list in the YAML and "
        "this omission are noted together so they do not silently disagree.",
        "",
        "## The defensible claim",
        "",
        "*A transparent, reproducible prioritization screen validated against "
        "subsequent burns* — nothing stronger. Every number above is emitted by "
        "`scripts/11_validate.py`; re-running it over the same inputs reproduces this "
        "file byte-for-byte.",
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
