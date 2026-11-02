#!/usr/bin/env python3
"""Install the canonical skill into both project-local agent directories."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

SKILL_NAME = "engineering-journal"
SOURCE = Path(__file__).resolve().parents[1] / "skill-source" / SKILL_NAME
AGENTS = (".claude", ".agents")


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


def install(target: Path, *, check: bool = False, force: bool = False) -> list[Path]:
    """Synchronize both adapters; drift needs --force and unknown files are preserved."""
    target = Path(os.path.abspath(target.expanduser()))
    desired = _files(SOURCE)
    if "SKILL.md" not in desired:
        raise ValueError(f"Canonical skill is missing: {SOURCE}")
    destinations = [target / agent / "skills" / SKILL_NAME for agent in AGENTS]
    existing = {}
    # Validate both destinations before changing either adapter.
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
        default=Path.cwd(),
        help="Project root to receive .claude and .agents skills",
    )
    parser.add_argument(
        "--check", action="store_true", help="Verify both installed copies without writing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace edited canonical files after inspecting the diff",
    )
    arguments = parser.parse_args()
    try:
        paths = install(arguments.target, check=arguments.check, force=arguments.force)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    for path in paths:
        print(f"{'Verified' if arguments.check else 'Installed'} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
