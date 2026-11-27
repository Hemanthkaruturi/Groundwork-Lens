"""Compute section-level changes since a document's last approval."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from engine.groundwork_common import FrontMatterError, body_hash, split_front_matter
from engine.groundwork_git import git_file_commit_shas, git_show_file
from engine.groundwork_secrets import is_secret_line


def _split_sections(body: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current_name = "Introduction"
    current_lines: list[str] = []
    for line in (body or "").replace("\r\n", "\n").splitlines():
        m = re.match(r"^#{1,3}\s+(.*)$", line.strip())
        if m:
            if current_lines or sections:
                sections.append((current_name, current_lines))
            current_name = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)
    sections.append((current_name, current_lines))
    return sections


def find_approved_body_in_git(repo: Path, rel_path: str, approved_hash: str) -> str | None:
    """Locate the historical body matching approved_hash in git history."""
    for sha in git_file_commit_shas(repo, rel_path, limit=50):
        content = git_show_file(repo, sha, rel_path)
        if content is None:
            continue
        try:
            _, body = split_front_matter(content)
        except FrontMatterError:
            continue
        if body_hash(body) == approved_hash:
            return body
    return None


def compute_changes_since_approval(
    repo: Path,
    rel_path: str,
    approved_hash: str,
    current_body: str,
) -> list[dict[str, Any]]:
    """Return per-section added/removed lines compared to the approved revision."""
    if not approved_hash:
        return []
    old_body = find_approved_body_in_git(repo, rel_path, approved_hash)
    if old_body is None:
        return [
            {
                "section": "Document",
                "added": [],
                "removed": [],
                "added_count": 0,
                "removed_count": 0,
                "note": "Approved revision not found in local git history",
            }
        ]

    old_sections = _split_sections(old_body)
    new_sections = _split_sections(current_body)
    old_map = {name: lines for name, lines in old_sections}
    ordered_names = [name for name, _ in new_sections]
    for name, _ in old_sections:
        if name not in ordered_names:
            ordered_names.append(name)
    new_map = {name: lines for name, lines in new_sections}

    changes: list[dict[str, Any]] = []
    for name in ordered_names:
        a = old_map.get(name, [])
        b = new_map.get(name, [])
        if a == b:
            continue
        added: list[str] = []
        removed: list[str] = []
        for diff_line in difflib.ndiff(a, b):
            if diff_line.startswith("+ "):
                txt = diff_line[2:]
                if txt.strip() and is_secret_line(txt) is None:
                    added.append(txt)
            elif diff_line.startswith("- "):
                txt = diff_line[2:]
                if txt.strip() and is_secret_line(txt) is None:
                    removed.append(txt)
        if added or removed:
            changes.append(
                {
                    "section": name,
                    "added": added,
                    "removed": removed,
                    "added_count": len(added),
                    "removed_count": len(removed),
                }
            )
    return changes
