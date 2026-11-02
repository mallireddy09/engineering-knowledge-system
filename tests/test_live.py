"""Optional read-only Microsoft smoke test; normal CI never uses real tokens."""

import os
from pathlib import Path

import pytest

from ekm.config import Config
from ekm.core import make_provider


@pytest.mark.live
@pytest.mark.skipif(
    not (
        os.getenv("EKM_LIVE_PROFILE")
        and os.getenv("EKM_LIVE_HOME")
        and os.getenv("EKM_LIVE_CONFIRM_TEST_ACCOUNT") == "1"
    ),
    reason="requires explicit dedicated test-account configuration",
)
def test_authorized_microsoft_storage_read_only():
    config = Config(Path(os.environ["EKM_LIVE_HOME"]))
    profile = config.get(os.environ["EKM_LIVE_PROFILE"])
    assert profile.account_type in {"personal", "company"}
    if profile.account_type == "personal":
        assert profile.root_id, "Initialize the dedicated test account explicitly before running"
    provider = make_provider(config, profile)
    try:
        assert provider.check_connection()["connected"]
        assert isinstance(provider.list_records(), list)
    finally:
        provider.graph.close()


@pytest.mark.live
@pytest.mark.skipif(
    not (
        os.getenv("EKM_LIVE_PROFILE")
        and os.getenv("EKM_LIVE_HOME")
        and os.getenv("EKM_LIVE_CONFIRM_TEST_ACCOUNT") == "1"
        and os.getenv("EKM_LIVE_APPROVE_TEMP_FILE") == "1"
    ),
    reason="requires explicit approval for an isolated temporary file in a dedicated test account",
)
def test_personal_onedrive_temporary_file_with_explicit_approval():
    config = Config(Path(os.environ["EKM_LIVE_HOME"]))
    profile = config.get(os.environ["EKM_LIVE_PROFILE"])
    assert profile.account_type == "personal" and profile.root_id
    provider = make_provider(config, profile)
    try:
        result = provider.test_connection(approved=True)
        assert result["connected"] and result["cleanup"] == "deleted"
    finally:
        provider.graph.close()
