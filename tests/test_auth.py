import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ekm.microsoft.auth import CONSUMER_TENANT, MicrosoftAuth
from ekm.providers.base import StorageError


@pytest.fixture
def identity(monkeypatch):
    account = {
        "home_account_id": "home.consumer",
        "realm": CONSUMER_TENANT,
        "local_account_id": "local",
    }
    result = {
        "access_token": "never-print-this",
        "id_token_claims": {"tid": CONSUMER_TENANT, "oid": "local"},
    }
    app = SimpleNamespace(
        initiate_device_flow=lambda **_: {"user_code": "CODE", "message": "Sign in at Microsoft"},
        acquire_token_by_device_flow=lambda _: result,
        get_accounts=lambda: [account],
        acquire_token_silent=lambda *_, **__: result,
        remove_account=lambda _: None,
    )
    monkeypatch.setattr(
        "ekm.microsoft.auth.build_encrypted_persistence",
        lambda _: SimpleNamespace(is_encrypted=True),
    )
    monkeypatch.setattr("ekm.microsoft.auth.PersistedTokenCache", lambda _: object())
    monkeypatch.setattr("ekm.microsoft.auth.msal.PublicClientApplication", lambda *_, **__: app)
    return account, result, app


def test_encrypted_token_login_and_binding(tmp_path, identity, capsys):
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    assert auth.login()["tenant_id"] == CONSUMER_TENANT
    assert auth.access_token() == "never-print-this"
    assert "never-print-this" not in capsys.readouterr().out
    assert "access_token" not in auth.binding_path.read_text()
    auth.logout()
    assert auth.binding_path.exists()


def test_reject_wrong_tenant_and_changed_home_identity(tmp_path, identity):
    account, result, _ = identity
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    auth.login()
    account["home_account_id"] = "another-user"
    with pytest.raises(StorageError, match="another account"):
        auth.login()
    result["id_token_claims"]["tid"] = str(uuid4())
    with pytest.raises(StorageError, match="tenant"):
        auth._validate(account, result)


def test_reject_unencrypted_persistence(tmp_path, identity, monkeypatch):
    monkeypatch.setattr(
        "ekm.microsoft.auth.build_encrypted_persistence",
        lambda _: SimpleNamespace(is_encrypted=False),
    )
    with pytest.raises(StorageError, match="plaintext fallback is disabled"):
        MicrosoftAuth(tmp_path, str(uuid4()), "personal")


def test_company_requires_specific_tenant(tmp_path):
    with pytest.raises(StorageError, match="UUID"):
        MicrosoftAuth(tmp_path, str(uuid4()), "company", "common")


def test_interactive_login_uses_msal_loopback_and_safe_metadata(tmp_path, identity):
    account, result, app = identity
    account["username"] = "person@example.invalid"
    result.update(scope="User.Read Files.ReadWrite", expires_in=3600)
    result["id_token_claims"]["name"] = "Personal User"
    calls = []

    def interactive(**kwargs):
        calls.append(kwargs)
        return result

    app.acquire_token_interactive = interactive
    auth = MicrosoftAuth(
        tmp_path,
        str(uuid4()),
        "personal",
        scopes=["User.Read", "Files.ReadWrite"],
        redirect_uri="http://localhost:8400",
    )
    metadata = auth.login(flow="interactive")
    assert calls == [
        {
            "scopes": ["User.Read", "Files.ReadWrite"],
            "timeout": 180,
            "port": 8400,
            "prompt": "select_account",
        }
    ]
    assert metadata["account"]["username"] == account["username"]
    assert metadata["granted_scopes"] == ["User.Read", "Files.ReadWrite"]
    assert metadata["token_expired"] is False
    assert "never-print-this" not in str(metadata)
    assert auth.bound_identity()["home_account_id"] == "home.consumer"


@pytest.mark.parametrize(
    "uri",
    [
        "https://localhost",
        "http://127.0.0.1",
        "http://example.com",
        "http://localhost/callback",
        "http://localhost?x=1",
        "http://localhost#fragment",
        "http://user@localhost",
        "http://localhost:0",
        "http://localhost:70000",
        "http://localhost:",
    ],
)
def test_reject_unsafe_or_unsupported_redirect(tmp_path, identity, uri):
    with pytest.raises(StorageError, match="redirect|loopback"):
        MicrosoftAuth(tmp_path, str(uuid4()), "personal", redirect_uri=uri)


def test_interactive_default_port_is_assigned_by_msal(tmp_path, identity):
    _, result, app = identity
    app.acquire_token_interactive = lambda **kwargs: result if kwargs["port"] is None else None
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    assert auth.login(flow="interactive")["account_type"] == "personal"


