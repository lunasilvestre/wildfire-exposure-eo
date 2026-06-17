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
    #: Current-season FWI operational overlay; ``None`` when no EWDS COGs are
    #: wired (e.g. the smoke bundle or a tree built before the FWI pull).
    fwi_overlay: FwiOverlay | None = None
