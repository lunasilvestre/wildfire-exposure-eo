"""End-to-end burn-scar smoke test on the 1 km × 1 km smoke AOI (prompt 09).

Real network, real model download (~2.3 GB, cached by huggingface_hub after
the first run), real MS PC reads, CPU inference, 1-month trailing window.
Guarded by `--runslow`:

    uv run pytest tests/integration/test_burn_scar_smoke.py -v --runslow
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wildfire_exposure_eo.cli import app
from wildfire_exposure_eo.schemas import BurnScarRun

pytestmark = [pytest.mark.slow, pytest.mark.needs_network]

SMOKE_AOI = Path("data/aoi/smoke.geojson")


@pytest.mark.skipif(not SMOKE_AOI.exists(), reason="smoke AOI not checked out")
def test_infer_burn_scar_smoke_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "burn_scar_smoke_{run_id}.tif"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "infer-burn-scar",
            "--smoke",
            "--window-months",
            "1",
            "--device",
            "cpu",
            "--out",
            str(out),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    cogs = sorted(tmp_path.glob("burn_scar_smoke_*.tif"))
    assert len(cogs) == 1, f"expected exactly one COG, got {cogs}"
    cog = cogs[0]

    # provenance sidecar validates and matches the run
    sidecar = cog.with_suffix(".json")
    record = BurnScarRun.model_validate(json.loads(sidecar.read_text()))
    assert record.s2_item_ids, "provenance must list every S2 item consumed"
    assert record.device == "cpu"
    assert record.output_path == str(cog)

    import numpy as np
    import rasterio

    with rasterio.open(cog) as src:
        assert src.crs is not None and src.crs.to_epsg() == 4326
        assert src.nodata == -9999.0
        tags = src.tags()
        embedded = BurnScarRun.model_validate(json.loads(tags["WILDFIRE_EXPOSURE_EO_PROVENANCE"]))
        assert embedded == record
        band = src.read(1)

    valid = band[band != -9999.0]
    assert valid.size > 0, "smoke COG has no valid pixels"
    assert float(np.nanmin(valid)) >= 0.0
    assert float(np.nanmax(valid)) <= 1.0