@pytest.mark.parametrize(
    "code,expected",
    [
        (50011, "redirect"),
        (50194, "personal Microsoft"),
        (7000218, "public desktop"),
        (12345, "cancelled"),
    ],
)
def test_safe_application_configuration_diagnostics(tmp_path, identity, code, expected, capsys):
    _, _, app = identity
    app.acquire_token_interactive = lambda **_: {
        "error": "invalid_client",
        "error_codes": [code],
        "error_description": "private-token-never-display",
    }
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    with pytest.raises(StorageError, match=expected) as caught:
        auth.login(flow="interactive")
    assert "private-token-never-display" not in str(caught.value)
    assert "private-token-never-display" not in capsys.readouterr().out
    assert auth.bound_identity() is None


def test_status_uses_silent_refresh_and_never_returns_credentials(tmp_path, identity):
    account, result, app = identity
    account["username"] = "personal@example.invalid"
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    auth.login()
    refreshed = {
        **result,
        "access_token": "fresh-private-token",
        "refresh_token": "private-refresh",
        "scope": "User.Read Files.ReadWrite",
        "expires_in": 3600,
    }
    calls = []

    def silent(*args, **kwargs):
        calls.append(kwargs["account"])
        return refreshed

    app.acquire_token_silent_with_error = silent
    status = auth.status()
    assert status["authenticated"] is True
    assert status["account"]["username"] == account["username"]
    assert status["expires_at"] is not None
    assert status["token_expired"] is False
    assert status["refresh_required"] is False
    assert len(calls) == 1
    assert "private-token" not in str(status) and "private-refresh" not in str(status)
    assert auth.access_token() == "fresh-private-token"


def test_expired_revoked_session_reports_sign_in_without_window(tmp_path, identity):
    _, _, app = identity
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    auth.login()
    app.acquire_token_silent_with_error = lambda *_, **__: {
        "error": "invalid_grant",
        "error_description": "secret context",
    }
    status = auth.status()
    assert not status["authenticated"] and status["refresh_required"]
    assert "auth login" in status["message"]
    assert "secret context" not in str(status)
    with pytest.raises(StorageError, match="revoked"):
        auth.access_token()


def test_status_before_login_and_after_logout_preserves_binding(tmp_path, identity):
    account, _, app = identity
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    assert auth.status()["authenticated"] is False
    auth.login()
    app.remove_account = lambda _: setattr(app, "get_accounts", lambda: [])
    auth.logout()
    assert auth.bound_identity()["home_account_id"] == account["home_account_id"]
    assert not auth.status()["authenticated"]


def test_network_errors_hide_sensitive_exception_details(tmp_path, identity):
    _, _, app = identity
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    auth.login()

    def failed(*_, **__):
        raise OSError("sensitive URL or header")

    app.acquire_token_silent_with_error = failed
    status = auth.status()
    assert "sensitive URL" not in str(status)
    assert "network" in status["message"]


def test_already_expired_token_is_not_used(tmp_path, identity):
    _, result, _ = identity
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    auth.login()
    result["expires_in"] = -10
    assert not auth.status()["authenticated"]
    with pytest.raises(StorageError, match="expired"):
        auth.access_token()


@pytest.mark.parametrize(
    "data",
    [
        [],
        "sensitive malformed identity",
        1,
        None,
        {},
        {"home_account_id": "home.consumer", "tenant_id": CONSUMER_TENANT},
        {"home_account_id": [], "tenant_id": CONSUMER_TENANT, "account_type": "personal"},
        {"home_account_id": " ", "tenant_id": CONSUMER_TENANT, "account_type": "personal"},
        {"home_account_id": "home.consumer", "tenant_id": [], "account_type": "personal"},
        {
            "home_account_id": "home.consumer",
            "tenant_id": CONSUMER_TENANT,
            "account_type": "company",
        },
        {"home_account_id": "home.consumer", "tenant_id": str(uuid4()), "account_type": "personal"},
    ],
)
def test_malformed_binding_fails_safely(tmp_path, identity, data):
    auth = MicrosoftAuth(tmp_path, str(uuid4()), "personal")
    auth.binding_path.write_text(json.dumps(data))
    with pytest.raises(StorageError, match="binding cannot be read"):
        auth.bound_identity()
    with pytest.raises(StorageError, match="binding cannot be read"):
        auth.access_token()
    status = auth.status()
    assert not status["authenticated"]
    assert "sensitive malformed identity" not in str(status)
