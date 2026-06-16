# Strategy & Positioning Brief — `wildfire-exposure-eo`

> Working document, 2026-06-14. Not a methodology spec, not a public-surface
> change. Purpose: decide how this repo earns its place as a portfolio piece,
> where it sits next to the Portuguese fire-risk landscape (DGT / IPMA / ICNF /
> MEJOR-LandOS), which open-source models close its biggest gap, and what to
> build next. Audience this brief optimises for: **technical hiring managers
> and prospective clients.** Every external fact is sourced at the bottom.

---

## 1. The thesis in one paragraph

Everyone in the Portuguese fire-risk space maps **the land**. DGT maps
structural hazard per 25 m cell. IPMA maps daily fire weather. MEJOR's LUCI
(surfaced through LandOS / *A Minha Terra*) maps seasonal susceptibility per
parcel. FireScope maps a Europe-wide ML risk raster. **Nobody ranks the things
*on* the land that somebody is paid to protect** — the substations, schools,
water plants, fire stations and distribution lines. That is the wedge this repo
already occupies. The one-line positioning: **"We don't map the fire. We rank
the infrastructure the fire would hit."** Hold that line and the project stops
competing with national agencies and starts being the asset layer that sits on
top of them.

---

## 2. The competitive landscape (who maps fire risk in Portugal, and how)

| Product | Operator | Unit of analysis | Horizon | Method | Access |
|---|---|---|---|---|---|
| Carta de Perigosidade de Incêndio Rural | DGT + ICNF (built by IGOT + PahlConsulting, POSEUR-INCENDIO) | 25 m raster cell | Structural, multi-year (2020–2030) | Bayesian likelihood-ratio on 1975–2018 burns + slope/altitude/COS-L5; **hazard = probability × susceptibility**; 5 classes | Vector download + WMS/WFS, attribution |
| RCM "Perigo de Incêndio Rural" + FWI | IPMA | Station → grid | **Daily**, forecast to ~H+120 | Canadian FWI System (FFMC/DMC/DC/ISI/BUI/FWI) × ICNF hazard matrix | MF2 dataservices + viewers |
| Áreas Ardidas | ICNF | Burn perimeter polygon | Annual (historical record) | Satellite + ground survey mapping | Shapefile / WMS/WFS |
| LUCI susceptibility → LandOS / *A Minha Terra* | **MEJOR Technologies** (model) + **Aminhaterra, Lda.** (platform) | 10 m cell, surfaced per parish/parcel | **Seasonal** (~monthly retrain) | ML susceptibility "before ignition": ERA5 + topography + vegetation/fuel load + land-cover change + recent burns | Free viewer; landowner workflow |
| FireScope risk map | INSAIT (academic, CVPR 2026) | Raster tile (Europe) | Structural / long-term | Vision-language model + Sentinel-2 + climatology, CC-BY-4.0 | HF dataset + firescope.ai |
| **`wildfire-exposure-eo`** | **this repo** | **Critical-infrastructure asset + class buffer** | Structural now; seasonal pending | Transparent linear score over EO features; **per-asset provenance**; validated vs *subsequent* ICNF burns | MIT repo + STAC + GeoParquet + geobrowser |

Read the "unit of analysis" column top to bottom. Five products resolve to a
**cell, parcel, or polygon**. One resolves to an **asset**. That column is the
entire competitive argument — keep it visible in every version of the pitch.

---

## 3. What Landos/MEJOR actually does — and where you overlap vs specialise

**The facts.** *A Minha Terra* (aminhaterra.pt, branded "LandOS") is a
rural-land platform for private landowners — Portugal has ~12M private rustic
parcels, much of it absentee-owned. Its `/fire` feature is **not a risk score**;
it's a fuel-management *workflow*: pick município → draw your parcel → answer a
few questions → receive a customised legal + good-practice **fuel-clearing
checklist** (with an AI assistant, "Bom Vizinho"). A separate companion map
(*perspetiva.aminhaterra.pt*) publishes a **2026 seasonal wildfire
susceptibility** layer at 10 m across all 3,049 mainland parishes — *relative
susceptibility, not fire probability* — and that layer is produced by **MEJOR
Technologies' LUCI** model (Dutch wildfire-tech startup; also piloting LUCI with
the insurer Fidelidade for underwriting). The platform is free for landowners;
monetisation is a future service-marketplace + municipal tooling.

