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
