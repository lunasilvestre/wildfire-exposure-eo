"""Unit tests for `wildfire_exposure_eo.burn_scar` (prompt 09).

No network, no real model: HF downloads and terratorch are stubbed. The
end-to-end path against live MS PC + the real checkpoint lives in
`tests/integration/test_burn_scar_smoke.py` behind `--runslow`.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from wildfire_exposure_eo import burn_scar
from wildfire_exposure_eo.schemas import (
    HF_MODEL_ID_PLACEHOLDER,
    BurnScarConfig,
    BurnScarRun,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _config(model_id: str = "ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars") -> BurnScarConfig:
    return BurnScarConfig.model_validate(
        {
            "model": {
                "family": "prithvi-eo-2.0",
                "downstream_task": "burn-scar",
                "hf_model_id": model_id,
                "hf_revision_sha": "a3f2c410e45b8ac7417976614528a872f024d831",
                "backbone_param_count": 300_000_000,
                "checkpoint_file": "Prithvi_EO_V2_300M_BurnScars.pt",
                "config_file": "burn_scars_config.yaml",
            },
            "inference": {
                "window_months": 12,
                "s2_max_cloud_cover": 30,
                "binarisation_threshold": 0.5,
                "output_format": "cog",
                "s2_assets": ["B02", "B03", "B04", "B8A", "B11", "B12"],
                "scl_mask_classes": [0, 1, 3, 8, 9, 10, 11],
                "tile_size": 512,
                "tile_stride": 448,
            },
        }
    )


def _run_record(output_path: str = "outputs/cogs/burn_scar_test.tif") -> BurnScarRun:
    return BurnScarRun(
        run_id="20260609T000000Z",
        code_commit_sha="deadbeef",
        created_at_utc=datetime(2026, 6, 9, tzinfo=UTC),
        model_id="ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars",
        model_version="prithvi-eo-2.0:burn-scar:300M",
        hf_revision_sha="a3f2c410e45b8ac7417976614528a872f024d831",
        terratorch_version="1.2.7",
        torch_version="2.11.0",
        device="cpu",
        aoi_path="data/aoi/smoke.geojson",
        aoi_geometry_sha="0" * 64,
        stac_catalog_url="https://planetarycomputer.microsoft.com/api/stac/v1",
        window_start=date(2026, 5, 9),
        window_end=date(2026, 6, 9),
        s2_max_cloud_cover=30,
        s2_item_ids=("S2A_MSIL2A_FAKE_1", "S2B_MSIL2A_FAKE_2"),
        scl_mask_classes=(0, 1, 3, 8, 9, 10, 11),
        binarisation_threshold=0.5,
        output_crs="EPSG:4326",
        resampling="nearest",
        nodata=-9999.0,
        output_path=output_path,
    )


@dataclass
class _FakeItem:
    id: str
    datetime: datetime
    properties: dict[str, Any] = field(default_factory=dict)


class _FakeSearch:
    def __init__(self, items: list[_FakeItem]) -> None:
        self._items = items

    def items(self) -> list[_FakeItem]:
        return self._items


class _FakeClient:
    def __init__(self, items: list[_FakeItem]) -> None:
        self._items = items
        self.search_kwargs: dict[str, Any] = {}

    def search(self, **kwargs: Any) -> _FakeSearch:
        self.search_kwargs = kwargs
        return _FakeSearch(self._items)


# ---------------------------------------------------------------------------
# resolve_prithvi_burn_scar_model
# ---------------------------------------------------------------------------


def test_resolve_raises_on_placeholder_model_id() -> None:
    cfg = _config(model_id=HF_MODEL_ID_PLACEHOLDER)
    with pytest.raises(ValueError, match="placeholder"):
        burn_scar.resolve_prithvi_burn_scar_model(cfg)


def test_resolve_loads_eval_mode_model_from_pinned_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The handle's model must be in eval mode with means/stds from the model's own config."""
    import torch
    import yaml

    model_yaml = tmp_path / "burn_scars_config.yaml"
    model_yaml.write_text(
        yaml.safe_dump({"data": {"init_args": {"means": [0.1] * 6, "stds": [0.2] * 6}}})
    )
    ckpt = tmp_path / "Prithvi_EO_V2_300M_BurnScars.pt"
    ckpt.write_bytes(b"fake")

    requested: list[tuple[str, str, str]] = []

    def fake_download(repo_id: str, filename: str, revision: str) -> str:
        requested.append((repo_id, filename, revision))
        return str(model_yaml if filename.endswith(".yaml") else ckpt)

    fake_hub = types.ModuleType("huggingface_hub")
    fake_hub.hf_hub_download = fake_download  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)

    inner = torch.nn.Linear(2, 2)
    inner.train()  # resolve must flip this to eval

    class _FakeLim:
        model = inner

        @classmethod
        def from_config(cls, config_path: Path, checkpoint_path: Path) -> _FakeLim:
            assert Path(config_path) == model_yaml
            assert Path(checkpoint_path) == ckpt
            return cls()

    fake_terratorch = types.ModuleType("terratorch.cli_tools")
    fake_terratorch.LightningInferenceModel = _FakeLim  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "terratorch.cli_tools", fake_terratorch)

    cfg = _config()
    handle = burn_scar.resolve_prithvi_burn_scar_model(cfg, device="cpu")

    assert handle.model.training is False, "model must be in eval mode (frozen, inference only)"
    assert handle.means == (0.1,) * 6
    assert handle.stds == (0.2,) * 6
    assert handle.hf_model_id == cfg.model.hf_model_id
    assert handle.device == "cpu"
    # every download pinned at the configured revision — never a floating ref
    assert {rev for _, _, rev in requested} == {cfg.model.hf_revision_sha}


