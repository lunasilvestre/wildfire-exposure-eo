"""Pydantic v2 schemas for wildfire-exposure-eo artifacts."""

from __future__ import annotations

from wildfire_exposure_eo.schemas.burn_scar import (
    HF_MODEL_ID_PLACEHOLDER,
    BurnScarConfig,
    BurnScarInferenceConfig,
    BurnScarModelConfig,
    BurnScarRun,
)
from wildfire_exposure_eo.schemas.source_health import SourceHealth, source_health_from_check
from wildfire_exposure_eo.schemas.stac_manifest import StacItemRef, StacManifest, StacWindow

__all__ = [
    "HF_MODEL_ID_PLACEHOLDER",
    "BurnScarConfig",
    "BurnScarInferenceConfig",
    "BurnScarModelConfig",
    "BurnScarRun",
    "SourceHealth",
    "StacItemRef",
    "StacManifest",
    "StacWindow",
    "source_health_from_check",
]