**Where you overlap (and should say so plainly — it builds credibility):**
same hazard substrate (fuel, terrain, vegetation, recent burns), same geography,
same 10 m working resolution, and the **same intellectual honesty** — both of you
say "relative susceptibility/exposure, *not* a calibrated probability." That
shared honesty is rare and worth foregrounding.

**Where you specialise (the part that is yours alone):**

| | LandOS / MEJOR LUCI | `wildfire-exposure-eo` |
|---|---|---|
| Question answered | "How susceptible is *this land*?" | "Which *assets* are most exposed, in priority order?" |
| Unit | Parcel / parish cell | Critical-infrastructure asset + network |
| Primary user | Landowner (B2C), future município | Civil-protection / utility / município **operations** |
| Output | Susceptibility map + clearing checklist | **Ranked protect-list** with per-asset provenance |
| Model posture | Proprietary ML, retrained per region | **Transparent linear score, auditable in 5 lines of YAML** |
| Validation | Not published | Leakage-clean lift / Spearman vs subsequent burns |

You are not a competitor to LandOS; you are the **asset-prioritisation layer**
that would naturally consume a susceptibility surface like LUCI's (or DGT's) as
*one input feature* and turn it into "send the crew to these 20 substations
first." That framing — "specialise on top of, not against" — is the strongest
and least defensive story for a portfolio.

---

## 4. The seasonal / current-risk gap — and the open-source models that close it

**The honest diagnosis.** Right now your shipped score is **structural**, not
seasonal. Of the six weights in `exposure_score.yaml` v0.2.0, the two
time-varying ones are effectively absent in the validated run: `fwi_p95` was
dropped project-wide (no verified public FWI source in-session), and
`recent_burn_share_12mo` is correctly nulled in the backdated validation to
avoid leakage. So the thing that makes LUCI/IPMA/DGT-RCM feel *current* — a
live fire-weather/seasonal signal — is the one ingredient you don't yet have.
**Closing that gap is the single highest-leverage technical improvement**, and
it directly answers your "find an equivalent to the MEJOR/DGT model" question.

Open-source equivalents, mapped to what they replace in the national stack:

| Need (their stack) | Open-source equivalent | What it gives you | License / Python |
|---|---|---|---|
| Daily/seasonal fire weather (IPMA FWI, MEJOR ERA5 driver) | **xclim `indices.fire`** (Canadian FWI) over **ERA5** | Compute FFMC/DMC/DC/ISI/BUI/FWI deterministically *in-pipeline*; same index EFFIS uses, so directly comparable to official layers | Apache-2.0 · `pip install xclim` |
| Ready-made danger layers + **seasonal forecast** | **EFFIS / Copernicus CEMS** fire danger (GEFF engine) | Current + seasonal (to ~216-day lead) danger rasters, no modelling needed | Copernicus open · `cdsapi` |
| Seasonal driver context (LUCI-style) | **SeasFire datacube** (Zenodo/Zarr) | 59 climate/veg/ocean/human vars, 8-day, as a seasonal context feature | open · xarray + Zarr |
| Burn-scar / recent-fire signal | **Prithvi-EO 2.0 BurnScars** (you already use this) | Sentinel-2-native scar detection — *of past fires, not ignition forecasting* | Apache-2.0 · TerraTorch |
| (Optional) spread/behaviour halo | **Pyretechnics** | Pure-Python Rothermel/ELMFIRE surface+crown spread around top assets | EPL-2.0 · PyPI |

The cleanest move: **add an FWI/seasonal feature via xclim+ERA5 (or pull the
EFFIS layer directly).** It is permissively licensed, network-light, fits the
existing STAC/feature pattern, restores a real "this-season" component to the
score, and gives you an honest, public-data analogue of what MEJOR feeds LUCI —
without claiming to *be* LUCI. (Items flagged unverified in research: FireScope,
Next-Day-Wildfire-Spread, FireRisk, cffdrs-python licenses — confirm before reuse.)

---

## 5. Burn-scar remediation (`prompts/16`) — recommendation

