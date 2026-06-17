"""Pydantic v2 schemas for wildfire-exposure-eo artifacts."""

from __future__ import annotations

from wildfire_exposure_eo.schemas.burn_perimeter import (
    BurnPerimeter,
    BurnPerimeterProvenance,
    IcnfLayerDescriptor,
)
from wildfire_exposure_eo.schemas.burn_scar import (
    HF_MODEL_ID_PLACEHOLDER,
    BurnScarConfig,
    BurnScarInferenceConfig,
    BurnScarModelConfig,
    BurnScarRun,
)
from wildfire_exposure_eo.schemas.fuel_layer import (
    Crosswalk,
    CrosswalkEntry,
    FuelLayerProvenance,
    GridSpec,
)
from wildfire_exposure_eo.schemas.geobrowser import (
    ExposureFeatureProperties,
    FuelLegendEntry,
    FwiOverlay,
    FwiOverlayComponent,
    GeobrowserArtifact,
    GeobrowserStyleData,
    StudyAreaLayer,
    ValidationHeadline,
)
from wildfire_exposure_eo.schemas.osm_asset import OsmAsset, OsmAssetProvenance
from wildfire_exposure_eo.schemas.scored_asset import (
    FEATURE_NAMES,
    SCORE_FEATURE_NAMES,
    TOPOLOGY_FEATURE_NAMES,
    AssetFeatures,
    ScoredAsset,
    ScoredAssetProvenance,
)
from wildfire_exposure_eo.schemas.source_health import SourceHealth, source_health_from_check
from wildfire_exposure_eo.schemas.stac_manifest import StacItemRef, StacManifest, StacWindow
from wildfire_exposure_eo.schemas.static_raster_manifest import (
    FetchRecord,
    StaticRasterManifest,
)
from wildfire_exposure_eo.schemas.watch_list import WatchListItem, WatchListRun

__all__ = [
    "FEATURE_NAMES",
    "HF_MODEL_ID_PLACEHOLDER",
    "SCORE_FEATURE_NAMES",
    "TOPOLOGY_FEATURE_NAMES",
    "AssetFeatures",
    "BurnPerimeter",
    "BurnPerimeterProvenance",
    "BurnScarConfig",
    "BurnScarInferenceConfig",
    "BurnScarModelConfig",
    "BurnScarRun",
    "Crosswalk",
    "CrosswalkEntry",
    "ExposureFeatureProperties",
    "FetchRecord",
    "FuelLayerProvenance",
    "FuelLegendEntry",
    "FwiOverlay",
    "FwiOverlayComponent",
    "GeobrowserArtifact",
    "GeobrowserStyleData",
    "GridSpec",
    "IcnfLayerDescriptor",
    "OsmAsset",
    "OsmAssetProvenance",
    "ScoredAsset",
    "ScoredAssetProvenance",
    "SourceHealth",
    "StacItemRef",
    "StacManifest",
    "StacWindow",
    "StaticRasterManifest",
    "StudyAreaLayer",
    "ValidationHeadline",
    "WatchListItem",
    "WatchListRun",
    "source_health_from_check",
]
