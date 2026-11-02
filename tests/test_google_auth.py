import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from msal_extensions.persistence import PersistenceNotFound

from ekm.google.auth import SCOPES, GoogleAuth
from ekm.google.settings import TOKEN_URI
from ekm.providers.base import StorageError

CLIENT_ID = "test-desktop.apps.googleusercontent.com"


class Cache:
    is_encrypted = True
    value = None

    def load(self):
        if self.value is None:
            raise PersistenceNotFound(location="fake")
        return self.value

    def save(self, value):
        self.value = value


class Credential:
    token = "never-print-access-token"
    refresh_token = "never-print-refresh-token"
    id_token = "never-print-id-token"
    valid = True
    granted_scopes = SCOPES
    scopes = SCOPES
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    refreshes = 0

    def to_json(self):
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": self.refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": "dummy-desktop-secret",
                "token_uri": TOKEN_URI,
            }
        )

    def refresh(self, request):
        self.refreshes += 1
        self.valid = True
        self.token = "new-secret-access-token"


@pytest.fixture
def services(tmp_path, monkeypatch):
    config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": "dummy-desktop-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }
    file = tmp_path / "credentials.json"
    file.write_text(json.dumps(config))
    monkeypatch.setattr("ekm.google.settings.read_google_settings", lambda _: {})
    cache = Cache()
    creds = Credential()
    calls = []
    monkeypatch.setattr("ekm.google.auth.build_encrypted_persistence", lambda _: cache)
    monkeypatch.setattr(
        "ekm.google.auth.Credentials.from_authorized_user_info", lambda *_, **__: creds
    )

    def from_config(config, **kwargs):
        calls.append(("flow", kwargs))
        return SimpleNamespace(
            run_local_server=lambda **kwargs: calls.append(("callback", kwargs)) or creds
        )

    monkeypatch.setattr("ekm.google.auth.InstalledAppFlow.from_client_config", from_config)
    monkeypatch.setattr(
        "ekm.google.auth.id_token.verify_oauth2_token",
        lambda *_, **kwargs: (
            calls.append(("verify", kwargs))
            or {"sub": "google-sub", "email": "person@example.test"}
        ),
    )
    monkeypatch.setattr(
        GoogleAuth,
        "_userinfo",
        staticmethod(lambda _: {"sub": "google-sub", "email": "person@example.test"}),
    )
    auth = GoogleAuth(tmp_path / "profile", file)
    return auth, creds, cache, calls, file


def test_google_pkce_signin_encrypts_tokens_and_pins_verified_subject(services, capsys):
    auth, _, cache, calls, _ = services
    result = auth.login()
    assert result["account"]["sub"] == "google-sub"
    assert calls[0][1]["autogenerate_code_verifier"] is True
    callback = next(data for name, data in calls if name == "callback")
    assert callback["host"] == "localhost" and callback["port"] == 0
    assert "{url}" not in callback["authorization_prompt_message"]
    assert next(data for name, data in calls if name == "verify")["audience"] == CLIENT_ID
    assert "never-print-refresh-token" in cache.value  # Only the fake encrypted store receives it.
    assert "never-print" not in auth.binding_path.read_text()
    assert "never-print" not in capsys.readouterr().out
    assert auth.access_token() == "never-print-access-token"


def test_manual_browser_flow_displays_authorization_request_without_tokens(services, capsys):
    auth, _, _, calls, _ = services
    assert auth.login(open_browser=False)["authenticated"]
    callback = next(data for name, data in calls if name == "callback")
    assert callback["open_browser"] is False
    assert "{url}" in callback["authorization_prompt_message"]
    assert callback["host"] == "localhost" and callback["port"] == 0
    assert "never-print" not in capsys.readouterr().out


def test_google_refresh_persists_and_status_never_displays_tokens(services):
    auth, creds, cache, _, _ = services
    auth.login()
    creds.valid = False
    state = auth.status()
    assert creds.refreshes == 1
    assert state["authenticated"] is True
    assert state["account"]["sub"] == "google-sub"
    assert "new-secret-access-token" in cache.value
    assert "new-secret" not in json.dumps(state)
    assert state["expires_at"]


def test_google_identity_mismatch_rejects_login_and_cached_access(services, monkeypatch):
    auth, credentials, _, _, _ = services
    auth.login()
    monkeypatch.setattr(
        GoogleAuth, "_userinfo", staticmethod(lambda _: {"sub": "different-account"})
    )
    credentials.token = "different-account-access-token"
    with pytest.raises(StorageError, match="another account"):
        auth.access_token()
    assert not auth.status()["authenticated"]
    with pytest.raises(StorageError, match="another account"):
        auth.login()


def test_google_missing_required_grants_never_saves_session(services):
    auth, credentials, cache, _, _ = services
    credentials.granted_scopes = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
    with pytest.raises(StorageError, match="drive.file"):
        auth.login()
    assert cache.value is None
    assert not auth.binding_path.exists()


