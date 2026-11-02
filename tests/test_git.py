import subprocess

import pytest

from ekm.cli import main
from ekm.integrations.git import git_context


@pytest.mark.parametrize("command", [["github", "owner/repo", "issues", "1"], ["log"]])
def test_subprocess_timeout_returns_safe_cli_error(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setenv("EKM_HOME", str(tmp_path / "home"))
    assert main(["account", "add", "demo", "--type", "mock"]) == 0
    assert main(["account", "switch", "demo", "--approve"]) == 0

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10, output="sensitive subprocess output")

    monkeypatch.setattr(subprocess, "run", timeout)
    assert main(command) == 1
    error = capsys.readouterr().err
    assert "timed out" in error
    assert "sensitive subprocess output" not in error


def test_metadata_preserves_modified_and_renamed_paths(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    original = tmp_path / "path with spaces.py"
    original.write_text("original\n")
    git("add", original.name)
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed")
    original.write_text("changed\n")
    context = git_context(tmp_path)
    assert context["files_changed"] == [original.name]
    git("mv", original.name, "renamed.py")
    context = git_context(tmp_path)
    assert context["files_changed"] == ["renamed.py"]
    assert len(context["commit_ids"]) == 1


def test_empty_repository_context(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    context = git_context(tmp_path)
    assert context["commit_ids"] == []
    assert context["files_changed"] == []
