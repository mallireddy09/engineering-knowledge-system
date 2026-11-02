import json
from pathlib import Path

import pytest

from ekm.cli import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("EKM_HOME", str(tmp_path / "state"))
    return tmp_path


def setup_mock():
    assert main(["account", "add", "demo", "--type", "mock"]) == 0
    assert main(["account", "switch", "demo", "--approve"]) == 0
    assert main(["init", "--approve"]) == 0


def input_file(home, data, name="record.json") -> Path:
    path = home / name
    path.write_text(json.dumps(data))
    return path


def test_account_switch_never_implicit(home, capsys):
    assert main(["account", "add", "demo", "--type", "mock"]) == 0
    assert main(["account", "status"]) == 1
    assert main(["account", "switch", "demo"]) == 1
    assert "approval required" in capsys.readouterr().err
    assert main(["account", "switch", "demo", "--approve"]) == 0


def test_log_dry_run_then_approved_save_search_export(home, capsys):
    setup_mock()
    source = input_file(
        home,
        {
            "title": "Kafka lag",
            "task_id": "ENG-1",
            "project_id": "platform",
            "implementation_summary": "Bounded retry protects Kafka consumer",
            "technologies": ["Kafka"],
        },
    )
    preview = home / "preview.json"
    assert main(["log", "--input", str(source), "--dry-run", "--preview-output", str(preview)]) == 0
    assert main(["search", "Kafka"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("[]")
    assert main(["log", "--input", str(preview), "--approve"]) == 0
    assert main(["search", "Kafka", "--project", "platform", "--technology", "Kafka"]) == 0
    assert "Matching terms" in capsys.readouterr().out
    destination = home / "export.json"
    assert main(["export", "--output", str(destination)]) == 0
    exported = json.loads(destination.read_text())
    assert len(exported["records"]) == 1
    assert exported["records"][0]["status"] == "unknown"
    assert main(["export", "--output", str(destination)]) == 1


def test_validation_errors_do_not_echo_secrets(home, capsys):
    setup_mock()
    source = input_file(home, {"title": "test", "extra": "sensitive plaintext"})
    assert main(["log", "--input", str(source), "--approve"]) == 1
    assert "sensitive plaintext" not in capsys.readouterr().err


def test_no_unapproved_write(home, capsys):
    setup_mock()
    source = input_file(home, {"title": "No save"})
    assert main(["log", "--input", str(source)]) == 1
    capsys.readouterr()
    assert main(["search", "save"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_named_profile_cannot_bypass_account_switch(home, capsys):
    setup_mock()
    assert main(["account", "add", "other", "--type", "mock"]) == 0
    source = input_file(home, {"title": "No crossing"})
    assert main(["--profile", "other", "log", "--input", str(source), "--approve"]) == 1
    assert "switch" in capsys.readouterr().err


def test_pending_offline_sync_and_restore(home, capsys):
    setup_mock()
    source = input_file(home, {"title": "Offline task", "task_id": "T-1"})
    assert main(["log", "--input", str(source), "--offline", "--approve"]) == 0
    assert main(["pending"]) == 0
    assert "Offline task" in capsys.readouterr().out
    assert main(["sync", "--approve"]) == 0
    backup = home / "backup.json"
    assert main(["backup", "--output", str(backup)]) == 0
    assert len(json.loads(backup.read_text())["records"]) == 1
    assert main(["restore", "--input", str(backup), "--approve"]) == 0
    destination = home / "after.json"
    assert main(["export", "--output", str(destination)]) == 0
    assert len(json.loads(destination.read_text())["records"]) == 1


def test_quiz_hides_answer_until_response(home, capsys):
    setup_mock()
    source = input_file(
        home,
        {
            "kind": "question",
            "title": "ETag",
            "question": "Why ETags?",
            "expected_answer": "Prevent lost updates",
            "topic": "reliability",
        },
    )
    assert main(["import", "--input", str(source), "--approve"]) == 0
    export = home / "question-export.json"
    assert main(["export", "--output", str(export)]) == 0
    record_id = json.loads(export.read_text())["records"][0]["id"]
    capsys.readouterr()
    assert main(["quiz", "--topic", "reliability"]) == 0
    output = capsys.readouterr().out
    assert "Why ETags?" in output and "Prevent lost updates" not in output
    assert main(["quiz", "--question", record_id]) == 0
    assert "Prevent lost updates" not in capsys.readouterr().out
    assert (
        main(
            [
                "review",
                "--question",
                record_id,
                "--answer",
                "stop overwrites",
                "--score",
                "0.8",
                "--feedback",
                "Correct mechanism; explain version mismatch",
                "--approve",
            ]
        )
        == 0
    )
    assert "Prevent lost updates" in capsys.readouterr().out
    assert main(["quiz"]) == 0
    states = json.loads(capsys.readouterr().out)
    assert states[0]["review_count"] == 1


def test_digest_binds_payload_destination_and_version(home, capsys):
    setup_mock()
    source = input_file(home, {"title": "Bound approval"})
    populated = home / "full.json"
    capsys.readouterr()
    assert (
        main(["log", "--input", str(source), "--dry-run", "--preview-output", str(populated)]) == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "log",
                "--input",
                str(populated),
                "--approve",
                "--expected-hash",
                preview["approval_hash"],
            ]
        )
        == 0
    )
    edited = json.loads(populated.read_text())
    edited["description"] = "changed after preview"
    populated.write_text(json.dumps(edited))
    assert (
        main(
            [
                "log",
                "--input",
                str(populated),
                "--approve",
                "--expected-hash",
                preview["approval_hash"],
            ]
        )
        == 1
    )
    assert "approval hash changed" in capsys.readouterr().err


def test_review_preview_and_feedback_never_echo_recognizable_secrets(home, capsys):
    setup_mock()
    source = input_file(
        home,
        {
            "kind": "question",
            "title": "Failure modes",
            "question": "Why bound retries?",
            "expected_answer": "Avoid storms",
        },
    )
    assert main(["import", "--input", str(source), "--approve"]) == 0
    export = home / "questions.json"
    assert main(["export", "--output", str(export)]) == 0
    record_id = json.loads(export.read_text())["records"][0]["id"]
    capsys.readouterr()
    assert (
        main(
            [
                "review",
                "--question",
                record_id,
                "--answer",
                'password="EXAMPLE SECRET VALUE"',
                "--score",
                "0.5",
                "--feedback",
                'password="EXAMPLE FEEDBACK SECRET"',
                "--approve",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert "FEEDBACK" not in output
    assert "[REDACTED]" in output


def test_sync_status_push_pull_and_corrupt_restore_bundle(home, capsys):
    from ekm.models import Record

    setup_mock()
    source = input_file(home, {"title": "Queued knowledge"})
    assert main(["log", "--input", str(source), "--offline", "--approve"]) == 0
    capsys.readouterr()
    assert main(["sync", "status"]) == 0
    assert len(json.loads(capsys.readouterr().out)["upload_queue"]) == 1
    assert main(["sync", "push", "--approve"]) == 0
    assert main(["sync", "pull"]) == 0
    bad = Record(kind="task", title="Original").sealed().model_dump(mode="json")
    bad["description"] = "Tampered backup"
    bundle = input_file(home, {"schema_version": 1, "records": [bad]}, "bad-backup.json")
    assert main(["restore", "--input", str(bundle), "--approve"]) == 1
    assert "hash" in capsys.readouterr().err
