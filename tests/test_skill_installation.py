import importlib.util
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "install_skills", REPOSITORY / "scripts" / "install_skills.py"
)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


@pytest.mark.parametrize("agent", [".claude", ".agents"])
def test_each_agent_gets_complete_canonical_skill(tmp_path, agent):
    INSTALLER.install(tmp_path)
    source = INSTALLER._files(INSTALLER.SOURCE)
    destination = tmp_path / agent / "skills" / "engineering-journal"
    assert INSTALLER._files(destination) == source
    assert INSTALLER.install(tmp_path, check=True)


@pytest.mark.parametrize(
    "agent,directory", [("claude", ".claude"), ("codex", ".agents"), ("cursor", ".cursor")]
)
def test_install_only_the_selected_agent(tmp_path, agent, directory):
    destinations = INSTALLER.install(tmp_path, agent=agent)
    assert destinations == [tmp_path / directory / "skills" / "engineering-journal"]
    assert INSTALLER._files(destinations[0]) == INSTALLER._files(INSTALLER.SOURCE)
    assert {p.name for p in tmp_path.iterdir()} == {directory}
    assert INSTALLER.install(tmp_path, agent=agent, check=True) == destinations


def test_all_agents_and_invalid_selection(tmp_path):
    with pytest.raises(ValueError, match="Unknown agent"):
        INSTALLER.install(tmp_path, agent="missing")
    assert not list(tmp_path.iterdir())
    destinations = INSTALLER.install(tmp_path, agent="all")
    assert len(destinations) == 3
    assert INSTALLER.install(tmp_path, agent="all", check=True) == destinations


def test_installation_is_idempotent_and_detects_local_edits(tmp_path):
    destinations = INSTALLER.install(tmp_path)
    first = destinations[0] / "SKILL.md"
    timestamp = first.stat().st_mtime_ns
    INSTALLER.install(tmp_path)
    assert first.stat().st_mtime_ns == timestamp
    first.write_text("local custom skill")
    with pytest.raises(ValueError, match="differs"):
        INSTALLER.install(tmp_path, check=True)
    with pytest.raises(ValueError, match="Local skill edits"):
        INSTALLER.install(tmp_path)
    assert first.read_text() == "local custom skill"
    INSTALLER.install(tmp_path, force=True)
    assert first.read_bytes() == (INSTALLER.SOURCE / "SKILL.md").read_bytes()


def test_check_missing_installation_does_not_write(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        INSTALLER.install(tmp_path, check=True)
    assert list(tmp_path.iterdir()) == []


def test_rejects_symlink_destination(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".claude").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        INSTALLER.install(tmp_path)
    assert list(outside.iterdir()) == []


def test_unmanaged_files_are_preserved_even_with_force(tmp_path):
    destinations = INSTALLER.install(tmp_path)
    extra = destinations[1] / "my-notes.txt"
    extra.write_text("keep")
    with pytest.raises(ValueError, match="Unmanaged"):
        INSTALLER.install(tmp_path, force=True)
    assert extra.read_text() == "keep"
