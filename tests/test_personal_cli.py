import argparse
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import ekm.cli_microsoft as commands
from ekm.config import Config, Profile
from ekm.models import Record, verify_integrity
from ekm.providers.base import StorageError


def arguments(*args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    commands.add_microsoft_commands(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(args)


@pytest.fixture
def configured(tmp_path):
    config = Config(tmp_path / "data")
    profile = Profile(
        name="personal",
        account_type="personal",
        client_id=str(uuid4()),
        scopes=["User.Read", "Files.ReadWrite"],
        storage_location="drive-root",
    )
    config.add(profile)
    config.switch(profile.name, approved=True)
    return config


@pytest.fixture
def services(monkeypatch):
    calls = []
    auth = SimpleNamespace(
        login=lambda **kwargs: (
            calls.append(("login", kwargs)) or {"username": "person@example.test"}
        ),
        status=lambda: {
            "authenticated": True,
            "account_type": "personal",
            "granted_scopes": ["User.Read", "Files.ReadWrite"],
        },
        access_token=lambda: "never-visible-token",
        logout=lambda: calls.append(("logout",)),
    )
    monkeypatch.setattr(commands, "MicrosoftAuth", lambda *args, **kwargs: auth)

    class Graph:
        def __init__(self, token):
            self.token = token

        def request(self, method, path):
            calls.append((method, path))
            data = (
                {"id": "user", "displayName": "Person"}
                if path == "/me"
                else {"id": "drive", "driveType": "personal"}
            )
            return SimpleNamespace(json=lambda: data)

        def close(self):
            calls.append(("graph.close",))

    monkeypatch.setattr(commands, "GraphClient", Graph)
    return calls, auth


def test_first_login_requires_profile_destination_approval(tmp_path, services, capsys):
    config = Config(tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text(f"MICROSOFT_CLIENT_ID={uuid4()}\n")
    with pytest.raises(ValueError, match="approval"):
        commands.dispatch(config, arguments("auth", "login", "--env-file", str(env)))
    assert config.profiles() == []
    assert config.active_name() is None
    assert commands.dispatch(
        config, arguments("auth", "login", "--env-file", str(env), "--approve")
    )
    assert config.get().storage_location == "drive-root"
    assert ("login", {"flow": "interactive"}) in services[0]
    assert ("GET", "/me") in services[0]
    assert ("GET", "/me/drive") in services[0]
    assert "never-visible-token" not in capsys.readouterr().out


def test_active_nonpersonal_does_not_silently_switch(configured, services):
    configured.add(Profile(name="demo", account_type="mock"))
    configured.switch("demo", approved=True)
    with pytest.raises(ValueError, match="not personal"):
        commands.dispatch(configured, arguments("auth", "login", "--approve"))
    assert configured.active_name() == "demo"
    assert not services[0]


def test_named_nonpersonal_profile_never_activated_by_auth(configured, services):
    configured.add(Profile(name="demo", account_type="mock"))
    with pytest.raises(ValueError, match="not a personal"):
        commands.dispatch(configured, arguments("auth", "login", "--name", "demo", "--approve"))
    assert configured.active_name() == "personal"


def test_auth_status_safe_and_logout(configured, services, capsys):
    assert commands.dispatch(configured, arguments("auth", "status"))
    status = json.loads(capsys.readouterr().out)
    assert status["authenticated"]
    assert status["onedrive"]["connected"]
    assert "never-visible-token" not in str(status)
    assert commands.dispatch(configured, arguments("auth", "logout"))
    assert ("logout",) in services[0]


def test_unconfigured_status_has_no_keychain_or_network(tmp_path, services, capsys):
    assert commands.dispatch(Config(tmp_path), arguments("auth", "status"))
    assert not json.loads(capsys.readouterr().out)["authenticated"]
    assert services[0] == []


def test_status_reports_connectivity_failure_safely(configured, services, monkeypatch, capsys):
    class FailingGraph:
        def __init__(self, token):
            pass

        def request(self, *args):
            raise StorageError("Microsoft Graph returned HTTP 403; verify consent")

        def close(self):
            pass

    monkeypatch.setattr(commands, "GraphClient", FailingGraph)
    commands.dispatch(configured, arguments("auth", "status"))
    status = json.loads(capsys.readouterr().out)
    assert status["authenticated"]
    assert not status["onedrive"]["connected"]


@pytest.fixture
def engine(configured, monkeypatch):
    real_prepare = commands.Engine.prepare
    calls = []
    provider = SimpleNamespace(
        root_id="owned-root",
        initialize_storage=lambda **kwargs: calls.append(("initialize", kwargs)),
        test_connection=lambda **kwargs: calls.append(("test", kwargs)) or {"success": True},
        upload_file=lambda path, **kwargs: calls.append(("upload", path, kwargs)) or {"etag": "v1"},
        update_document=lambda path, etag, **kwargs: (
            calls.append(("update", path, etag, kwargs)) or {"etag": "v2"}
        ),
        document_metadata=lambda folder, name: {"etag": "v1", "name": name},
        download_document=lambda folder, name, path: (
            calls.append(("download", folder, name, path)) or {"written": str(path)}
        ),
        backup_data=lambda records, name, **kwargs: (
            calls.append(("backup", records, name, kwargs)) or {"name": name}
        ),
        export_data=lambda: [],
    )
    fake = SimpleNamespace(
        profile=configured.get(),
        provider=provider,
        pending=lambda: [],
        close=lambda: calls.append(("close",)),
        sync=lambda **kwargs: {"saved": [], "failed": [], "conflicts": []},
        pull=lambda: {"downloaded": 0, "failed": [], "conflicts": []},
        records=lambda: [],
    )
    fake.prepare = lambda data: real_prepare(fake, data)
    monkeypatch.setattr(commands, "Engine", lambda *args: fake)
    return calls, fake


def test_owned_initialization_requires_approval_and_persists_root(configured, engine):
    with pytest.raises(ValueError, match="approval"):
        commands.dispatch(configured, arguments("onedrive", "initialize"))
    assert not any(c[0] == "initialize" for c in engine[0])
    commands.dispatch(
        configured, arguments("onedrive", "initialize", "--approve", "--adopt-existing")
    )
    assert ("initialize", {"approved": True, "adopt_existing": True}) in engine[0]
    assert configured.get().root_id == "owned-root"


def test_temporary_live_check_is_never_implicit(configured, engine):
    with pytest.raises(ValueError, match="approval"):
        commands.dispatch(configured, arguments("onedrive", "test"))
    commands.dispatch(configured, arguments("onedrive", "test", "--approve"))
    assert ("test", {"approved": True}) in engine[0]


def test_document_upload_update_and_download(configured, engine, tmp_path):
    document = tmp_path / "dashboard.xlsx"
    document.write_bytes(b"generated workbook")
    commands.dispatch(
        configured, arguments("onedrive", "upload", "--input", str(document), "--approve")
    )
    commands.dispatch(
        configured,
        arguments("onedrive", "upload", "--input", str(document), "--etag", "v1", "--approve"),
    )
    assert ("upload", document, {"approved": True}) in engine[0]
    assert ("update", document, "v1", {"approved": True}) in engine[0]
    output = tmp_path / "download.xlsx"
    commands.dispatch(
        configured,
        arguments(
            "onedrive",
            "download",
            "--folder",
            "dashboards",
            "--name",
            document.name,
            "--output",
            str(output),
        ),
    )
    assert ("download", "dashboards", document.name, output) in engine[0]


def test_remote_backup_preserves_local_snapshot_and_refuses_partial_pull(
    configured, engine, tmp_path
):
    output = tmp_path / "knowledge.json"
    commands.dispatch(
        configured, arguments("onedrive", "backup", "--output", str(output), "--approve")
    )
    assert json.loads(output.read_text())["records"] == []
    assert ("backup", [], output.name, {"approved": True}) in engine[0]
    engine[1].pull = lambda: {"conflicts": ["id"], "failed": []}
    other = tmp_path / "partial.json"
    with pytest.raises(StorageError, match="incomplete"):
        commands.dispatch(
            configured, arguments("onedrive", "backup", "--output", str(other), "--approve")
        )
    assert not other.exists()


def test_onedrive_writes_cannot_bypass_active_destination(configured, engine):
    configured.add(Profile(name="demo", account_type="mock"))
    configured.switch("demo", approved=True)
    with pytest.raises(ValueError, match="switch"):
        commands.dispatch(
            configured, arguments("--profile", "personal", "onedrive", "initialize", "--approve")
        )
    assert not any(c[0] == "initialize" for c in engine[0])


def test_sync_reports_nested_download_failure(configured, engine):
    engine[1].sync = lambda **kwargs: {
        "saved": [],
        "conflicts": [],
        "failed": [],
        "pull": {"failed": ["record"], "conflicts": [], "queued": 1},
    }
    with pytest.raises(StorageError, match="incomplete"):
        commands.dispatch(configured, arguments("onedrive", "sync", "--approve"))


def test_backup_uses_remote_records_redacts_and_reseals(configured, engine, tmp_path):
    record = Record(
        kind="task", title="Remote source", description='password="EXAMPLE SECRET"'
    ).sealed()
    engine[1].provider.export_data = lambda: [record.model_dump(mode="json")]
    engine[1].records = lambda: [Record(kind="task", title="Retained stale cache row")]
    output = tmp_path / "sanitized-backup.json"
    commands.dispatch(
        configured, arguments("onedrive", "backup", "--output", str(output), "--approve")
    )
    data = json.loads(output.read_text())
    assert len(data["records"]) == 1
    backed_up = verify_integrity(Record.model_validate(data["records"][0]))
    assert backed_up.title == "Remote source"
    assert "SECRET" not in backed_up.description
    assert backed_up.content_hash != record.content_hash
    assert next(c for c in engine[0] if c[0] == "backup")[1] == data["records"]