**Run it in Claude Code. Yes.** It is an almost ideal CC work-unit: a written
mission, a Phase-0 *diagnostic-before-change* step, three scoped Phase-1 edits,
a Phase-2 validation harness, explicit unit/schema/smoke tests, the four gates,
and two HIL flags drawn precisely on the data-contract lines. That structure is
what CC executes well and what a reviewer loves to see.

Three pieces of judgement to carry in with you:

1. **It's a credibility fix first, a modelling fix second.** Your public
   geobrowser currently renders a burn-scar layer that over-predicts ~950× vs
   ICNF-mapped area (47% of pixels ≥0.5). For a piece you'll show to hiring
   managers, a *visibly broken layer on the live demo* is the liability to
   remove before anything else. Even if you did nothing else, fix or caveat this.
2. **It does not move the historical validation numbers.** `recent_burn_share`
   is nulled in the backdated run, so remediation improves the *current-season /
   display* product and any forward-looking score — not the lift/Spearman table.
   Don't expect §6 to change because of it.
3. **Hold FLAG A unless you intend to re-publish.** Approving the reducer swap
   means re-running inference, re-uploading the R2 COG, re-pointing the STAC
   href, and a downstream exposure re-score. Only greenlight that if you're
   committing to refresh the published artefacts in the same push.

Estimated 1–2 CC sessions. Sequence it **first** in the next block of work.

---

## 6. The credibility risk a sharp reviewer will find

Your validation is honest, which is a strength — but the **signal is thin**: 5
burned assets out of 3,045, top-decile lift unresolved (the ablation actually
out-scores the full config within one-asset granularity), Spearman ρ ≈ 0.037.
A good interviewer will go straight here. Two ways to handle it, not mutually
exclusive:

- **Reframe (free, do now):** lead the README and any deck with *"transparent,
  reproducible screening method, validated against subsequent burns"* and put
  the **ablation row first**. You already do this in the report — pull it up to
  the headline so the honesty reads as rigour, not as a buried weakness.
- **Strengthen (cheap, do soon):** widen the evaluation universe — more AOI
  area and/or more post-window ICNF vintages — to get N(burned assets) from 5
  into the dozens, where lift becomes legible. This is the difference between
  "interesting demo" and "method that demonstrably works."

---

## 7. Recommended next steps — prioritised

**P0 — do now (cheap, high portfolio ROI):**

1. **Burn-scar remediation** (`prompts/16` in CC) — remove the broken layer from
   the live demo. *§5.*
2. **Add a fire-weather / seasonal feature** (xclim+ERA5 or EFFIS layer) —
   restores the "this-season" signal and answers the MEJOR/DGT-equivalent
   question with public data. *§4.*