def test_verified_token_identity_is_reused_until_refresh_or_token_changes(services, monkeypatch):
    auth, credentials, _, _, _ = services
    calls = []
    monkeypatch.setattr(
        GoogleAuth,
        "_userinfo",
        staticmethod(lambda token: calls.append(token) or {"sub": "google-sub"}),
    )
    auth.login()
    auth.access_token()
    auth.access_token()
    assert len(calls) == 1
    credentials.valid = False
    auth.access_token()
    assert len(calls) == 2


def test_custom_env_file_remains_available_when_working_directory_changes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"GOOGLE_CLIENT_ID={CLIENT_ID}\nGOOGLE_CLIENT_SECRET=dummy-secret\n")
    cache = Cache()
    monkeypatch.setattr("ekm.google.auth.build_encrypted_persistence", lambda _: cache)
    other = tmp_path / "another-repository"
    other.mkdir()
    monkeypatch.chdir(other)
    auth = GoogleAuth(tmp_path / "profile", tmp_path / "missing.json", env_file=env)
    assert auth.client_id == CLIENT_ID


def test_installed_google_library_builds_pkce_challenge_without_network():
    from urllib.parse import parse_qs, urlsplit

    from google_auth_oauthlib.flow import InstalledAppFlow

    config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": "dummy-desktop-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(
        config, scopes=SCOPES, autogenerate_code_verifier=True
    )
    flow.redirect_uri = "http://localhost:12345/"
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    parameters = parse_qs(urlsplit(url).query)
    assert parameters["code_challenge_method"] == ["S256"]
    assert parameters["code_challenge"] and state
    assert set(parameters["scope"][0].split()) == set(SCOPES)
    assert "client_secret" not in parameters


def test_oauth_sdk_callback_and_token_diagnostics_are_scoped_and_redacted(
    services, monkeypatch, caplog
):
    import logging

    auth, credentials, _, _, _ = services
    caplog.set_level(logging.DEBUG)
    original = logging.getLogRecordFactory()

    def flow(*args, **kwargs):
        logging.getLogger("google_auth_oauthlib.flow").info(
            "GET /?code=PRIVATE-CODE&state=PRIVATE-STATE"
        )
        logging.getLogger("oauthlib.oauth2.rfc6749.clients.base").debug(
            "access_token=PRIVATE-TOKEN"
        )
        logging.getLogger("ekm.user").info("ordinary application log")
        return SimpleNamespace(run_local_server=lambda **kwargs: credentials)

    monkeypatch.setattr("ekm.google.auth.InstalledAppFlow.from_client_config", flow)
    auth.login()
    assert "PRIVATE-" not in caplog.text
    assert "ordinary application log" in caplog.text
    assert logging.getLogRecordFactory() is original
    credentials.valid = False
    original_refresh = credentials.refresh

    def refresh(request):
        logging.getLogger("requests_oauthlib.oauth2_session").debug("refresh_token=PRIVATE-REFRESH")
        return original_refresh(request)

    credentials.refresh = refresh
    auth.access_token()
    assert "PRIVATE-" not in caplog.text
    assert logging.getLogRecordFactory() is original


def test_oauth_diagnostic_configuration_restored_after_failure(services, monkeypatch):
    import logging

    auth, _, _, _, _ = services
    original = logging.getLogRecordFactory()

    def failed(*args, **kwargs):
        raise RuntimeError("private callback code")

    monkeypatch.setattr("ekm.google.auth.InstalledAppFlow.from_client_config", failed)
    with pytest.raises(StorageError):
        auth.login()
    assert logging.getLogRecordFactory() is original


def test_google_logout_erases_cached_credentials_keeps_binding(services):
    auth, _, cache, _, _ = services
    auth.login()
    auth.logout()
    assert auth.bound_identity()["sub"] == "google-sub"
    assert "refresh_token" not in cache.value and "access-token" not in cache.value
    assert not auth.status()["authenticated"]


def test_google_fail_closed_without_encryption(services, monkeypatch):
    auth, _, _, _, file = services
    monkeypatch.setattr(
        "ekm.google.auth.build_encrypted_persistence", lambda _: SimpleNamespace(is_encrypted=False)
    )
    with pytest.raises(StorageError, match="plaintext fallback"):
        GoogleAuth(auth.directory, file)


@pytest.mark.parametrize("bad", [[], {}, {"sub": "other", "client_id": "wrong"}])
def test_google_malformed_binding_safe(services, bad):
    auth, _, _, _, _ = services
    auth.binding_path.write_text(json.dumps(bad))
    assert not auth.status()["authenticated"]


def test_google_revoked_refresh_session_returns_safe_diagnostic(services):
    auth, creds, _, _, _ = services
    auth.login()
    creds.valid = False

    def rejected(_):
        raise ValueError("private refresh_token=never-print")

    creds.refresh = rejected
    state = auth.status()
    assert not state["authenticated"]
    assert "private" not in json.dumps(state)


def test_google_cached_token_endpoint_cannot_be_redirected(services):
    auth, _, cache, _, _ = services
    auth.login()
    cache.value = json.dumps({"client_id": CLIENT_ID, "token_uri": "https://evil.example/token"})
    with pytest.raises(StorageError, match="invalid"):
        auth.access_token()
