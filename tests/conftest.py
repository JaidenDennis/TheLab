"""Hermetic-environment guard for the whole suite.

Settings reads `.env` (env_file) and NQ_* environment variables by design --
that is how production gets its secrets. It is also how the test suite
stopped being hermetic the day a real API key landed in `.env`: tests that
assert credential-missing behaviour found credentials, and one walked far
enough down the live path to open a REAL connection to the data vendor and
hang the suite waiting for market data.

Every test therefore runs with the dotenv source disabled and NQ_* scrubbed
from the process environment. Tests that need credentials set them
explicitly, which is the only version of "has credentials" a test should
ever trust.
"""

import os

import pytest

from nq_agent.config import Settings


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in list(os.environ):
        if name.startswith("NQ_"):
            monkeypatch.delenv(name)
