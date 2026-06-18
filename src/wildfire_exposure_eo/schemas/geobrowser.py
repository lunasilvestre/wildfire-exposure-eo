"""Pydantic v2 schemas for the GitHub Pages geobrowser data bundle (WU-9).

Two artefacts share this module:

* ``ExposureFeatureProperties`` — the ``properties`` dict of one feature in the
  exposure-assets display GeoJSON (a strict subset of ``ScoredAsset``; every
  source row is validated against ``ScoredAsset`` before export).
* ``GeobrowserStyleData`` — ``docs/app/data/style_data.json``: colour LUTs
  sampled from the same matplotlib colormaps the WU-8 figures use, the
  fuel-class legend from ``config/fuel_crosswalk.yaml``, the validation
  headline read verbatim from the WU-7 metrics JSON, and the artefact manifest
  (run ids, hrefs, CRS notes — non-negotiable #2: CRS is explicit, always).

Terminology guard (CLAUDE.md non-negotiable #6): ``exposure_score`` is a
*relative, AOI-normalised screening rank* in [0, 1] — never a probability of
fire. The one allowed "probability" is the Prithvi *burn-scar inference
probability* (a detection output; not calibrated, not a forecast).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: RGB triple, 0–255 per channel.
Rgb = tuple[int, int, int]


class ExposureFeatureProperties(BaseModel):
    """``properties`` of one exposure-assets GeoJSON feature (display copy)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: str = Field(..., min_length=1)
    osm_type: Literal["node", "way", "relation"]
    osm_id: int = Field(..., gt=0)
    asset_class: str = Field(..., min_length=1)
    criticality_weight: float = Field(..., ge=0.0, le=1.0)
    #: Relative, AOI-normalised screening rank in [0, 1] — NOT a probability.
    exposure_score: float = Field(..., ge=0.0, le=1.0)
    #: Integer position within the AOI; 1 = most exposed.
    exposure_rank: int = Field(..., ge=1)
    #: Area share of the asset buffer that intersected an ICNF historical-burn
    #: perimeter within the scoring window (the ``historical_burn_share`` Stage-2
    #: feature, lifted out of the nested ``features`` dict for the display copy).
    #: A descriptive footprint statistic — NOT a probability and NOT the
    #: validation label (the validation "burned" count comes from buffers that
    #: intersect burns *after* the window, computed separately in validation.py).
    #: ``None`` when the feature was not present for that run (the field is
    #: optional so display copies exported before this column existed stay valid).
    historical_burn_share: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Cross-AOI-comparable triage severity in [0, 1] = ``exposure_score`` ×
    #: ``criticality_weight``, then NORMALISED across the POOLED assets of every
    #: published AOI (global max → 1). Drives the full-extent (Iberia) OUTPUT
    #: layer's colour so the most-exposed important assets across all study areas
    #: are comparable on one ramp. Honest scope (non-negotiable #6): "relative
    #: within-AOI exposure × asset criticality, normalised across study areas" —
    #: NOT an absolute cross-region risk or probability, NOT a forecast. The
    #: per-AOI ``exposure_rank`` stays AOI-relative; this is the only cross-AOI
    #: axis. ``None`` for display copies exported before this column existed (the
    #: field is optional for back-compat).
    impact_severity: float | None = Field(default=None, ge=0.0, le=1.0)


class FuelLegendEntry(BaseModel):
    """One fuel-class legend entry (EFFIS NFFL code → label + display colour)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: int = Field(..., ge=0, le=255)
    label: str = Field(..., min_length=1)
    color: Rgb


class ValidationHeadline(BaseModel):
    """Headline numbers read from the WU-7 metrics JSON (never re-derived)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    n_assets: int = Field(..., ge=1)
    n_burned: int = Field(..., ge=0)
    base_rate: float = Field(..., ge=0.0, le=1.0)
    #: True when too few burned assets for lift/Spearman (e.g. the smoke tile);
    #: the lift / Spearman fields are then ``None``, mirroring the metrics JSON.
    degenerate: bool
    top_decile_lift: float | None = Field(default=None, ge=0.0)
    cumulative_lift_top30pct: float | None = Field(default=None, ge=0.0)
    spearman_rho: float | None = Field(default=None, ge=-1.0, le=1.0)
    spearman_p: float | None = Field(default=None, ge=0.0, le=1.0)
    ablation_top_decile_lift: float | None = Field(default=None, ge=0.0)
    window_end: str = Field(..., min_length=1)
    validation_years: list[int]


