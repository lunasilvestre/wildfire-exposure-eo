"""Pydantic v2 schemas for the fuel-layer pipeline (WU-5).

Split from fuel.py so tests and CI validate-schemas can import without
pulling rasterio/rioxarray.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CrosswalkEntry(BaseModel):
    """One row of config/fuel_crosswalk.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effis_code: int = Field(..., ge=1, le=13)
    nffl_name: str = Field(..., min_length=1)
    internal_class: str = Field(..., min_length=1)
    severity: float = Field(..., ge=0.0, le=1.0)
    comment: str = Field(..., min_length=1)


class Crosswalk(BaseModel):
    """Parsed and validated config/fuel_crosswalk.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    source_taxonomy: str = Field(..., min_length=1)
    internal_taxonomy_ref: str = Field(..., min_length=1)
    cosc_herbaceous_override_severity: float = Field(..., ge=0.0, le=1.0)
    entries: tuple[CrosswalkEntry, ...]
    crosswalk_sha: str = Field(..., min_length=64, max_length=64)

    @field_validator("entries")
    @classmethod
    def _entries_not_empty(cls, v: tuple[CrosswalkEntry, ...]) -> tuple[CrosswalkEntry, ...]:
        if not v:
            raise ValueError("crosswalk must contain at least one entry")
        return v

    def severity_for_code(self, code: int) -> tuple[str, float]:
        """Return (internal_class, severity) for an EFFIS code.

        Raises ValueError for unmapped codes — CLAUDE.md non-negotiable #1:
        no invented mappings; the crosswalk must be complete.
        """
        for entry in self.entries:
            if entry.effis_code == code:
                return entry.internal_class, entry.severity
        raise ValueError(
            f"EFFIS code {code} is not mapped in the crosswalk (version {self.version}). "
            "Add an explicit entry to config/fuel_crosswalk.yaml."
        )


class GridSpec(BaseModel):
    """Explicit grid specification for the pilot or smoke AOI.

    Derived solely from the AOI geometry; no hardcoded coordinates
    (CLAUDE.md non-negotiable #10).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    crs: str = Field(..., min_length=1)
    transform: tuple[float, ...]
    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)
    resolution_m: int = Field(..., gt=0)
    aoi_geometry_sha: str = Field(..., min_length=1)


class FuelLayerProvenance(BaseModel):
    """Full provenance record embedded in the fuel-layer COG sidecar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    code_commit_sha: str = Field(..., min_length=1)
    aoi_path: str = Field(..., min_length=1)
    aoi_geometry_sha: str = Field(..., min_length=1)

    effis_cache_path: str = Field(..., min_length=1)
    effis_sha256: str = Field(..., min_length=64, max_length=64)
    effis_vintage: str = Field(..., min_length=1)
    effis_native_res_m: float = Field(..., gt=0)

    cosc_cache_path: str = Field(..., min_length=1)
    cosc_sha256: str = Field(..., min_length=64, max_length=64)
    cosc_vintage: str = Field(..., min_length=1)
    cosc_native_res_m: float = Field(..., gt=0)

    crosswalk_sha: str = Field(..., min_length=64, max_length=64)
    crosswalk_version: str = Field(..., min_length=1)

    grid: GridSpec

    description: str = Field(
        default=(
            "Fuel-class raster: band 1 = EFFIS NFFL fuel-model class (uint8), "
            "band 2 = severity × 100 (uint8). Derived from EFFIS fuel map crosswalk "
            "refined by DGT COSc land-cover. Non-fuel nodata = 255. "
            "COS (species-level) refinement is future work. "
            "EFFIS native resolution is coarser than the 10 m output grid — "
            "see effis_native_res_m."
        )
    )

    @field_validator("aoi_path")
    @classmethod
    def _aoi_must_exist(cls, v: str) -> str:
        if not Path(v).exists():
            raise ValueError(f"aoi_path does not exist: {v}")
        return v
