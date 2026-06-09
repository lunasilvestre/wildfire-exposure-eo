"""StacManifest — frozen Pydantic record of the STAC items the pipeline resolved.

The manifest is the project's single source of truth for *which* scenes the
pipeline saw on a given run. Two runs of the same query against an unchanged
MS PC catalog must produce byte-identical `items` arrays inside each
`StacWindow`. Phases 6/7/10 consume the manifest as input — they never call
`pystac_client.search` themselves.

Schema lives separately from `stac.py` so test fixtures and CI's
`validate-schemas` job can import it without pulling the resolution code.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class StacItemRef(BaseModel):
    """One STAC item, captured at resolution time.

    `href_root` is the un-signed blob directory the item lives under; SAS tokens
    are deliberately not persisted (downstream code re-signs via
    `planetary_computer.sign()` at read time).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    datetime_iso: datetime
    bbox: tuple[float, float, float, float]
    cloud_cover: float | None = None
    assets_referenced: tuple[str, ...]
    href_root: str = Field(..., min_length=1)
    extra: dict[str, str | int | float] = Field(default_factory=dict)


class StacWindow(BaseModel):
    """One temporal window of items for one collection.

    For S2 there are two windows (spring strict, summer relaxed) per the
    asymmetry documented in `docs/methodology.md` §3. For S1, DEM, WorldCover
    there is one window each (with `max_cloud_cover=None`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(..., min_length=1)
    start: date
    end: date
    max_cloud_cover: int | None = None
    items: tuple[StacItemRef, ...]
    items_returned: int = Field(..., ge=0)
    relaxed_threshold_reason: str | None = None


class StacManifest(BaseModel):
    """Top-level manifest. One per `resolve-stac` invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    code_commit_sha: str = Field(..., min_length=1)
    aoi_path: str = Field(..., min_length=1)
    aoi_geometry_sha: str = Field(..., min_length=1)
    resolved_at_utc: datetime
    stac_catalog_url: str = Field(..., min_length=1)
    collections: dict[str, tuple[StacWindow, ...]]
    totals: dict[str, int]