class GeobrowserArtifact(BaseModel):
    """One geodata file the site renders, with its CRS stated explicitly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    href: str = Field(..., min_length=1)
    crs: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    #: Display copies are derived from the authoritative artefact (e.g. warped
    #: to EPSG:3857 for client-side COG rendering); ``authoritative`` files are
    #: the published STAC assets themselves.
    role: Literal["authoritative", "display"]
    description: str = Field(..., min_length=1)


class FwiOverlayComponent(BaseModel):
    """One current-season FWI overlay component (FWI or a Canadian sub-index).

    The href points at the EPSG:3857 display COG on Cloudflare R2. ``value_min``
    / ``value_max`` are the finite range of the surface, driving the colour ramp
    in the geobrowser legend. The value is an OBSERVED reanalysis danger
    *index*, never a probability or a forecast (non-negotiable #6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Short component token (``fwi``, ``ffmc``, ``dmc``, ``dc``, ``isi``, ``bui``).
    component: str = Field(..., min_length=1)
    #: Human label shown in the legend (e.g. "Fine Fuel Moisture Code (FFMC)").
    label: str = Field(..., min_length=1)
    href: str = Field(..., min_length=1)
    crs: str = Field(..., min_length=1)
    value_min: float
    value_max: float

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        if self.value_max < self.value_min:
            raise ValueError(
                f"FWI component {self.component!r}: value_max {self.value_max} "
                f"< value_min {self.value_min}"
            )


class FwiOverlay(BaseModel):
    """Current-season EWDS FWI overlay manifest (the operational second axis).

    Terminology guard (non-negotiable #6): this is CURRENT OBSERVED reanalysis
    fire weather (~2-day lag, 0.25° regional grid), NOT a per-asset score, NOT a
    probability, NOT a forecast. It is the operational counterpart to the
    VALIDATED STRUCTURAL exposure rank carried by the assets layer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: netCDF ``valid_time`` of the EWDS reanalysis day rendered (ISO date).
    valid_date: str = Field(..., min_length=1)
    #: Lag note shown in the caption (e.g. "~2-day lag").
    lag_note: str = Field(..., min_length=1)
    #: Source attribution (CEMS EWDS, CC-BY-4.0) read from config.
    attribution: str = Field(..., min_length=1)
    #: Components in display order (FWI first, then the Canadian sub-indices).
    components: list[FwiOverlayComponent] = Field(..., min_length=1)


class InputRasterLayer(BaseModel):
    """One per-AOI model-INPUT display COG shown as a toggleable raster layer.

    These are the relative model INPUT rasters (canopy height, slope, NBR-delta,
    fuel NFFL class) warped to EPSG:3857 for client-side rendering and hosted on
    Cloudflare R2. Honesty bar (non-negotiable #6): an input raster, never a
    probability, score, or forecast. ``kind`` selects the colour ramp / legend
    (see :class:`InputRampSpec`); ``href`` points at the R2 display COG (loads
    lazily on toggle, like the burn-scar / ICNF layers).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Input type token; matches an :class:`InputRampSpec` key.
    kind: Literal["canopy_height", "slope", "nbr_delta", "fuel_class"]
    href: str = Field(..., min_length=1)
    crs: str = Field(..., min_length=1)
    #: Run id of the display COG (the warp run stamp in the filename).
    run_id: str = Field(..., min_length=1)


class InputRampSpec(BaseModel):
    """Display ramp + legend metadata for one model-INPUT raster *kind*.

    A continuous input (canopy / slope / NBR-delta) paints a value→colour ramp
    stretched between ``value_min`` and ``value_max`` (measured from the COGs,
    not invented — non-negotiable #1); the fuel class reuses the categorical
    ``fuel_legend`` and so carries no continuous ramp here. The values are the
    relative model INPUTS, never probabilities or forecasts (non-negotiable #6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["canopy_height", "slope", "nbr_delta", "fuel_class"]
    label: str = Field(..., min_length=1)
    #: Legend unit suffix shown after the value (e.g. "m", "°"); empty for the
    #: unitless NBR-delta / categorical fuel.
    unit: str = ""
    #: Matplotlib colormap name the LUT was sampled from (provenance; the LUT
    #: itself is carried in ``lut`` so the client needs no matplotlib).
    cmap: str = Field(..., min_length=1)
    #: 256-step RGB LUT sampled from ``cmap`` (None for the categorical fuel).
    lut: list[Rgb] | None = None
    #: Continuous display range driving the ramp (None for the categorical fuel).
    value_min: float | None = None
    value_max: float | None = None
    #: Short honest legend caption (terminology guard).
    caption: str = Field(..., min_length=1)


class MosaicTile(BaseModel):
    """One AOI tile of an INTERIM mosaic (burn-scar or NBR-delta) display COG."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: AOI slug the tile covers (``pilot`` / ``monchique`` / …).
    aoi_name: str = Field(..., min_length=1)
    href: str = Field(..., min_length=1)
    crs: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)


class MosaicLayer(BaseModel):
    """An INTERIM raster mosaic shown as ONE toggle over ALL its AOI tiles.

    The thematic pivot for the interim rasters: instead of a per-AOI swap, every
    tile is shown at once under a single toggle (burn-scar inference, or
    NBR-delta). Honesty bar (non-negotiable #6): burn-scar is recent-scar
    DETECTION (spectral signatures of fires that already happened), never a
    forecast or ignition prediction; NBR-delta is a relative spectral change
    input. ``kind`` selects the colour ramp the client paints these COGs with.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["burn_scar", "nbr_delta"]
    #: AOI tiles composing the mosaic (display order; pilot first by convention).
    tiles: list[MosaicTile] = Field(..., min_length=1)
    #: Honest caption (terminology guard #6).
    caption: str = Field(..., min_length=1)


class FirescopeLayer(BaseModel):
    """FireScope relative wildfire-risk RANK reference COG (a SOTA validation layer).

    A full-Iberia uint8 relative-risk *rank* (0–254, nodata 255) warped to
    EPSG:3857 on Cloudflare R2, shown as the VALIDATION reference. Honest scope
    (non-negotiable #6): a relative wildfire-risk RANK, NOT a probability and NOT
    a forecast. CC-BY-4.0 (INSAIT-Institute + ETH, arXiv:2511.17171) —
    ``attribution`` is REQUIRED in the caption (non-negotiable #1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    href: str = Field(..., min_length=1)
    crs: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    #: Inclusive display range of the relative-risk rank (0–254; 255 is nodata).
    value_min: float
    value_max: float
    #: Matplotlib colormap the LUT was sampled from (provenance; LUT in ``lut``).
    cmap: str = Field(..., min_length=1)
    #: 256-step RGB LUT sampled from ``cmap``.
    lut: list[Rgb] = Field(..., min_length=256, max_length=256)
    #: Required attribution string (CC-BY-4.0, INSAIT/ETH, arXiv:2511.17171).
    attribution: str = Field(..., min_length=1)
    #: Honest legend caption (terminology guard #6).
    caption: str = Field(..., min_length=1)


class BurnHistorySourceStyle(BaseModel):
    """Display style + provenance for one burn-history source (ICNF or EFFIS)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Source token as stored in the GeoJSON ``source`` column.
    source: Literal["ICNF", "EFFIS"]
    #: Human label shown in the legend (e.g. "ICNF (Portugal, fine)").
    label: str = Field(..., min_length=1)
    #: Fill / outline colour for this source (distinct per source).
    color: Rgb
    #: Earliest vintage year present for this source (measured, not invented).
    vintage_min: int = Field(..., ge=1900, le=2100)
    #: Latest vintage year present for this source.
    vintage_max: int = Field(..., ge=1900, le=2100)
    #: Perimeter count for this source (drives the legend/caption).
    n_perimeters: int = Field(..., ge=0)


class BurnHistoryLayer(BaseModel):
    """Iberia historical burned-area perimeters (ICNF-PT + EFFIS-ES), styled by source.

    A full-Iberia vector layer on Cloudflare R2 (too large to commit). Observed
    historical burned-area perimeters (non-negotiable #6: never a probability or
    forecast). The PT/ES temporal+resolution asymmetry (ICNF PT fine 1990–2025 vs
    EFFIS ES coarse 2016–2025) is surfaced via the per-source styles + caption.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    href: str = Field(..., min_length=1)
    crs: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    #: Per-source display styles (ICNF + EFFIS), each with its measured vintages.
    sources: list[BurnHistorySourceStyle] = Field(..., min_length=1)
    #: Honest caption noting the PT/ES temporal + resolution asymmetry.
    caption: str = Field(..., min_length=1)


class ProvenanceSummary(BaseModel):
    """Temporal methodology summary powering the geobrowser process panel.

    Read from the scored-parquet provenance + WU-7 metrics (non-negotiable #1 /
    #3: nothing invented). Frames "the process as a deliverable, temporally":
    the model version, the EO input windows, the FWI valid date, the validation
    years, and the code commit the bundle was generated at (linked to GitHub).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    model_version: str = Field(..., min_length=1)
    #: Full 40-char git commit SHA the scored run was produced at.
    code_commit_sha: str = Field(..., min_length=7, max_length=40)
    #: Sentinel-2 input window (ISO dates) the rank was built from.
    window_start: str = Field(..., min_length=1)
    window_end: str = Field(..., min_length=1)
    #: ICNF burn vintages the rank was validated against (strictly post-window).
    validation_years: list[int]
    #: Count of Sentinel-2 STAC items in the scored run's provenance.
    s2_item_count: int = Field(..., ge=0)
    #: EWDS FWI reanalysis valid date (ISO), or ``None`` when no FWI was wired.
    fwi_valid_date: str | None = None


class StudyAreaLayer(BaseModel):
    """One Wave-2 validation study area shown as a toggleable geobrowser layer.

    Each study area carries its own scored-exposure GeoJSON (same viridis
    ``exposure_rank`` styling as the pilot) plus its AOI outline. The honesty
    bar (non-negotiable #3, #1): ``model_version`` is read VERBATIM from the
    scored parquet's provenance — these are v0.3.0 runs and are labelled as
    such, never relabelled. ``bbox_4326`` drives the fly-to control.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Canonical AOI slug (``monchique``, ``pedrogao_grande`` …); matches
    #: ``data/aoi/<name>.geojson`` (non-negotiable #10 — no hardcoded coords).
    name: str = Field(..., min_length=1)
    #: Human label shown in the AOI selector (e.g. "Monchique").
    label: str = Field(..., min_length=1)
    #: Scored-exposure display GeoJSON (EPSG:4326). When ``committed`` is False
    #: the href points at Cloudflare R2 and the layer loads lazily on toggle.
    exposure_href: str = Field(..., min_length=1)
    exposure_crs: str = Field(..., min_length=1)
    #: AOI outline GeoJSON (EPSG:4326), committed under ``docs/app/data``.
    outline_href: str = Field(..., min_length=1)
    outline_crs: str = Field(..., min_length=1)
    #: Scoring run id of the exposure parquet this layer was exported from.
    run_id: str = Field(..., min_length=1)
    #: Model version read VERBATIM from the parquet provenance (e.g. "0.3.0").
    model_version: str = Field(..., min_length=1)
    #: Assets in this study area (drives the legend/popup denominator).
    n_assets: int = Field(..., ge=1)
    #: ``True`` when the exposure GeoJSON is committed under ``docs/app/data``
    #: (loads eagerly); ``False`` when it is too large and hosted on R2 (loads
    #: lazily on toggle, like the burn-scar / ICNF layers).
    committed: bool
    #: AOI bbox ``[minlon, minlat, maxlon, maxlat]`` for the fly-to control.
    bbox_4326: tuple[float, float, float, float]
    #: ICNF Áreas Ardidas perimeters for THIS AOI (EPSG:4326), hosted on
    #: Cloudflare R2 and loaded lazily when the AOI is shown. ``None`` when no
    #: burns layer was published for the AOI (the geobrowser then just omits its
    #: ICNF overlay). Honest scope (#6): observed historical burned-area
    #: perimeters, never a probability or forecast.
    icnf_href: str | None = Field(default=None, min_length=1)
    icnf_crs: str | None = Field(default=None, min_length=1)
    #: Count of ICNF perimeters in ``icnf_href`` (drives the legend/caption).
    icnf_n_perimeters: int | None = Field(default=None, ge=0)
    #: Per-AOI model-INPUT display COGs (canopy / slope / NBR-delta / fuel),
    #: each toggleable and shown WITH this AOI. Empty when none were published.
    #: Honest scope (#6): inputs, never probabilities or forecasts.
    input_layers: list[InputRasterLayer] = Field(default_factory=list)


class GeobrowserStyleData(BaseModel):
    """``docs/app/data/style_data.json`` — everything the site needs that is
    derived from pipeline artefacts or repo config (nothing hand-made)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_by: str = Field(..., min_length=1)
    code_commit_sha: str = Field(..., min_length=1)
    #: 256-step LUTs sampled from matplotlib — same encodings as the WU-8 figures.
    viridis_lut: list[Rgb] = Field(..., min_length=256, max_length=256)
    ylorrd_lut: list[Rgb] = Field(..., min_length=256, max_length=256)
    fuel_legend: list[FuelLegendEntry]
    validation: ValidationHeadline
    artifacts: dict[str, GeobrowserArtifact]
    #: Wave-2 validation study areas (the four AOIs beyond the pilot), each a
    #: toggleable exposure layer + outline with its own honest model_version.
    study_areas: list[StudyAreaLayer] = Field(default_factory=list)
    #: Pilot AOI model-INPUT display COGs (canopy / slope / NBR-delta). The
    #: pilot's FUEL input keeps its dedicated ``artifacts["fuel_class"]`` entry,
    #: so it is NOT duplicated here. Empty when none were published.
    pilot_input_layers: list[InputRasterLayer] = Field(default_factory=list)
    #: Display ramp + legend metadata per model-INPUT *kind*, shared by the pilot
    #: and every study area (the per-AOI layers reuse one ramp per kind so the
    #: legend reads consistently across AOIs). Empty when no input layers wired.
    input_ramps: list[InputRampSpec] = Field(default_factory=list)
    #: Current-season FWI operational overlay; ``None`` when no EWDS COGs are
    #: wired (e.g. the smoke bundle or a tree built before the FWI pull).
    fwi_overlay: FwiOverlay | None = None
    #: Full-Iberia model-INPUT display COGs (fuel / slope / canopy height), each a
    #: first-class toggleable layer shown at Iberia extent (the thematic pivot —
    #: inputs are no longer per-AOI swaps). They reuse ``input_ramps`` (slope /
    #: canopy) and ``fuel_legend`` (fuel). Empty when no Iberia COGs are wired.
    iberia_inputs: list[InputRasterLayer] = Field(default_factory=list)
    #: FireScope relative wildfire-risk RANK reference COG (the SOTA VALIDATION
    #: layer); ``None`` when not wired. CC-BY-4.0, attribution required.
    firescope: FirescopeLayer | None = None
    #: Iberia historical burned-area perimeters (ICNF-PT + EFFIS-ES), styled by
    #: source; ``None`` when not wired. Observed history, never a forecast.
    burn_history: BurnHistoryLayer | None = None
    #: INTERIM raster mosaics (burn-scar, NBR-delta), each shown as ONE toggle
    #: over all its AOI tiles at once (no per-AOI swap). Empty when none wired.
    mosaics: list[MosaicLayer] = Field(default_factory=list)
    #: Temporal methodology summary powering the process panel; ``None`` when the
    #: scored-run provenance was unavailable (e.g. a smoke bundle).
    provenance_summary: ProvenanceSummary | None = None
