import subprocess

from ekm.integrations.git import git_context


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
