"""SourceHealth — canonical machine-readable shape for one audit probe result.

`audit.CheckResult` is the dataclass used internally by the probe functions.
`SourceHealth` is the validated, externally-facing record written to
`outputs/audit/<run_id>.json`. Keeping the two distinct lets the probe code
stay dependency-light while still guaranteeing every record published to disk
or CI has been validated by Pydantic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from wildfire_exposure_eo.audit import CheckResult

Status = Literal["GREEN", "YELLOW", "RED"]


class SourceHealth(BaseModel):
    """One row of the audit report.

    The `items_found` and `endpoint` fields are nullable / best-effort because
    not every probe maps cleanly (e.g. multi-collection HLS, OSM Overpass with
    per-class counts). The adapter populates them from `details` when present.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(..., min_length=1)
    status: Status
    items_found: int | None = None
    endpoint: str = ""
    message: str
    elapsed_ms: int = Field(..., ge=0)
    checked_at_utc: datetime


_DETAIL_ENDPOINT_KEYS = ("endpoint", "url")
_DETAIL_ITEMS_KEYS = ("items_found", "total")


def _coerce_items_found(details: dict[str, object]) -> int | None:
    for key in _DETAIL_ITEMS_KEYS:
        val = details.get(key)
        if isinstance(val, int):
            return val
    counts = details.get("counts_per_layer") or details.get("counts")
    if isinstance(counts, dict):
        total = sum(v for v in counts.values() if isinstance(v, int) and v > 0)
        return total if total > 0 else None
    items_per_collection = details.get("items_per_collection")
    if isinstance(items_per_collection, dict):
        total = sum(v for v in items_per_collection.values() if isinstance(v, int) and v > 0)
        return total if total > 0 else None
    return None


def _coerce_endpoint(details: dict[str, object]) -> str:
    for key in _DETAIL_ENDPOINT_KEYS:
        val = details.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def source_health_from_check(
    result: CheckResult,
    *,
    elapsed_ms: int,
    checked_at_utc: datetime | None = None,
    source_id: str | None = None,
    endpoint: str | None = None,
) -> SourceHealth:
    """Adapt an internal `CheckResult` into a validated `SourceHealth` record."""
    ts = checked_at_utc or datetime.now(UTC)
    chosen_endpoint = endpoint if endpoint is not None else _coerce_endpoint(result.details)
    return SourceHealth(
        source_id=source_id or result.name,
        status=result.status,
        items_found=_coerce_items_found(result.details),
        endpoint=chosen_endpoint,
        message=result.message,
        elapsed_ms=elapsed_ms,
        checked_at_utc=ts,
    )
