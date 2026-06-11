"""Integration smoke test for wildfire_exposure_eo.fuel.

Runs the full fuel-layer pipeline on the smoke AOI using the real WU-3 cached
rasters. Kept separate from unit tests so `uv run pytest` (no --runslow) still
exercises all unit tests; this test requires the WU-3 cache on disk.

Gate: uv run wildfire-exposure-eo fuel-layer --smoke
"""

from __future__ import annotations

from pathlib import Path

import pytest
import rasterio
from rasterio.crs import CRS

from wildfire_exposure_eo import fuel as fl
from wildfire_exposure_eo.schemas.fuel_layer import FuelLayerProvenance

CACHE_DIR = Path("data/cache")
EFFIS_PATH = CACHE_DIR / "effis" / "effis_european_fuel_map.tif"
COSC_PATH = CACHE_DIR / "dgt-cosc" / "cosc_2024_pre_verao.tif"
CROSSWALK_PATH = Path("config/fuel_crosswalk.yaml")
SMOKE_AOI = Path("data/aoi/smoke.geojson")


def _skip_if_no_cache() -> None:
    for p in [EFFIS_PATH, COSC_PATH, CROSSWALK_PATH, SMOKE_AOI]:
        if not p.exists():
            pytest.skip(f"WU-3 cache / crosswalk not available: {p}")


@pytest.mark.slow
def test_fuel_smoke_cog_opens_with_correct_crs_and_bands(tmp_path: Path) -> None:
    """End-to-end smoke run: COG opens, CRS = EPSG:32629, 2 bands, sidecar validates."""
    _skip_if_no_cache()

    import hashlib

    cw = fl.load_crosswalk(CROSSWALK_PATH)
    grid = fl.pilot_grid(SMOKE_AOI)

    assert grid.crs == "EPSG:32629"
    assert grid.width > 0 and grid.height > 0
    assert grid.resolution_m == 10

    klass, sev = fl.reclass_effis(EFFIS_PATH, grid, cw)
    klass, sev = fl.refine_with_cosc(klass, sev, COSC_PATH, grid, cw)

    aoi_sha = hashlib.sha256(SMOKE_AOI.read_bytes()).hexdigest()
    effis_sha = fl._sha256_file(EFFIS_PATH)
    cosc_sha = fl._sha256_file(COSC_PATH)

    with rasterio.open(EFFIS_PATH) as ds:
        effis_res_m = float(abs(ds.res[0]))
    with rasterio.open(COSC_PATH) as ds:
        cosc_res_m = float(abs(ds.res[0]))

    prov = FuelLayerProvenance(
        run_id="test_smoke",
        code_commit_sha="a" * 40,
        aoi_path=str(SMOKE_AOI),
        aoi_geometry_sha=aoi_sha,
        effis_cache_path=str(EFFIS_PATH),
        effis_sha256=effis_sha,
        effis_vintage="2023",
        effis_native_res_m=effis_res_m,
        cosc_cache_path=str(COSC_PATH),
        cosc_sha256=cosc_sha,
        cosc_vintage="2024_pre_verao",
        cosc_native_res_m=cosc_res_m,
        crosswalk_sha=cw.crosswalk_sha,
        crosswalk_version=cw.version,
        grid=grid,
    )

    out_path = tmp_path / "fuel_class_smoke_test.tif"
    fl.write_fuel_cog(klass, sev, grid, out_path, provenance=prov)

    assert out_path.exists(), "COG not written"

    # Assert COG opens, CRS is EPSG:32629, 2 bands
    with rasterio.open(out_path) as ds:
        assert ds.count == 2, f"Expected 2 bands, got {ds.count}"
        assert ds.crs is not None, "COG has no CRS"
        assert ds.crs == CRS.from_epsg(32629), f"CRS mismatch: {ds.crs}"
        assert ds.dtypes == ("uint8", "uint8"), f"dtype mismatch: {ds.dtypes}"
        assert ds.nodata == 255, f"nodata mismatch: {ds.nodata}"
        assert ds.width == grid.width
        assert ds.height == grid.height

    # Sidecar validates as FuelLayerProvenance
    sidecar = out_path.with_suffix(".json")
    assert sidecar.exists(), "Sidecar JSON not written"
    import json

    sidecar_data = json.loads(sidecar.read_text())
    prov_back = FuelLayerProvenance.model_validate(sidecar_data)
    assert prov_back.run_id == "test_smoke"
    assert prov_back.crosswalk_version == cw.version


@pytest.mark.slow
def test_fuel_smoke_contains_fuel_pixels(tmp_path: Path) -> None:
    """Smoke run produces at least some fuel pixels (non-zero class, non-255 nodata)."""
    _skip_if_no_cache()
    import numpy as np

    cw = fl.load_crosswalk(CROSSWALK_PATH)
    grid = fl.pilot_grid(SMOKE_AOI)
    klass, sev = fl.reclass_effis(EFFIS_PATH, grid, cw)
    klass, sev = fl.refine_with_cosc(klass, sev, COSC_PATH, grid, cw)

    fuel_pixels = int(np.sum((klass > 0) & (klass < 255)))
    assert fuel_pixels > 0, "Smoke AOI should contain at least some EFFIS fuel pixels"

    # Severity values in range 0-100 for fuel pixels
    fuel_mask = (klass > 0) & (klass < 255)
    if fuel_mask.any():
        assert sev[fuel_mask].max() <= 100
        assert sev[fuel_mask].min() >= 0
