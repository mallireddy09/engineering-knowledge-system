#!/usr/bin/env python3
"""Install or package the canonical skill for supported coding agents."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

SKILL_NAME = "engineering-journal"
SOURCE = Path(__file__).resolve().parents[1] / "skill-source" / SKILL_NAME
AGENTS = {"claude": ".claude", "codex": ".agents", "cursor": ".cursor"}
PLUGIN_SKILL = SOURCE.parents[1] / "plugins" / "ekm" / "skills" / SKILL_NAME


def _files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink in skill tree: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _safe_directory(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"Refusing symlink destination: {parent}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"Destination is not a directory: {path}")


def install(
    target: Path, *, agent: str | None = None, check: bool = False, force: bool = False
) -> list[Path]:
    """Keep the original two-agent default; allow one agent or all three."""
    if agent is None:
        selected = ("claude", "codex")
    elif agent == "all":
        selected = tuple(AGENTS)
    else:
        selected = (agent,)
    if any(name not in AGENTS for name in selected):
        raise ValueError(f"Unknown agent: {agent}")
    target = Path(os.path.abspath(target.expanduser()))
    destinations = [target / AGENTS[name] / "skills" / SKILL_NAME for name in selected]
    return _sync(destinations, check=check, force=force)


def package(*, check: bool = False, force: bool = False) -> list[Path]:
    """Bundle only the canonical skill; credentials and CLI state stay outside it."""
    return _sync([PLUGIN_SKILL], check=check, force=force)


def _sync(destinations: list[Path], *, check: bool, force: bool) -> list[Path]:
    desired = _files(SOURCE)
    if "SKILL.md" not in desired:
        raise ValueError(f"Canonical skill is missing: {SOURCE}")
    existing = {}
    # Validate all destinations before changing any adapter.
    for destination in destinations:
        _safe_directory(destination)
        current = _files(destination)
        existing[destination] = current
        if check and current != desired:
            raise ValueError(f"Skill is missing or differs from canonical source: {destination}")
        extras = set(current) - set(desired)
        if extras:
            raise ValueError(
                f"Unmanaged files in {destination}; preserve them elsewhere before updating"
            )
        if current and current != desired and not force:
            raise ValueError(
                f"Local skill edits detected in {destination}; inspect them, then use --force to replace"
            )
    if check:
        return destinations
    for destination in destinations:
        destination.mkdir(parents=True, exist_ok=True)
        for relative, content in desired.items():
            if existing[destination].get(relative) == content:
                continue
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".ekm-", dir=output.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, output)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return destinations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        help="Project root to receive skills (defaults to the current directory)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--agent", choices=[*AGENTS, "all"], help="Install for one agent or all three"
    )
    mode.add_argument(
        "--package", action="store_true", help="Sync the repository's marketplace plugin skill"
    )
    parser.add_argument(
        "--check", action="store_true", help="Verify selected copies without writing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace edited canonical files after inspecting the diff",
    )
    arguments = parser.parse_args()
    if arguments.package and arguments.target is not None:
        parser.error("--package updates the repository bundle and cannot be combined with --target")
    try:
        paths = (
            package(check=arguments.check, force=arguments.force)
            if arguments.package
            else install(
                arguments.target or Path.cwd(),
                agent=arguments.agent,
                check=arguments.check,
                force=arguments.force,
            )
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    for path in paths:
        print(f"{'Verified' if arguments.check else 'Installed'} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