# ---------------------------------------------------------------------------
# query_recent_s2
# ---------------------------------------------------------------------------


def test_query_recent_s2_orders_deterministically() -> None:
    from shapely.geometry import box

    items = [
        _FakeItem("S2B_LATER", datetime(2026, 5, 2, 11, 0, tzinfo=UTC)),
        _FakeItem("S2A_SAME_TS_B", datetime(2026, 5, 1, 11, 0, tzinfo=UTC)),
        _FakeItem("S2A_SAME_TS_A", datetime(2026, 5, 1, 11, 0, tzinfo=UTC)),
        _FakeItem("S2A_EARLIEST", datetime(2026, 4, 30, 11, 0, tzinfo=UTC)),
    ]
    client = _FakeClient(items)
    out = burn_scar.query_recent_s2(
        box(-8.6, 40.6, -8.2, 40.9),
        3,
        max_cloud_cover=30,
        window_end=date(2026, 6, 9),
        client=client,  # pyright: ignore[reportArgumentType]
    )
    assert [it.id for it in out] == [
        "S2A_EARLIEST",
        "S2A_SAME_TS_A",
        "S2A_SAME_TS_B",
        "S2B_LATER",
    ]
    assert client.search_kwargs["collections"] == ["sentinel-2-l2a"]
    assert client.search_kwargs["datetime"] == "2026-03-09/2026-06-09"
    assert client.search_kwargs["query"] == {"eo:cloud_cover": {"lte": 30}}


def test_query_recent_s2_is_stable_across_input_order() -> None:
    from shapely.geometry import box

    items = [
        _FakeItem("S2_B", datetime(2026, 5, 1, tzinfo=UTC)),
        _FakeItem("S2_A", datetime(2026, 5, 1, tzinfo=UTC)),
    ]
    aoi = box(-8.6, 40.6, -8.2, 40.9)
    first = burn_scar.query_recent_s2(
        aoi,
        1,
        window_end=date(2026, 6, 9),
        client=_FakeClient(items),  # pyright: ignore[reportArgumentType]
    )
    second = burn_scar.query_recent_s2(
        aoi,
        1,
        window_end=date(2026, 6, 9),
        client=_FakeClient(list(reversed(items))),  # pyright: ignore[reportArgumentType]
    )
    assert [it.id for it in first] == [it.id for it in second] == ["S2_A", "S2_B"]


