from uuid import uuid4

import pytest

from ekm.microsoft.settings import KEYS, personal_profile


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def test_existing_public_client_env_settings(tmp_path):
    client_id = str(uuid4())
    path = tmp_path / ".env"
    path.write_text(
        f'MICROSOFT_CLIENT_ID={client_id}\nMICROSOFT_SCOPES="User.Read Files.ReadWrite" # comment\n'
    )
    profile = personal_profile(env_file=path)
    assert profile.client_id == client_id
    assert profile.scopes == ["User.Read", "Files.ReadWrite"]
    assert profile.storage_location == "drive-root"
    assert profile.redirect_uri == "http://localhost"


def test_environment_override_and_no_shell_execution(tmp_path, monkeypatch):
    marker = tmp_path / "should-not-exist"
    path = tmp_path / ".env"
    path.write_text(f"MICROSOFT_CLIENT_ID=$(touch {marker})\nCLIENT_SECRET=ignored\n")
    client_id = str(uuid4())
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", client_id)
    assert personal_profile(env_file=path).client_id == client_id
    assert not marker.exists()


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "MICROSOFT_ACCOUNT_TYPE=company\n",
        "MICROSOFT_SCOPES=Files.ReadWrite.All\n",
        "EKM_STORAGE_PROVIDER=sharepoint\n",
    ],
)
def test_missing_or_unsupported_settings_explain_fix(tmp_path, suffix):
    path = tmp_path / ".env"
    path.write_text((f"MICROSOFT_CLIENT_ID={uuid4()}\n" if suffix else "") + suffix)
    with pytest.raises(ValueError):
        personal_profile(env_file=path)
