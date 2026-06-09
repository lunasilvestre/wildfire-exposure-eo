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

    binarisation_threshold: float = Field(..., gt=0.0, lt=1.0)
    output_crs: str = Field(..., min_length=1)
    resampling: str = Field(..., min_length=1)
    nodata: float
    output_path: str = Field(..., min_length=1)
