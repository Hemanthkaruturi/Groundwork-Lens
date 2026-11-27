"""GroundWork doctor: 6-step adoption ladder and unreadable file detection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.groundwork_common import (
    FrontMatterError,
    load_approvals,
    load_config,
    load_ledger,
    split_front_matter,
)

STAGES = [
    "not-started",
    "scaffolded",
    "documented",
    "current",
    "guarded",
    "practicing",
]


def _iter_groundwork_markdown(repo: Path) -> list[Path]:
    files: list[Path] = []
    for folder in ("DECISIONS", "specs", "bugs", "requests"):
        root = repo / folder
        if root.is_dir():
            for p in sorted(root.rglob("*.md")):
                if p.name.lower() == "readme.md":
                    continue
                files.append(p)
    return files


def scan_unreadable(repo: Path) -> list[dict[str, str]]:
    """Return list of GroundWork markdown files whose front matter is unreadable."""
    problems: list[dict[str, str]] = []
    for p in _iter_groundwork_markdown(repo):
        rel = p.relative_to(repo).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
            split_front_matter(text)
        except FrontMatterError as exc:
            problems.append({"path": rel, "problem": f"front matter is not valid: {exc}"})
        except OSError as exc:
            problems.append({"path": rel, "problem": f"could not read file: {exc}"})
    return problems


def run_doctor(repo: Path) -> dict[str, Any]:
    """Evaluate a repository against the 6-step GroundWork doctor ladder."""
    found: list[str] = []
    missing: list[str] = []
    next_steps: list[str] = []

    has_config = (repo / ".groundwork" / "config.json").is_file()
    has_gw_dir = (repo / ".groundwork").is_dir()
    has_project = (repo / "PROJECT.md").is_file()
    has_arch = (repo / "ARCHITECTURE.md").is_file()
    has_const = (repo / "CONSTITUTION.md").is_file()
    has_approvals = (repo / ".groundwork" / "approvals.json").is_file()
    has_ledger = (repo / ".groundwork" / "ledger.jsonl").is_file()

    checks = [
        (".groundwork/config.json", has_config, "Initialize GroundWork (.groundwork/config.json)"),
        ("PROJECT.md", has_project, "Write PROJECT.md with team roles"),
        ("ARCHITECTURE.md", has_arch, "Write ARCHITECTURE.md"),
        ("CONSTITUTION.md", has_const, "Write CONSTITUTION.md"),
    ]
    for label, ok, step in checks:
        if ok:
            found.append(label)
        else:
            missing.append(label)
            next_steps.append(step)

    md_files = _iter_groundwork_markdown(repo)
    if md_files:
        found.append("GroundWork documents")
    if has_approvals:
        found.append(".groundwork/approvals.json")
    if has_ledger:
        found.append(".groundwork/ledger.jsonl")

    unreadable = scan_unreadable(repo)

    has_any_gw = has_gw_dir or has_config or has_project or bool(md_files)
    if not has_any_gw:
        stage_num = 0
    else:
        cfg = load_config(repo)
        approvals = load_approvals(repo) if has_approvals else {}
        ledger = load_ledger(repo) if has_ledger else []

        has_evals = (repo / "specs").is_dir() and any((repo / "specs").rglob("evals.md"))
        has_handovers = (repo / "specs").is_dir() and any((repo / "specs").rglob("handover.md"))

        stage_num = 1  # scaffolded
        if has_arch and has_const:
            stage_num = 2  # documented
            if md_files:
                stage_num = 3  # current
                if approvals or has_evals:
                    stage_num = 4  # guarded
                    if ledger and not unreadable and (has_handovers or len(ledger) >= 2 or cfg):
                        stage_num = 5  # practicing

    return {
        "stage": STAGES[stage_num],
        "stage_number": stage_num,
        "found": found,
        "missing": missing,
        "next_steps": next_steps,
        "unreadable": unreadable,
    }
