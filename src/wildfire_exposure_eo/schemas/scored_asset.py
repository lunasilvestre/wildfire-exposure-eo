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

#: Score-input feature order — matches the keys in ``config/exposure_score.yaml``
#: ``weights`` and the deterministic column order of the features parquet. These
#: are the features that may carry a normalized weight in the composite score.
SCORE_FEATURE_NAMES: tuple[str, ...] = (
    "fuel_class_severity_weight",
    "canopy_height_p90_m",
    "slope_max_deg",
    "historical_burn_share",
    "recent_burn_share_12mo",
    "nbr_delta_recent",
    "fire_danger_seasonal",
)

#: Topology-aware features (WU-19, pillar 1). AVAILABLE / reported-secondary: they
#: are computed and carried per asset, but they are NOT in the normalized weight
#: block of ``config/exposure_score.yaml`` — that integration is serialized later
#: (WU-19 phase 3). ``None`` for any asset the graph does not cover (never imputed).
TOPOLOGY_FEATURE_NAMES: tuple[str, ...] = (
    "feeder_count",
    "network_component_size",
    "network_exposure_propagated",
)

#: Current-season FWI-system components from the CEMS EWDS reanalysis
#: (``cems-fire-historical-v1``, daily-updated, ~2-day lag). AVAILABLE /
#: reported-secondary: computed and carried per asset, but NOT in the normalized
#: weight block of ``config/exposure_score.yaml`` (that integration is a separate
#: calibration step). The full Canadian FWI *system* — FWI plus its five
#: components — plus the U.S. NFDRS burning index. Each is an observed reanalysis
#: danger *index*, a relative input, never a probability or forecast (#6).
#: ``None`` for any asset/run the EWDS surface does not cover (never imputed).
EWDS_FWI_FEATURE_NAMES: tuple[str, ...] = (
    "fwi_fwi_current",
    "fwi_bui_current",
    "fwi_dc_current",
    "fwi_dmc_current",
    "fwi_ffmc_current",
    "fwi_isi_current",
    "fwi_bi_current",
)

