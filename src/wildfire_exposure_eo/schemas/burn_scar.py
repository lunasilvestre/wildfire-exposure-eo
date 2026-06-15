"""BurnScarRun — provenance record for one Prithvi burn-scar inference run.

One record per `infer-burn-scar` invocation, embedded in the output COG's
GeoTIFF tags and written as a JSON sidecar next to it. Captures everything
needed to reproduce the raster: the pinned model identity, the exact S2 item
IDs consumed, the AOI hash, and the software versions involved.

Terminology guard (CLAUDE.md): the raster value is a *burn-scar inference
probability* — the model's class-1 score for a post-event burn scar. It is
not a calibrated probability that a pixel burned and not a fire forecast.

Schema lives separately from `burn_scar.py` so test fixtures and CI's
`validate-schemas` job can import it without pulling torch/terratorch.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

#: Sentinel value the config ships with until the HF model ID has been
#: verified against the live Hugging Face API (CLAUDE.md non-negotiable #1).
HF_MODEL_ID_PLACEHOLDER = "TBD-verified-at-audit"


class BurnScarModelConfig(BaseModel):
    """`model:` section of `config/burn_scar.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str = Field(..., min_length=1)
    downstream_task: str = Field(..., min_length=1)
    hf_model_id: str = Field(..., min_length=1)
    hf_revision_sha: str = Field(..., min_length=1)
    backbone_param_count: int = Field(..., gt=0)
    checkpoint_file: str = Field(..., min_length=1)
    config_file: str = Field(..., min_length=1)


class BurnScarInferenceConfig(BaseModel):
    """`inference:` section of `config/burn_scar.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_months: int = Field(..., ge=1, le=24)
    s2_max_cloud_cover: int = Field(..., ge=0, le=100)
    binarisation_threshold: float = Field(..., gt=0.0, lt=1.0)
    output_format: str = Field(..., min_length=1)
    s2_assets: tuple[str, ...] = Field(..., min_length=1)
    scl_mask_classes: tuple[int, ...]
    tile_size: int = Field(..., gt=0)
    tile_stride: int = Field(..., gt=0)
    #: Composite reducer applied across the scene stack — see
    #: `wildfire_exposure_eo.burn_scar.reduce_stack`. Defaults to ``max`` so a
    #: config that predates WU-10 (no `reducer:` key) reproduces the original
    #: np.fmax composite exactly; the shipped config sets ``p85``.
    reducer: str = Field(default="max", min_length=1)
    #: Fire-season window: only S2 scenes whose acquisition month falls in
    #: ``[season_start_month, season_end_month]`` (1-indexed, inclusive) are
    #: composited. Defaults to 1..12 (no restriction) so pre-WU-10 configs are
    #: unchanged; the shipped config sets 6..10 (ICNF's principal fire season).
    season_start_month: int = Field(default=1, ge=1, le=12)
    season_end_month: int = Field(default=12, ge=1, le=12)


class BurnScarConfig(BaseModel):
    """Parsed `config/burn_scar.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: BurnScarModelConfig
    inference: BurnScarInferenceConfig


class BurnScarRun(BaseModel):
    """Full provenance for one burn-scar inference run (non-negotiable #3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    code_commit_sha: str = Field(..., min_length=1)
    created_at_utc: datetime

    model_id: str = Field(..., min_length=1)
    model_version: str = Field(..., min_length=1)
    hf_revision_sha: str = Field(..., min_length=1)
    terratorch_version: str = Field(..., min_length=1)
    torch_version: str = Field(..., min_length=1)
    device: str = Field(..., min_length=1)

    aoi_path: str = Field(..., min_length=1)
    aoi_geometry_sha: str = Field(..., min_length=1)
    stac_catalog_url: str = Field(..., min_length=1)
    window_start: date
    window_end: date
    s2_max_cloud_cover: int = Field(..., ge=0, le=100)
    s2_item_ids: tuple[str, ...]
    scl_mask_classes: tuple[int, ...]

    #: Composite reducer applied across the scene stack (WU-10). Defaulted so
    #: provenance records written before WU-10 still deserialise; ``max``
    #: reproduces the original np.fmax composite.
    reducer: str = Field(default="max", min_length=1)
    #: Fire-season window actually applied to the scene query (WU-10), 1-indexed
    #: inclusive. Defaulted to 1..12 (no restriction) for backward compat.
    season_start_month: int = Field(default=1, ge=1, le=12)
    season_end_month: int = Field(default=12, ge=1, le=12)

    binarisation_threshold: float = Field(..., gt=0.0, lt=1.0)
    output_crs: str = Field(..., min_length=1)
    resampling: str = Field(..., min_length=1)
    nodata: float
    output_path: str = Field(..., min_length=1)