def test_months_back_clamps_day() -> None:
    assert burn_scar.months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert burn_scar.months_back(date(2026, 6, 9), 12) == date(2025, 6, 9)
    assert burn_scar.months_back(date(2026, 1, 15), 2) == date(2025, 11, 15)


# ---------------------------------------------------------------------------
# composite reducers (WU-10)
# ---------------------------------------------------------------------------


def _legacy_fmax_composite(stack: Any) -> Any:
    """The pre-WU-10 accumulator: composite = np.fmax(composite, prob) per scene."""
    import numpy as np

    composite = stack[0].copy()
    for layer in stack[1:]:
        composite = np.fmax(composite, layer)
    return composite


def test_reducer_max_backward_compat() -> None:
    """`reducer="max"` must be bit-identical to the old np.fmax accumulator."""
    import numpy as np

    nan = np.nan
    stack = np.array(
        [
            [[0.10, nan], [0.30, nan]],
            [[0.90, 0.20], [nan, nan]],
            [[nan, 0.70], [0.50, nan]],
        ],
        dtype=np.float32,
    )
    reduced = burn_scar.reduce_stack(stack, "max")
    legacy = _legacy_fmax_composite(stack)
    assert np.array_equal(reduced, legacy, equal_nan=True)
    # the all-NaN pixel stays NaN (no scene observed it)
    assert np.isnan(reduced[1, 1])


def test_reducer_p85() -> None:
    """`reducer="p85"` returns the NaN-ignoring 85th percentile per pixel."""
    import numpy as np

    # 10 scenes, one pixel: scores 0.0..0.9; p85 interpolates between 0.7/0.8.
    scores = np.linspace(0.0, 0.9, 10, dtype=np.float32)
    stack = scores.reshape(10, 1, 1)
    reduced = burn_scar.reduce_stack(stack, "p85")
    assert reduced[0, 0] == pytest.approx(np.percentile(scores, 85), abs=1e-5)
    # a single high spike does not survive p85 (it would under max)
    spiky = np.zeros((10, 1, 1), dtype=np.float32)
    spiky[0, 0, 0] = 1.0
    assert burn_scar.reduce_stack(spiky, "p85")[0, 0] == pytest.approx(0.0, abs=1e-6)
    assert burn_scar.reduce_stack(spiky, "max")[0, 0] == pytest.approx(1.0)


def test_reducer_percentile_ignores_nan() -> None:
    import numpy as np

    nan = np.nan
    stack = np.array([[[0.2]], [[nan]], [[0.4]], [[0.6]]], dtype=np.float32)
    # median over observed {0.2,0.4,0.6} == 0.4, NaN ignored
    assert burn_scar.reduce_stack(stack, "median")[0, 0] == pytest.approx(0.4, abs=1e-6)
    all_nan = np.full((3, 1, 1), nan, dtype=np.float32)
    assert np.isnan(burn_scar.reduce_stack(all_nan, "median")[0, 0])


def test_reducer_consensus() -> None:
    """`consensus_5` flags 1.0 only when >5/10 observing scenes score >=0.5."""
    import numpy as np

    # pixel A: 6 of 10 scenes >=0.5  -> share 0.6 > 0.5 -> 1.0
    # pixel B: 5 of 10 scenes >=0.5  -> share 0.5, NOT > 0.5 -> 0.0
    a = np.array([0.9] * 6 + [0.1] * 4, dtype=np.float32)
    b = np.array([0.9] * 5 + [0.1] * 5, dtype=np.float32)
    stack = np.stack([a, b], axis=1).reshape(10, 1, 2)
    reduced = burn_scar.reduce_stack(stack, "consensus_5")
    assert reduced[0, 0] == pytest.approx(1.0)
    assert reduced[0, 1] == pytest.approx(0.0)


def test_reducer_consensus_ignores_nan_in_denominator() -> None:
    import numpy as np

    nan = np.nan
    # 4 observed scenes (2 NaN ignored), 3 of 4 >=0.5 -> share 0.75 > 0.5 -> 1.0
    col = np.array([0.9, 0.9, 0.9, 0.1, nan, nan], dtype=np.float32)
    stack = col.reshape(6, 1, 1)
    assert burn_scar.reduce_stack(stack, "consensus_5")[0, 0] == pytest.approx(1.0)
    all_nan = np.full((3, 1, 1), nan, dtype=np.float32)
    assert np.isnan(burn_scar.reduce_stack(all_nan, "consensus_5")[0, 0])