3. **Sharpen the one-line wedge** everywhere ("we rank the infrastructure, not
   the land") and make sure **nothing on the geobrowser is visibly wrong** — it's
   your 10-second proof.

**P1 — do soon (positions you against the field):**

4. **Widen validation** (more area / more burn years → N up). *§6.*
5. **FireScope benchmark** (`prompts/13`) — a head-to-head against the public
   SOTA risk raster, run through *your own* leakage-clean harness. Strong
   portfolio signal: "I can benchmark my transparent screen against a CVPR model
   and tell you honestly where each wins."
6. **National reference distribution** so exposure scores compare across AOIs
   (currently AOI-relative by design — documented, but a rollout needs this).

**P2 — the real differentiator (later, higher effort):**

7. **Network / topology exposure** — model power and water as *graphs*, not
   isolated points: a substation's exposure should include the lines feeding it
   and what it serves downstream. Nobody in §2 does this. It's the most
   defensible long-term moat and the most interesting engineering story.
   *(Superseded by `docs/operationalization.md` §3 — network/topology is now a
   first-class Wave-1 WU (Pillar 1, `prompts/19`), promoted because it is the
   headline differentiator and a long-lead engineering item. The "P2 — later"
   label here reflects the original brief; the operationalization program is the
   live execution order.)*
8. **One-page decision brief generator** — turn the ranked parquet into a
   muni/utility-facing "top-20 assets to clear this season" PDF.
9. **Optional Pyretechnics spread halo** around the top-ranked assets for a
   behaviour-aware exposure refinement.

---

## 8. Three pivots, and the one to pick

- **A. "The asset layer" (recommended).** Stay infrastructure-exposure;
  position explicitly as the prioritisation layer that consumes susceptibility
  surfaces (DGT/LUCI/EFFIS) and outputs a ranked protect-list. Clear, unique,
  defensible, and honest about not competing with agencies. Best fit for a
  hiring/consulting portfolio.
- **B. "Validated screening method."** Lean academic — FireScope head-to-head,
  write up the transparent-screen-vs-black-box comparison. Good if you want a
  publishable/talk artefact; slower payoff.
- **C. "Insurance/utility decision tool."** Productise the protect-list. Real
  market (MEJOR is already piloting Fidelidade) — but crowded, heavy, and pulls
  you away from a portfolio piece toward a startup. Note it as a destination,
  don't pivot the repo into it now.

Pick **A**, borrow the FireScope benchmark from **B** as a credibility set-piece,
and keep **C** as the "where this could go commercially" slide.

---

## 9. How to present it (audience = hiring / consulting)

What a reviewer should take away in 60 seconds: *this person ships a
reproducible EO pipeline (STAC-native, COG/GeoParquet, CI-gated, schema-
validated), reasons about validation honestly, and can position a product in a
real market.* Foreground: the **wedge** (asset vs land), the **provenance +
honesty** (per-row lineage, ablation-first validation, "rank not probability"),
and the **craft** (the gates and verify-then-act protocol in `CLAUDE.md`).
De-emphasise the raw lift numbers until §6 strengthens them. The **geobrowser is
the hook** — so P0 #1 and #3 (nothing visibly broken) are non-negotiable before
you send the link to anyone.

---

## Sources

- [A Minha Terra / LandOS (aminhaterra.pt)](https://aminhaterra.pt) · [Observador — 2026 susceptibility map per freguesia](https://observador.pt/2026/06/04/novo-mapa-de-satelite-mostra-areas-mais-suscetiveis-ao-fogo-em-todas-as-freguesias-de-portugal-continental/) · [beira.pt / Lusa — fuel-management platform](https://beira.pt/portal/noticias/incendios-plataforma-tecnologica-ajuda-proprietarios-a-limpar-terrenos/)
- [MEJOR Technologies — LUCI](https://www.mejortechnologies.com/luci) · [MEJOR — "LUCI supports Portugal's 2026 susceptibility map with LandOS"](https://www.mejortechnologies.com/post/luci-supports-portugal-s-new-2026-wildfire-susceptibility-map-with-landos) · [MEJOR — Protechting / Fidelidade pilot](https://www.mejortechnologies.com/post/mejor-technologies-selected-for-protechting-open-innovation-challenge)
- [DGT — Carta de Perigosidade de Incêndio Rural](https://www.dgterritorio.gov.pt/atividades/paisagem/ptp/carta-perigosidade-incendio-rural) · [DGT/ICNF — 2020 methodology PDF](https://www.dgterritorio.gov.pt/sites/default/files/ficheiros-dgt/ICNF_cartografia_perigosidade_incendio_2020.pdf) · [DGT — POSEUR-INCENDIO project](https://www.dgterritorio.gov.pt/investigacao/projetos/POSEUR-INCENDIO?language=en)
- [IPMA — FWI & fire danger](https://www.ipma.pt/pt/riscoincendio/fwi/) · [IPMA MF2 dataservices](https://mf2.ipma.pt/about) · [ICNF — Áreas Ardidas (SNIG metadata)](https://geocatalogo.icnf.pt/metadados/area_ardida.html) · [AGIF / SGIFR — APPS](https://www.sgifr.gov.pt/en/apps)
- [xclim fire indices](https://xclim.readthedocs.io/en/stable/api/xclim.indices.fire.html) · [Copernicus CEMS seasonal fire danger](https://ewds.climate.copernicus.eu/datasets/cems-fire-seasonal?tab=overview) · [SeasFire datacube](https://github.com/SeasFire/seasfire-datacube) · [Prithvi-EO-2.0-300M-BurnScars](https://huggingface.co/ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars) · [Pyretechnics](https://github.com/pyregence/pyretechnics) · [FireScope (arXiv 2511.17171)](https://arxiv.org/abs/2511.17171)
