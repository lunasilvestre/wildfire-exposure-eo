"""Pydantic v2 schemas for OSM asset GeoParquet artifacts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


class OsmAssetProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    osm_snapshot_iso: datetime
    overpass_endpoint: str
    overpass_query_sha: str
    taxonomy_sha: str
    taxonomy_version: str
    run_id: str
    code_commit_sha: str
    aoi_path: str
    aoi_geometry_sha: str


class OsmAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    asset_id: str
    osm_type: Literal["node", "way", "relation"]
    osm_id: int
    asset_class: str
    geometry_wkb: bytes
    centroid_lon: float
    centroid_lat: float
    tags: dict[str, str]
    provenance: OsmAssetProvenance

    @field_validator("tags", mode="before")
    @classmethod
    def _parse_tags(cls, v: Any) -> dict[str, str]:
        # Parquet stores tags as JSON string to avoid pyarrow struct-promotion null values.
        if isinstance(v, str):
            parsed = json.loads(v)
            return {str(k): str(val) for k, val in parsed.items()}
        return {str(k): str(val) for k, val in (v or {}).items() if val is not None}

    @field_validator("osm_id")
    @classmethod
    def _osm_id_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("osm_id must be a positive integer")
        return v

    @field_validator("centroid_lon")
    @classmethod
    def _lon_range(cls, v: float) -> float:
        if not (-180.0 <= v <= 180.0):
            raise ValueError("centroid_lon must be in [-180, 180]")
        return v

    @field_validator("centroid_lat")
    @classmethod
    def _lat_range(cls, v: float) -> float:
        if not (-90.0 <= v <= 90.0):
            raise ValueError("centroid_lat must be in [-90, 90]")
        return v
