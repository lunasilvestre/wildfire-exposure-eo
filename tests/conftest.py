"""Shared pytest configuration.

`slow` tests (real model download + inference) are skipped unless the run
opts in with `--runslow`, keeping the default `uv run pytest` gate fast and
network-free. Prompt 09's smoke gate runs them explicitly:

    uv run pytest tests/integration/test_burn_scar_smoke.py -v --runslow
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="run tests marked @pytest.mark.slow (real downloads / inference)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--runslow"):
        return
    skip_slow = pytest.mark.skip(reason="slow test: pass --runslow to enable")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
