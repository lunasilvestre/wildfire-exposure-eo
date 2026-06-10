"""Pydantic v2 schemas for ICNF burn-perimeter GeoParquet artifacts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class IcnfLayerDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    layer_id: int
    year: int  # first year covered by this layer (1975..latest)
    name: str  # ICNF's layer name, verbatim from the MapServer
    feature_count_total: int  # what the server reports before any AOI filter


class BurnPerimeterProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    icnf_layer_id: int
    icnf_layer_name: str
    vintage_year: int
    mapserver_url: str
    fetched_at_utc: datetime
    run_id: str
    code_commit_sha: str
    aoi_path: str
    aoi_geometry_sha: str
    license: str = "ICNF open data, attribution required"
    attribution: str = "ICNF – Áreas Ardidas em Portugal Continental"


class BurnPerimeter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    row_id: str  # canonical "icnf:<year>:<feature_id>"
    vintage_year: int  # primary join key for Phase 10 / 12
    icnf_feature_id: int  # raw ID from ArcGIS REST
    geometry_wkb: bytes  # EPSG:4326 WKB
    area_ha: float  # from ICNF attribute (preferred) or computed (fallback)
    provenance: BurnPerimeterProvenance
