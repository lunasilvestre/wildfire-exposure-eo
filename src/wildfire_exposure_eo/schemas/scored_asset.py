"""Pydantic v2 schemas for the per-asset feature + exposure-rank GeoParquet (WU-6).

Two artefacts share this module:

* ``AssetFeatures`` — the raw per-asset zonal feature values (all optional;
  ``None`` means the feature could not be computed for that asset, e.g. an
  out-of-window input or a buffer that fell entirely on nodata). Never imputed.
* ``ScoredAsset`` — one row of ``exposure_{run_id}.parquet``: the features plus
  the composite ``exposure_score`` and integer ``exposure_rank`` and the full
  provenance contract (non-negotiable #3).

Terminology guard (CLAUDE.md non-negotiable #6): ``exposure_score`` is a
*relative, AOI-normalised screening rank* in [0, 1] — never a probability of
fire. ``exposure_rank`` is the integer position (1 = most exposed).

Kept separate from ``features.py`` / ``scoring.py`` so CI's ``validate-schemas``
job can import and validate a sample row without importing rasterio/stackstac.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Canonical feature order — matches the keys in ``config/exposure_score.yaml``
#: ``weights`` and the deterministic column order of the features parquet.
FEATURE_NAMES: tuple[str, ...] = (
    "fuel_class_severity_weight",
    "canopy_height_p90_m",
    "slope_max_deg",
    "historical_burn_share",
    "recent_burn_share_12mo",
    "nbr_delta_recent",
)


class AssetFeatures(BaseModel):
    """Raw per-asset zonal feature values; ``None`` = not computed (never imputed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Zonal mean of the WU-5 fuel-severity band (0–1) inside the buffer.
    fuel_class_severity_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Zonal p90 of ETH GCH canopy height (metres) inside the buffer.
    canopy_height_p90_m: float | None = Field(default=None, ge=0.0)
    #: Zonal max of Cop-DEM GLO-30 Horn slope (degrees) inside the buffer.
    slope_max_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    #: Area share of the buffer intersecting ICNF burns with vintage ≤ window.end.
    historical_burn_share: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Share of buffer pixels with WU-1 burn-scar probability ≥ threshold.
    #: Upward-biased relative rank input (max-composite retains single-scene
    #: false positives), not a burned-area estimate. ``None`` when the requested
    #: window falls outside the fixed burn-scar COG window.
    recent_burn_share_12mo: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Zonal mean of the S2 spring-minus-late-summer median-NBR delta inside the
    #: buffer. Positive = NBR declined into late summer (drier / more stressed
    #: vegetation). NBR ∈ [-1, 1] so the delta ∈ [-2, 2].
    nbr_delta_recent: float | None = Field(default=None, ge=-2.0, le=2.0)


class ScoredAssetProvenance(BaseModel):
    """Full provenance for one scored-asset row (non-negotiable #3).

    Carries the identity of every artefact the score consumed, so any row can
    be traced back to its exact inputs and reproduced from ``code_commit_sha``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Exposure-config (``config/exposure_score.yaml``) version — the model_version.
    model_version: str = Field(..., min_length=1)
    config_sha: str = Field(..., min_length=64, max_length=64)
    crosswalk_sha: str = Field(..., min_length=64, max_length=64)
    run_id: str = Field(..., min_length=1)
    code_commit_sha: str = Field(..., min_length=1)

    aoi_path: str = Field(..., min_length=1)
    aoi_geometry_sha: str = Field(..., min_length=64, max_length=64)
    window_start: date
    window_end: date

    #: Source-artefact SHA-256s (64 hex). ``None`` where the source was not used
    #: for this run (e.g. burn-scar COG skipped for an out-of-window backdate).
    osm_parquet_sha: str = Field(..., min_length=64, max_length=64)
    burns_parquet_sha: str = Field(..., min_length=64, max_length=64)
    fuel_cog_sha: str = Field(..., min_length=64, max_length=64)
    gch_cache_sha: str = Field(..., min_length=64, max_length=64)
    burn_scar_cog_sha: str | None = Field(default=None)

    #: Cop-DEM GLO-30 STAC item IDs used to derive the slope raster.
    dem_item_ids: tuple[str, ...] = Field(default_factory=tuple)
    #: Sentinel-2 L2A item IDs used to derive ``nbr_delta_recent``.
    s2_item_ids: tuple[str, ...] = Field(default_factory=tuple)
    #: Probability threshold used to binarise the burn-scar COG.
    burn_share_threshold: float = Field(..., gt=0.0, lt=1.0)

    @field_validator("burn_scar_cog_sha")
    @classmethod
    def _sha_or_none(cls, v: str | None) -> str | None:
        if v is not None and len(v) != 64:
            raise ValueError("burn_scar_cog_sha must be a 64-char SHA-256 or None")
        return v


class ScoredAsset(BaseModel):
    """One row of ``exposure_{run_id}.parquet``."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    asset_id: str = Field(..., min_length=1)
    osm_type: Literal["node", "way", "relation"]
    osm_id: int = Field(..., gt=0)
    asset_class: str = Field(..., min_length=1)
    criticality_weight: float = Field(..., ge=0.0, le=1.0)
    centroid_lon: float = Field(..., ge=-180.0, le=180.0)
    centroid_lat: float = Field(..., ge=-90.0, le=90.0)
    geometry_wkb: bytes

    features: AssetFeatures
    #: Names of the features that were actually computed (non-null) for this row.
    features_present: tuple[str, ...]
    #: Relative, AOI-normalised screening rank in [0, 1] — NOT a probability.
    exposure_score: float = Field(..., ge=0.0, le=1.0)
    #: Integer position within the AOI; 1 = most exposed.
    exposure_rank: int = Field(..., ge=1)

    provenance: ScoredAssetProvenance

    @field_validator("features", mode="before")
    @classmethod
    def _parse_features(cls, v: Any) -> Any:
        # Parquet stores the nested features as a JSON string.
        if isinstance(v, str):
            return json.loads(v)
        return v

    @field_validator("provenance", mode="before")
    @classmethod
    def _parse_provenance(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v)
        return v

    @field_validator("features_present", mode="before")
    @classmethod
    def _parse_present(cls, v: Any) -> Any:
        # Parquet round-trips a list column; normalise to a tuple of names.
        if isinstance(v, str):
            return tuple(json.loads(v))
        return tuple(v)

    @field_validator("features_present")
    @classmethod
    def _present_are_known(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        unknown = set(v) - set(FEATURE_NAMES)
        if unknown:
            raise ValueError(f"features_present has unknown feature(s): {sorted(unknown)}")
        return v