def test_reducer_unknown_raises() -> None:
    import numpy as np

    stack = np.zeros((2, 1, 1), dtype=np.float32)
    with pytest.raises(ValueError, match="unrecognised reducer"):
        burn_scar.reduce_stack(stack, "bogus")
    with pytest.raises(ValueError, match="consensus_N"):
        burn_scar.reduce_stack(stack, "consensus_99")
    with pytest.raises(ValueError, match="consensus_N"):
        burn_scar.reduce_stack(stack, "consensus_x")


def test_reduce_stack_blockwise_matches_unblocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block-wise reduction must equal the unblocked reduce for every reducer."""
    import numpy as np

    rng = np.random.default_rng(42)
    stack = rng.random((7, 600, 11), dtype=np.float64).astype(np.float32)
    # punch some NaNs so masked pixels exercise the all-NaN handling
    stack[:, rng.integers(0, 600, 50), rng.integers(0, 11, 50)] = np.nan
    monkeypatch.setattr(burn_scar, "_REDUCE_BLOCK_ROWS", 256)
    for reducer in ("max", "median", "p85", "p90", "consensus_3"):
        blocked = burn_scar._reduce_stack_blockwise(stack, reducer)
        whole = burn_scar.reduce_stack(stack, reducer)
        assert np.array_equal(blocked, whole, equal_nan=True), reducer


# ---------------------------------------------------------------------------
# fire-season filter (WU-10)
# ---------------------------------------------------------------------------


def test_season_filter_drops_off_season_items() -> None:
    items = [
        _FakeItem("S2_MAY", datetime(2025, 5, 15, tzinfo=UTC)),
        _FakeItem("S2_JUL", datetime(2025, 7, 15, tzinfo=UTC)),
        _FakeItem("S2_OCT", datetime(2025, 10, 1, tzinfo=UTC)),
        _FakeItem("S2_NOV", datetime(2025, 11, 1, tzinfo=UTC)),
    ]
    kept = burn_scar.filter_to_season(
        items,  # pyright: ignore[reportArgumentType]
        season_start_month=6,
        season_end_month=10,
    )
    assert [it.id for it in kept] == ["S2_JUL", "S2_OCT"]


def test_season_filter_noop_for_full_year() -> None:
    items = [
        _FakeItem("S2_JAN", datetime(2025, 1, 1, tzinfo=UTC)),
        _FakeItem("S2_DEC", datetime(2025, 12, 1, tzinfo=UTC)),
    ]
    kept = burn_scar.filter_to_season(
        items,  # pyright: ignore[reportArgumentType]
        season_start_month=1,
        season_end_month=12,
    )
    assert [it.id for it in kept] == ["S2_JAN", "S2_DEC"]


def test_season_filter_wraps_year_end() -> None:
    items = [
        _FakeItem("S2_NOV", datetime(2025, 11, 1, tzinfo=UTC)),
        _FakeItem("S2_JAN", datetime(2026, 1, 1, tzinfo=UTC)),
        _FakeItem("S2_JUN", datetime(2025, 6, 1, tzinfo=UTC)),
    ]
    kept = burn_scar.filter_to_season(
        items,  # pyright: ignore[reportArgumentType]
        season_start_month=11,
        season_end_month=2,
    )
    assert [it.id for it in kept] == ["S2_NOV", "S2_JAN"]


# ---------------------------------------------------------------------------
# COG writer
# ---------------------------------------------------------------------------


def _tiny_dataarray() -> Any:
    import numpy as np
    import rioxarray  # noqa: F401
    import xarray as xr

    data = np.array([[0.1, 0.9], [np.nan, 0.5]], dtype=np.float32)
    da = xr.DataArray(
        data,
        dims=("y", "x"),
        coords={"y": [40.7005, 40.6995], "x": [-8.4005, -8.3995]},
    )
    return da.rio.write_crs("EPSG:4326")


def test_write_burn_scar_cog_embeds_provenance_tags(tmp_path: Path) -> None:
    import rasterio

    out = tmp_path / "burn_scar_test.tif"
    record = _run_record(output_path=str(out))
    path = burn_scar.write_burn_scar_cog(_tiny_dataarray(), out, record)

    assert path == out
    with rasterio.open(path) as src:
        tags = src.tags()
        embedded = json.loads(tags["WILDFIRE_EXPOSURE_EO_PROVENANCE"])
        assert embedded == record.model_dump(mode="json")
        assert tags["RUN_ID"] == record.run_id
        assert tags["MODEL_ID"] == record.model_id
        assert "not a calibrated probability" in tags["VALUE_DESCRIPTION"]
        assert src.nodata == burn_scar.NODATA
        assert src.crs is not None and src.crs.to_epsg() == 4326


def test_write_burn_scar_cog_writes_validating_sidecar(tmp_path: Path) -> None:
    out = tmp_path / "burn_scar_test.tif"
    record = _run_record(output_path=str(out))
    burn_scar.write_burn_scar_cog(_tiny_dataarray(), out, record)

    sidecar = out.with_suffix(".json")
    assert sidecar.exists()
    assert BurnScarRun.model_validate(json.loads(sidecar.read_text())) == record


def test_write_burn_scar_cog_maps_nan_to_nodata(tmp_path: Path) -> None:
    import numpy as np
    import rasterio

    out = tmp_path / "burn_scar_test.tif"
    burn_scar.write_burn_scar_cog(_tiny_dataarray(), out, _run_record(output_path=str(out)))
    with rasterio.open(out) as src:
        band = src.read(1)
    assert band[1, 0] == pytest.approx(burn_scar.NODATA)
    assert np.isclose(band[0, 1], 0.9, atol=1e-6)


# ---------------------------------------------------------------------------
# scene helpers
# ---------------------------------------------------------------------------


def test_boa_offset_by_processing_baseline() -> None:
    new = _FakeItem("a", datetime(2026, 1, 1, tzinfo=UTC), {"s2:processing_baseline": "05.11"})
    old = _FakeItem("b", datetime(2018, 1, 1, tzinfo=UTC), {"s2:processing_baseline": "03.01"})
    missing = _FakeItem("c", datetime(2026, 1, 1, tzinfo=UTC), {})
    assert burn_scar._boa_offset(new) == 1000.0  # pyright: ignore[reportArgumentType]
    assert burn_scar._boa_offset(old) == 0.0  # pyright: ignore[reportArgumentType]
    assert burn_scar._boa_offset(missing) == 0.0  # pyright: ignore[reportArgumentType]


def test_item_epsg_reads_both_proj_forms() -> None:
    legacy = _FakeItem("a", datetime(2026, 1, 1, tzinfo=UTC), {"proj:epsg": 32629})
    modern = _FakeItem("b", datetime(2026, 1, 1, tzinfo=UTC), {"proj:code": "EPSG:32629"})
    neither = _FakeItem("c", datetime(2026, 1, 1, tzinfo=UTC), {})
    assert burn_scar._item_epsg(legacy) == 32629  # pyright: ignore[reportArgumentType]
    assert burn_scar._item_epsg(modern) == 32629  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="proj"):
        burn_scar._item_epsg(neither)  # pyright: ignore[reportArgumentType]


def test_pad_to_min_pads_small_inputs_and_keeps_large() -> None:
    import numpy as np

    small = np.zeros((6, 100, 120), dtype=np.float32)
    padded, h, w = burn_scar._pad_to_min(small, 512, 512)
    assert padded.shape == (6, 512, 512)
    assert (h, w) == (100, 120)

    large = np.zeros((6, 600, 700), dtype=np.float32)
    same, h2, w2 = burn_scar._pad_to_min(large, 512, 512)
    assert same.shape == (6, 600, 700)
    assert (h2, w2) == (600, 700)


# ---------------------------------------------------------------------------
# BurnScarRun schema
# ---------------------------------------------------------------------------


def test_burn_scar_run_rejects_extra_fields() -> None:
    from pydantic import ValidationError

    payload = _run_record().model_dump(mode="json")
    payload["surprise"] = True
    with pytest.raises(ValidationError):
        BurnScarRun.model_validate(payload)


def test_burn_scar_run_round_trips_json() -> None:
    record = _run_record()
    assert BurnScarRun.model_validate(json.loads(record.model_dump_json())) == record


def test_burn_scar_run_reducer_field() -> None:
    """The WU-10 reducer/season fields round-trip and default for old records."""
    # explicit value survives a JSON round-trip
    record = _run_record().model_copy(
        update={"reducer": "p85", "season_start_month": 6, "season_end_month": 10}
    )
    again = BurnScarRun.model_validate(json.loads(record.model_dump_json()))
    assert again.reducer == "p85"
    assert (again.season_start_month, again.season_end_month) == (6, 10)

    # a provenance dict written BEFORE WU-10 (no reducer/season keys) still
    # deserialises, defaulting to the backward-compatible max / full-year window
    legacy = _run_record().model_dump(mode="json")
    for key in ("reducer", "season_start_month", "season_end_month"):
        legacy.pop(key, None)
    restored = BurnScarRun.model_validate(legacy)
    assert restored.reducer == "max"
    assert (restored.season_start_month, restored.season_end_month) == (1, 12)


def test_burn_scar_inference_config_reducer_defaults() -> None:
    """An inference config without the WU-10 keys defaults to max / full year."""
    cfg = _config()
    # the test _config() omits reducer/season — must default for backward compat
    assert cfg.inference.reducer == "max"
    assert (cfg.inference.season_start_month, cfg.inference.season_end_month) == (1, 12)


# ---------------------------------------------------------------------------
# STAC item writer
# ---------------------------------------------------------------------------


def _write_tiny_cog(tmp_path: Path) -> tuple[Path, BurnScarRun]:
    out = tmp_path / "outputs" / "cogs" / "burn_scar_test.tif"
    record = _run_record(output_path=str(out))
    burn_scar.write_burn_scar_cog(_tiny_dataarray(), out, record)
    return out, record


def test_write_stac_item_creates_catalog_collection_item(tmp_path: Path) -> None:
    import pystac

    cog, record = _write_tiny_cog(tmp_path)
    stac_root = tmp_path / "stac"
    item_path = burn_scar.write_stac_item(record, cog, stac_root=stac_root)

    assert (stac_root / "catalog.json").exists()
    assert item_path.exists()

    catalog = pystac.Catalog.from_file(str(stac_root / "catalog.json"))
    collection = catalog.get_child("burn-scar-recent")
    assert collection is not None
    item = pystac.Item.from_file(str(item_path))
    assert item.id == f"burn-scar-{record.run_id}"
    assert item.properties["wildfire_exposure_eo:provenance"] == record.model_dump(mode="json")
    asset = item.assets["burn_scar_probability"]
    assert "inference probability" in (asset.title or "")
    # self-contained: the asset href must be relative, pointing at the COG
    assert not Path(asset.href).is_absolute()
    resolved = (item_path.parent / asset.href).resolve()
    assert resolved == cog.resolve()


def test_write_stac_item_rejects_duplicate_run_id(tmp_path: Path) -> None:
    cog, record = _write_tiny_cog(tmp_path)
    stac_root = tmp_path / "stac"
    burn_scar.write_stac_item(record, cog, stac_root=stac_root)
    with pytest.raises(ValueError, match="already exists"):
        burn_scar.write_stac_item(record, cog, stac_root=stac_root)


def test_write_stac_item_appends_to_existing_catalog(tmp_path: Path) -> None:
    import pystac

    cog, record = _write_tiny_cog(tmp_path)
    stac_root = tmp_path / "stac"
    burn_scar.write_stac_item(record, cog, stac_root=stac_root)

    second = record.model_copy(update={"run_id": "20260610T000000Z"})
    burn_scar.write_stac_item(second, cog, stac_root=stac_root)

    catalog = pystac.Catalog.from_file(str(stac_root / "catalog.json"))
    collection = catalog.get_child("burn-scar-recent")
    assert collection is not None
    ids = {it.id for it in collection.get_items()}
    assert ids == {"burn-scar-20260609T000000Z", "burn-scar-20260610T000000Z"}


# ---------------------------------------------------------------------------
# scene retry (transient blob failures / mid-scene SAS expiry)
# ---------------------------------------------------------------------------


def _retry_setup(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | None]
) -> tuple[Any, list[int]]:
    """Patch _scene_probability to fail per `outcomes` (None = succeed).

    The fake 20x20 grid sits in EPSG:32629 over the test AOI box
    (-8.41, 40.69, -8.39, 40.71) so the reproject + clip path stays real.
    """
    import numpy as np

    calls: list[int] = []
    prob = np.full((20, 20), 0.5, dtype=np.float32)
    xs = np.linspace(548_500.0, 551_500.0, 20)
    ys = np.linspace(4_506_500.0, 4_503_500.0, 20)

    def fake_scene(item: Any, handle: Any, **_kwargs: Any) -> Any:
        outcome = outcomes[len(calls)]
        calls.append(1)
        if outcome is not None:
            raise outcome
        return prob, xs, ys

    monkeypatch.setattr(burn_scar, "_scene_probability", fake_scene)
    monkeypatch.setattr(burn_scar, "_item_epsg", lambda _it: 32629)
    monkeypatch.setattr(burn_scar.time, "sleep", lambda _s: None)
    return prob, calls


def _retry_handle() -> burn_scar.ModelHandle:
    return burn_scar.ModelHandle(
        model=object(),
        hf_model_id="x/y",
        hf_revision_sha="z",
        model_version="v",
        checkpoint_path=Path("ckpt"),
        model_config_path=Path("cfg"),
        means=(0.0,) * 6,
        stds=(1.0,) * 6,
        device="cpu",
    )


def test_scene_failure_retries_with_fresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from shapely.geometry import box

    _, calls = _retry_setup(monkeypatch, [RuntimeError("read failed"), None])
    burn_scar._SAS_CACHE["sentinel-2-l2a"] = ("stale", datetime(2099, 1, 1, tzinfo=UTC))

    item = _FakeItem("S2_X", datetime(2026, 5, 1, tzinfo=UTC), {"proj:epsg": 32629})
    da = burn_scar.infer_burn_probability(
        [item],  # pyright: ignore[reportArgumentType]
        _retry_handle(),
        box(-8.41, 40.69, -8.39, 40.71),
        s2_assets=("B02", "B03", "B04", "B8A", "B11", "B12"),
        scl_mask_classes=(0,),
    )
    assert len(calls) == 2, "first failure must be retried"
    assert "sentinel-2-l2a" not in burn_scar._SAS_CACHE, "retry must drop the cached token"
    assert float(da.max()) == pytest.approx(0.5)


def test_scene_failure_raises_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    from shapely.geometry import box

    _, calls = _retry_setup(monkeypatch, [RuntimeError("boom")] * burn_scar._SCENE_ATTEMPTS)
    item = _FakeItem("S2_X", datetime(2026, 5, 1, tzinfo=UTC), {"proj:epsg": 32629})
    with pytest.raises(RuntimeError, match="failed after 3 attempt"):
        burn_scar.infer_burn_probability(
            [item],  # pyright: ignore[reportArgumentType]
            _retry_handle(),
            box(-8.41, 40.69, -8.39, 40.71),
            s2_assets=("B02", "B03", "B04", "B8A", "B11", "B12"),
            scl_mask_classes=(0,),
        )
    assert len(calls) == burn_scar._SCENE_ATTEMPTS


def test_gdal_http_defaults_set_but_never_clobber(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in burn_scar._GDAL_HTTP_DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GDAL_HTTP_TIMEOUT", "7")

    burn_scar._apply_gdal_http_defaults()

    assert os.environ["GDAL_HTTP_TIMEOUT"] == "7", "pre-set values must win"
    for key, value in burn_scar._GDAL_HTTP_DEFAULTS.items():
        if key != "GDAL_HTTP_TIMEOUT":
            assert os.environ[key] == value
