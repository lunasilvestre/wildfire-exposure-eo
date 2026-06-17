"""Pydantic v2 schemas for the operational "assets to watch" decision product (WU-26).

The watch list is the two-axis join the operational refresh spine emits every
two days (matching the ~2-day EWDS reanalysis lag): each pilot scored asset's
VALIDATED STRUCTURAL exposure rank crossed with the CURRENT OBSERVED fire
weather (EWDS FWI) at the asset's location.

Two artefacts share this module:

* ``WatchListItem`` — one row of the watch-list GeoParquet / JSON: the asset
  identity, its structural ``exposure_score`` (validated v0.3.1 rank, 0..1), the
  current FWI sampled at the asset (raw + normalised 0..1), and the transparent
  triage priority ``watch_priority``.
* ``WatchListRun`` — the run-level header (provenance + the exact formula
  parameters) written alongside the rows so every artefact is self-describing.

Terminology guard (CLAUDE.md non-negotiable #6 + #9): ``watch_priority`` is an
OPERATIONAL TRIAGE score — "validated high-exposure assets currently under
elevated OBSERVED fire weather, prioritise monitoring". It is NOT a forecast,
NOT a probability, NOT a prediction of ignition. FWI is observed reanalysis
(~2-day lag, 0.25° regional grid). No production / operationally-validated
claims attach to this product.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WatchListItem(BaseModel):
    """One asset on the operational watch list (a row of the join artefacts).

    ``watch_priority = exposure_score * fwi_norm`` where
    ``fwi_norm = clip(fwi_current / fwi_ref, 0, 1)`` (the reference + the formula
    are recorded on :class:`WatchListRun`). ``fwi_current`` is ``None`` when the
    EWDS surface does not cover the asset's cell — that asset is then carried with
    ``watch_priority = None`` (never imputed; non-negotiable, never fabricate).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: str = Field(..., min_length=1)
    osm_type: Literal["node", "way", "relation"]
    osm_id: int = Field(..., gt=0)
    asset_class: str = Field(..., min_length=1)
    criticality_weight: float = Field(..., ge=0.0, le=1.0)
    #: Asset location (EPSG:4326) — representative point, for the human table.
    lon: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)
    #: VALIDATED STRUCTURAL screening rank in [0, 1] — NOT a probability (#6).
    exposure_score: float = Field(..., ge=0.0, le=1.0)
    #: Integer structural position within the AOI; 1 = most exposed.
    exposure_rank: int = Field(..., ge=1)
    #: Current OBSERVED EWDS FWI at the asset's grid cell; ``None`` if uncovered.
    fwi_current: float | None = Field(default=None)
    #: ``fwi_current`` normalised to [0, 1] by the run's reference; ``None`` if
    #: ``fwi_current`` is ``None``.
    fwi_norm: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Transparent triage priority ``exposure_score * fwi_norm`` in [0, 1];
    #: ``None`` when FWI is uncovered (never imputed).
    watch_priority: float | None = Field(default=None, ge=0.0, le=1.0)


class WatchListRun(BaseModel):
    """Run-level header for one operational-refresh watch list (provenance + formula).

    Self-describes the artefacts: the exact triage formula and FWI normalisation,
    the run provenance (run_id, code_commit_sha, model_version, seed), and the
    EWDS FWI source identity + observed valid date (non-negotiable #3). The EWDS
    API key is NEVER carried here (security).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    code_commit_sha: str = Field(..., min_length=1)
    #: Exposure-config version the structural rank came from (e.g. ``0.3.1``).
    model_version: str = Field(..., min_length=1)
    #: Deterministic seed (non-negotiable #4); no RNG is used, threaded for contract.
    seed: int
    aoi_name: str = Field(..., min_length=1)
    aoi_path: str = Field(..., min_length=1)
    #: Source scored-asset run-id the structural ranks were read from.
    exposure_run_id: str = Field(..., min_length=1)

    #: Human-readable triage formula (the contract, spelled out).
    formula: str = Field(..., min_length=1)
    #: FWI normalisation reference value (raw FWI mapped to 1.0 at this value).
    fwi_ref: float = Field(..., gt=0.0)
    #: How the FWI reference was chosen (cited, never invented).
    fwi_ref_rationale: str = Field(..., min_length=1)

    #: EWDS FWI source identity + the observed reanalysis valid date.
    fwi_valid_date: str = Field(..., min_length=1)
    fwi_requested_date: str = Field(..., min_length=1)
    fwi_product_id: str = Field(..., min_length=1)
    fwi_doi: str = Field(..., min_length=1)
    fwi_dataset_type: str = Field(..., min_length=1)
    fwi_system_version: str = Field(..., min_length=1)
    fwi_attribution: str = Field(..., min_length=1)
    fwi_lag_note: str = Field(..., min_length=1)

    #: Counts for the run summary.
    n_assets: int = Field(..., ge=0)
    n_with_fwi: int = Field(..., ge=0)
    top_n: int = Field(..., ge=1)
