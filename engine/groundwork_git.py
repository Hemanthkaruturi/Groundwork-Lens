"""Single-pass git history scanner for GroundWork dates, commits, and historical content."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


def _run_git(repo: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def get_head_commit(repo: Path) -> tuple[str, str]:
    """Return (short_sha, full_sha) for HEAD, or ('', '') if repo has no commits."""
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        return "", ""
    res = _run_git(repo, ["rev-parse", "--short", "HEAD"])
    if res.returncode != 0:
        return "", ""
    short = res.stdout.strip()
    full_res = _run_git(repo, ["rev-parse", "HEAD"])
    full = full_res.stdout.strip() if full_res.returncode == 0 else short
    return short, full


def scan_git_history(repo: Path, max_commits: int = 1000) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Scan git log in a single pass.

    Returns:
        file_dates: mapping from repo-relative POSIX path -> last commit ISO date.
        commits: list of commit dicts {hash, at, author, email, subject, files}.
    """
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        return {}, []
    fmt = "%h%x1f%aI%x1f%an%x1f%ae%x1f%s"
    res = _run_git(repo, ["log", f"-n{max_commits}", f"--format={fmt}", "--name-only"])
    if res.returncode != 0:
        return {}, []

    file_dates: dict[str, str] = {}
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in res.stdout.splitlines():
        if "\x1f" in line:
            if current is not None:
                commits.append(current)
            parts = line.split("\x1f", 4)
            if len(parts) == 5:
                short_h, at_iso, author, email, subject = parts
                current = {
                    "hash": short_h.strip(),
                    "at": at_iso.strip(),
                    "author": author.strip(),
                    "email": email.strip(),
                    "subject": subject.strip(),
                    "files": [],
                }
            else:
                current = None
        else:
            path_line = line.strip()
            if path_line and current is not None:
                current["files"].append(path_line)
                if path_line not in file_dates:
                    file_dates[path_line] = current["at"]

    if current is not None:
        commits.append(current)

    return file_dates, commits


def last_commit_date_for_paths(
    file_dates: dict[str, str],
    paths: list[str],
    prefixes: list[str] | None = None,
) -> str:
    """Return the newest git commit date across exact paths or path prefixes."""
    candidates: list[str] = []
    for p in paths:
        if p in file_dates:
            candidates.append(file_dates[p])
    for pref in prefixes or []:
        for fpath, dt in file_dates.items():
            if fpath == pref or fpath.startswith(pref.rstrip("/") + "/"):
                candidates.append(dt)
    if not candidates:
        return ""
    return max(candidates)


def link_commits(
    commits: list[dict[str, Any]],
    path_prefixes: list[str],
    message_tokens: list[str],
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Link commits touching item files or naming item identifiers in subject."""
    patterns = [re.compile(rf"\b{re.escape(tok)}\b", re.IGNORECASE) for tok in message_tokens if tok]
    linked: list[dict[str, Any]] = []
    for c in commits:
        matched = False
        for fpath in c.get("files", []):
            for pref in path_prefixes:
                norm_pref = pref.rstrip("/")
                if fpath == norm_pref or fpath.startswith(norm_pref + "/"):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            subj = c.get("subject", "")
            for pat in patterns:
                if pat.search(subj):
                    matched = True
                    break
        if matched:
            linked.append(
                {
                    "hash": c["hash"],
                    "at": c["at"],
                    "author": c["author"],
                    "subject": c["subject"],
                }
            )
            if len(linked) >= limit:
                break
    return linked


def git_show_file(repo: Path, commit_sha: str, rel_path: str) -> str | None:
    """Read file text at a specific git commit."""
    res = _run_git(repo, ["show", f"{commit_sha}:{rel_path}"])
    if res.returncode != 0:
        return None
    return res.stdout


def git_file_commit_shas(repo: Path, rel_path: str, limit: int = 50) -> list[str]:
    """Return commit SHAs touching rel_path, newest first."""
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        return []
    res = _run_git(repo, ["log", f"-n{limit}", "--format=%H", "--", rel_path])
    if res.returncode != 0:
        return []
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
