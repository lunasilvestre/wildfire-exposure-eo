"""Pydantic v2 schemas for static raster fetch manifest artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class FetchRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: Literal["eth-gch-2020", "effis-fuel-map", "dgt-cosc", "dgt-cos"]
    vintage: str
    tile_id: str | None
    source_url: str
    local_path: str
    bytes_downloaded: int
    sha256: str
    fetched_at_utc: datetime
    cache_hit: bool
    license: str
    attribution: str


class StaticRasterManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    code_commit_sha: str
    aoi_path: str
    aoi_geometry_sha: str
    resolved_at_utc: datetime
    records: list[FetchRecord]
    totals_bytes: int
    totals_by_source: dict[str, int]
