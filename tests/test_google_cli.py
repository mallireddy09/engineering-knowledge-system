import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from ekm.cli import main
from ekm.config import Config, Profile
from ekm.models import Kind
from ekm.providers.mock import MockStorageProvider


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("EKM_HOME", str(tmp_path / "state"))
    import ekm.cli_google as cli

    calls = []

    def profile(name="google", env_file=None):
        return Profile(
            name=name,
            account_type="google",
            client_id="example.apps.googleusercontent.com",
            google_credentials_file=str(tmp_path / "credentials.json"),
        )

    monkeypatch.setattr(cli, "google_profile", profile)
    auth = SimpleNamespace(
        login=lambda: {"authenticated": True, "account": {"sub": "subject"}},
        status=lambda: {"authenticated": True, "provider": "google"},
        logout=lambda: calls.append("logout"),
    )
    monkeypatch.setattr(cli, "GoogleAuth", lambda *args, **kwargs: auth)

    def provider(config, selected):
        result = MockStorageProvider(config.profile_dir(selected) / "remote")
        result.initialize_storage(approved=True)
        return result

    monkeypatch.setattr("ekm.core.make_provider", provider)
    return Config(), calls


def test_google_login_isolated_and_provider_switch_requires_approval(setup, capsys):
    config, calls = setup
    microsoft = Profile(name="personal", account_type="personal", client_id=str(uuid4()))
    config.add(microsoft)
    config.switch(microsoft.name, approved=True)
    assert main(["auth", "google", "login"]) == 1
    assert config.active_name() == "personal"
    assert main(["auth", "google", "login", "--name", "google"]) == 1
    assert config.active_name() == "personal"
    assert main(["auth", "google", "login", "--name", "google", "--approve"]) == 0
    assert config.active_name() == "google"
    assert main(["auth", "google", "status"]) == 0
    assert main(["provider", "switch", "microsoft"]) == 1
    assert config.active_name() == "google"
    assert main(["provider", "switch", "microsoft", "--approve"]) == 0
    assert config.active_name() == "personal"
    assert not calls
    capsys.readouterr()
    assert main(["provider", "list"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["supported"] == ["microsoft", "google"]
    assert len(result["profiles"]) == 2
    assert main(["--profile", "google", "auth", "google", "status"]) == 0
    assert config.active_name() == "personal"
    assert main(["--profile", "google", "auth", "google", "logout"]) == 1
    assert not calls


def test_google_generic_workflows_keep_microsoft_history_separate(setup, tmp_path, capsys):
    config, _ = setup
    assert main(["auth", "google", "login", "--approve"]) == 0
    source = tmp_path / "task.json"
    source.write_text(
        json.dumps(
            {
                "kind": "task",
                "title": "Google engineering knowledge",
                "project_id": "events",
                "technologies": ["Kafka"],
            }
        )
    )
    assert main(["log", "--input", str(source), "--approve"]) == 0
    capsys.readouterr()
    assert main(["search", "Kafka"]) == 0
    assert "Google engineering knowledge" in capsys.readouterr().out
    assert main(["decision", "list"]) == 0
    assert main(["debt", "list"]) == 0
    assert main(["review"]) == 0
    assert main(["quiz"]) == 0
    assert main(["weekly"]) == 0
    config.add(Profile(name="personal", account_type="personal", client_id=str(uuid4())))
    assert main(["provider", "switch", "microsoft", "--approve"]) == 0
    capsys.readouterr()
    assert main(["search", "Kafka"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["provider", "switch", "google", "--approve"]) == 0
    backup = tmp_path / "export.json"
    assert main(["export", "--output", str(backup)]) == 0
    assert len(json.loads(backup.read_text())["records"]) == 1
    assert main(["provider", "status"]) == 0


def test_journal_links_native_id_and_url_without_duplicate_task_links(setup, tmp_path, monkeypatch):
    from ekm.core import Engine

    config, _ = setup
    assert main(["auth", "google", "login", "--approve"]) == 0
    task_id = str(uuid4())
    source = tmp_path / "task.json"
    external_ref = "https://docs.google.com/document/d/external-design/edit"
    source.write_text(
        json.dumps(
            {"id": task_id, "kind": "task", "title": "Task journal", "references": [external_ref]}
        )
    )
    assert main(["log", "--input", str(source), "--approve"]) == 0
    result = {"id": "native-doc", "url": "https://docs.google.com/document/d/native-doc/edit"}
    rendered_references = []
    native = SimpleNamespace(
        journal=lambda task, *args, **kwargs: rendered_references.append(task.references) or result,
        dashboard=lambda *args, **kwargs: {
            "id": "native-sheet",
            "url": "https://docs.google.com/spreadsheets/d/native-sheet/edit",
        },
    )
    monkeypatch.setattr("ekm.google.documents.GoogleDocuments", lambda _: native)
    assert main(["google", "journal", task_id, "--approve"]) == 0
    assert main(["google", "journal", task_id, "--approve"]) == 0
    engine = Engine(config, config.get())
    refs = engine.get(Kind.task, UUID(task_id), remote=True).record.references
    engine.close()
    assert refs == [external_ref, "google-doc-id:native-doc", result["url"]]
    assert all(external_ref in refs for refs in rendered_references)
    assert main(["google", "dashboard", "--approve"]) == 0
    assert config.get().spreadsheet_id == "native-sheet"
    assert (
        config.get().spreadsheet_url == "https://docs.google.com/spreadsheets/d/native-sheet/edit"
    )
    assert main(["google", "dashboard"]) == 1


def test_google_profile_selection_never_guesses_between_accounts(setup):
    config, _ = setup
    assert main(["auth", "google", "login", "--approve"]) == 0
    config.add(
        Profile(name="second", account_type="google", client_id="other.apps.googleusercontent.com")
    )
    assert main(["provider", "switch", "google", "--approve"]) == 1
    assert main(["provider", "switch", "google", "--name", "second", "--approve"]) == 0
    assert config.active_name() == "second"
    assert main(["provider", "switch", "microsoft", "--approve"]) == 1


def test_missing_google_config_status_without_keychain(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EKM_HOME", str(tmp_path / "empty"))
    assert main(["auth", "google", "status"]) == 0
    assert not json.loads(capsys.readouterr().out)["authenticated"]
    assert main(["provider", "status"]) == 0


@pytest.mark.parametrize("pull_failed", [False, True])
def test_remote_merge_reports_retained_local_draft_and_pull_failure(
    setup, tmp_path, monkeypatch, capsys, pull_failed
):
    from ekm.core import Engine

    config, _ = setup
    assert main(["auth", "google", "login", "--approve"]) == 0
    task_id = str(uuid4())
    source = tmp_path / "task.json"
    source.write_text(json.dumps({"id": task_id, "kind": "task", "title": "Initial"}))
    assert main(["log", "--input", str(source), "--approve"]) == 0
    engine = Engine(config, config.get())
    stored = engine.get(Kind.task, UUID(task_id), remote=True)
    local = stored.record.model_copy(update={"title": "Local pending"}).sealed()
    engine.save(local, etag=stored.etag, offline=True, approved=True)
    engine.close()

    class ResolvedStorage(MockStorageProvider):
        def resolve_conflict(self, kind, record_id, heads, merged, *, approved):
            current = self.get_record(kind, record_id)
            return self.update_record(merged, current.etag, approved=approved)

    monkeypatch.setattr(
        "ekm.core.make_provider",
        lambda config, profile: ResolvedStorage(config.profile_dir(profile) / "remote"),
    )
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps({"id": task_id, "kind": "task", "title": "Merged remote"}))
    if pull_failed:
        monkeypatch.setattr(
            Engine, "pull", lambda _: {"downloaded": 0, "conflicts": [], "failed": [task_id]}
        )
    capsys.readouterr()
    code = main(
        [
            "google",
            "resolve",
            "task",
            task_id,
            "--head",
            "a",
            "--head",
            "b",
            "--input",
            str(merged),
            "--approve",
        ]
    )
    assert code == (1 if pull_failed else 0)
    output = capsys.readouterr()
    assert '"remote_resolved"' in output.out
    assert '"local_pending_retained"' in output.out
    assert "ekm sync resolve" in output.out
    engine = Engine(config, config.get())
    assert engine.pending()[0]["record"]["title"] == "Local pending"
    assert engine.get(Kind.task, UUID(task_id), remote=True).record.title == "Merged remote"
    engine.close()


def test_native_generation_stops_before_writes_when_pull_is_incomplete(setup, monkeypatch):
    from ekm.core import Engine

    assert main(["auth", "google", "login", "--approve"]) == 0
    monkeypatch.setattr(Engine, "pull", lambda _: {"conflicts": ["unresolved"], "failed": []})
    monkeypatch.setattr(
        "ekm.google.documents.GoogleDocuments",
        lambda _: pytest.fail("native write should not start"),
    )
    assert main(["google", "dashboard", "--approve"]) == 1
