"""Prevent marketplace installs from shipping drifted skills or private state."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plugins" / "ekm"


def test_marketplaces_resolve_to_one_isolated_plugin():
    for catalog in (
        ".claude-plugin/marketplace.json",
        ".agents/plugins/marketplace.json",
        ".cursor-plugin/marketplace.json",
    ):
        market = json.loads((ROOT / catalog).read_text())
        assert market["name"] == "ekm"
        (entry,) = market["plugins"]
        assert entry["name"] == "ekm"
        relative = entry["source"]
        if isinstance(relative, dict):
            assert relative["source"] == "local"
            relative = relative["path"]
        assert (ROOT / relative).resolve() == PACKAGE


def test_each_plugin_surface_discovers_the_canonical_skill():
    versions = set()
    for agent in ("claude", "codex", "cursor"):
        manifest = json.loads((PACKAGE / f".{agent}-plugin/plugin.json").read_text())
        assert manifest["name"] == "ekm"
        versions.add(manifest["version"])
        skill = PACKAGE / manifest["skills"] / "engineering-journal"
        source = ROOT / "skill-source" / "engineering-journal"
        desired = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        actual = {p.relative_to(skill): p.read_bytes() for p in skill.rglob("*") if p.is_file()}
        assert actual == desired
    assert len(versions) == 1


def test_plugin_bundle_contains_no_credentials_executable_hooks_or_local_state():
    allowed = {
        "LICENSE",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".cursor-plugin/plugin.json",
        "skills/engineering-journal/SKILL.md",
        "skills/engineering-journal/references/records.md",
    }
    files = set()
    for path in PACKAGE.rglob("*"):
        assert not path.is_symlink()
        if path.is_file():
            files.add(path.relative_to(PACKAGE).as_posix())
    assert files == allowed
    assert (PACKAGE / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
