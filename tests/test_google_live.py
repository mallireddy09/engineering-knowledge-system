"""Opt-in read-only live smoke test; no browser flow or remote mutations."""

import os
from pathlib import Path

import pytest

from ekm.config import Config
from ekm.core import make_provider


@pytest.mark.live
@pytest.mark.skipif(
    not (
        os.getenv("EKM_GOOGLE_LIVE_HOME")
        and os.getenv("EKM_GOOGLE_LIVE_PROFILE")
        and os.getenv("EKM_GOOGLE_LIVE_CONFIRM_TEST_ACCOUNT") == "1"
    ),
    reason="requires explicit dedicated Google test-account configuration",
)
def test_google_owned_storage_read_only():
    config = Config(Path(os.environ["EKM_GOOGLE_LIVE_HOME"]))
    profile = config.get(os.environ["EKM_GOOGLE_LIVE_PROFILE"])
    assert profile.account_type == "google" and profile.root_id
    provider = make_provider(config, profile)
    try:
        assert provider.check_connection()["connected"]
        assert isinstance(provider.list_records(), list)
    finally:
        provider.client.close()
