"""Read local metadata only. Never collect source/diffs or upload automatically."""

import json
import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode:
        raise ValueError("Git metadata unavailable; select a Git working tree")
    return result.stdout.rstrip("\n")


def git_context(root: Path) -> dict:
    branch = _git(root, "branch", "--show-current")
    status = _git(root, "status", "--porcelain=v1", "-z")
    entries = status.split("\0")
    files = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if entry:
            files.append(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                index += 1  # Porcelain -z places the original rename/copy path next.
        index += 1
    result = {
        "repository": root.resolve().name,
        "branch": branch,
        "files_changed": files,
        "commit_ids": [],
        "verified_facts": [],
    }
    try:
        commits = _git(root, "log", "-5", "--format=%H").splitlines()
        result["commit_ids"] = commits
        result["verified_facts"] = [
            f"Git branch: {branch}",
            f"Working tree has {len(result['files_changed'])} changed entries",
        ]
    except ValueError:
        pass  # A repository with no commits is still a valid logging source.
    return result


def github_metadata(repo: str, kind: str, number: int) -> dict:
    """Optional gh API reads with explicit repository; no code is transmitted."""
    import re

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or number < 1:
        raise ValueError("provide an owner/repository and positive issue or PR number")
    if kind not in {"issues", "pulls"}:
        raise ValueError("GitHub metadata kind must be issues or pulls")
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/{kind}/{number}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            "GitHub metadata unavailable; check gh authentication and repository access"
        )
    data = json.loads(result.stdout)
    return {
        key: data.get(key)
        for key in ("number", "title", "html_url", "state", "labels", "milestone")
    }