#: All known per-asset feature names (score inputs + topology + EWDS FWI).
#: ``compose_exposure`` only weights names that also appear in the config
#: ``weights`` block, so adding these here makes them AVAILABLE without changing
#: the score.
FEATURE_NAMES: tuple[str, ...] = (
    SCORE_FEATURE_NAMES + TOPOLOGY_FEATURE_NAMES + EWDS_FWI_FEATURE_NAMES
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
    #: Zonal mean of the season-reduced (p95) Canadian Fire Weather Index over
    #: the asset buffer (WU-17, GWIS NASA GPM-IMERG FWI). FWI is an OPEN danger
    #: *index* — this is one normalised input to a relative rank, never a
    #: probability or forecast. ``None`` when the season-year falls outside the
    #: GWIS layer's real archive (all-zero raster → feature absent, not imputed).
    #: FWI is unbounded above but physically ≲ 200; bound generously.
    fire_danger_seasonal: float | None = Field(default=None, ge=0.0, le=1000.0)

    # --- Topology-aware features (WU-19, pillar 1) — AVAILABLE / reported-secondary.
    # Not in the normalized score-weight block (that integration is serialized
    # later, WU-19 phase 3). ``None`` for any asset the network graph does not
    # cover (e.g. non-power/water classes, or isolated nodes for the propagated
    # feature when local exposure is absent). Never imputed.
    #: Node degree under the inferred power/water topology (substation feeder count
    #: / plant-reservoir link count). A structural relative feature, not a probability.
    feeder_count: float | None = Field(default=None, ge=0.0)
    #: Size of the connected component the node belongs to (1 = isolated). Larger =
    #: embedded in a more extensive co-exposed sub-network. Structural, not a probability.
    network_component_size: float | None = Field(default=None, ge=1.0)
    #: Local exposure rank blended linearly with the mean of graph neighbours' local
    #: exposure rank (α·self + (1-α)·mean(neighbours)). A *relative* within-AOI
    #: screening rank like its inputs — never a calibrated probability or forecast.
    network_exposure_propagated: float | None = Field(default=None, ge=0.0, le=1.0)

    # --- Current-season FWI-system components (CEMS EWDS, AVAILABLE / unweighted).
    # Zonal mean of the observed daily reanalysis (~2-day lag) over the asset
    # buffer. NOT in the normalized score-weight block (separate calibration).
    # Each is a relative danger *index* input, never a probability or forecast
    # (#6). ``None`` when the EWDS surface is absent for the run (never imputed).
    # Indices are unbounded above but physically modest; bound generously. The
    # drought code (``dc``) climbs highest under sustained drought, so it carries
    # the widest bound.
    #: Zonal mean of the Canadian Forest Fire Weather Index (current season).
    fwi_fwi_current: float | None = Field(default=None, ge=0.0, le=1000.0)
    #: Zonal mean of the FWI Build-Up Index (current season).
    fwi_bui_current: float | None = Field(default=None, ge=0.0, le=1000.0)
    #: Zonal mean of the FWI Drought Code (current season; widest physical range).
    fwi_dc_current: float | None = Field(default=None, ge=0.0, le=2000.0)
    #: Zonal mean of the FWI Duff Moisture Code (current season).
    fwi_dmc_current: float | None = Field(default=None, ge=0.0, le=1000.0)
    #: Zonal mean of the FWI Fine Fuel Moisture Code (current season; ~0..101).
    fwi_ffmc_current: float | None = Field(default=None, ge=0.0, le=101.0)
    #: Zonal mean of the FWI Initial Spread Index (current season).
    fwi_isi_current: float | None = Field(default=None, ge=0.0, le=1000.0)
    #: Zonal mean of the U.S. NFDRS Burning Index (current season).
    fwi_bi_current: float | None = Field(default=None, ge=0.0, le=1000.0)


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

    # --- Seasonal fire-weather (WU-17). All ``None`` when the feature was not
    # computed for this run (no fire-weather config, or out-of-archive season).
    #: GWIS FWI source product id (``GWIS/nasa.fwi_gpm.fwi``).
    fire_weather_product_id: str | None = Field(default=None)
    #: GWIS FWI ``config/fire_weather.yaml`` version.
    fire_weather_config_version: str | None = Field(default=None)
    #: Season-year whose fire season was sampled for the FWI surface.
    fire_weather_season_year: int | None = Field(default=None)
    #: Daily-FWI sample dates (ISO) actually fetched for the seasonal surface.
    fire_weather_sample_dates: tuple[str, ...] = Field(default_factory=tuple)
    #: ``True`` when the season fell outside the GWIS archive (feature absent).
    fire_weather_out_of_archive: bool | None = Field(default=None)

    # --- Current-season EWDS FWI-system source. All ``None`` when the EWDS
    # source was not used for this run (no ewds-fwi config, or out-of-range).
    #: EWDS dataset product id (``cems-fire-historical-v1``).
    fwi_product_id: str | None = Field(default=None)
    #: EWDS dataset DOI (``10.24381/cds.0e89c522``).
    fwi_doi: str | None = Field(default=None)
    #: ``config/fire_weather.yaml`` version that pinned the EWDS source.
    fwi_config_version: str | None = Field(default=None)
    #: EWDS dataset-type (``intermediate_dataset`` for the current season).
    fwi_dataset_type: str | None = Field(default=None)
    #: EWDS system version (underscore form, e.g. ``4_1``).
    fwi_system_version: str | None = Field(default=None)
    #: Requested calendar date for the EWDS FWI pull (ISO).
    fwi_requested_date: str | None = Field(default=None)
    #: Observed netCDF ``valid_time`` date (~2-day lag from real time, ISO).
    fwi_valid_date: str | None = Field(default=None)
    #: EWDS request-name -> netCDF-var-name map for the components fetched.
    fwi_variable_map: dict[str, str] = Field(default_factory=dict)

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
