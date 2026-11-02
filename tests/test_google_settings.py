import json

import pytest

from ekm.google.settings import (
    KEYS,
    TOKEN_URI,
    google_profile,
    load_client_config,
    validate_client_config,
)
from ekm.providers.base import StorageError


def config():
    return {
        "installed": {
            "client_id": "existing.apps.googleusercontent.com",
            "client_secret": "dummy-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("token_uri", "https://evil.example/token"),
        ("auth_uri", "https://evil.example/auth"),
        ("client_id", "not-google"),
        ("client_secret", ""),
        ("redirect_uris", ["https://evil.example/callback"]),
        ("redirect_uris", ["http://localhost/callback?token=x"]),
        ("redirect_uris", []),
    ],
)
def test_google_untrusted_credentials_rejected(field, value):
    data = config()
    data["installed"][field] = value
    with pytest.raises(StorageError):
        validate_client_config(data)


@pytest.mark.parametrize("data", [{"web": {}}, {"type": "service_account"}, [], {}])
def test_google_requires_desktop_client(data):
    with pytest.raises(StorageError, match="Desktop"):
        validate_client_config(data)


def test_google_profile_config_contains_no_secret(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps(config()))
    env = tmp_path / ".env"
    env.write_text("GOOGLE_CREDENTIALS_FILE=credentials.json\n")
    profile = google_profile(env_file=env)
    assert profile.account_type == "google"
    assert profile.client_id == "existing.apps.googleusercontent.com"
    assert profile.google_credentials_file == str(credentials)
    assert "dummy-secret" not in profile.model_dump_json()


def test_google_env_fallback_no_shell_execution(tmp_path):
    marker = tmp_path / "do-not-create"
    env = tmp_path / ".env"
    env.write_text(
        f'GOOGLE_CLIENT_ID=existing.apps.googleusercontent.com\nGOOGLE_CLIENT_SECRET="$(touch {marker})"\n'
    )
    loaded = load_client_config(tmp_path / "missing.json", env_file=env)
    assert loaded["installed"]["client_secret"].startswith("$(touch")
    assert not marker.exists()
