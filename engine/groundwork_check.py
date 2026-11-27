"""GroundWork repository check rules (errors and warnings)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from engine.groundwork_common import (
    FrontMatterError,
    body_hash,
    load_approvals,
    parse_project_people,
    resolve_person,
    split_front_matter,
)

MANUAL_TASK_RE = re.compile(
    r"\b(?:T90\d|by hand|manual(?:ly)?|go-live|sign-?off|browser look|buy a plan|credentials)\b",
    re.IGNORECASE,
)


def parse_tasks_lines(tasks_text: str) -> list[dict[str, Any]]:
    """Parse tasks.md checklist items and optional Waiting on / Done when sub-lines."""
    tasks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw in (tasks_text or "").replace("\r\n", "\n").splitlines():
        m = re.match(r"^\s*-\s*\[([ xX~])\]\s*(.+)$", raw)
        if m:
            if current is not None:
                tasks.append(current)
            mark = m.group(1).lower()
            state = "done" if mark == "x" else ("in_progress" if mark == "~" else "todo")
            rest = m.group(2).strip()
            tid = ""
            title = rest
            tm = re.match(r"^\*\*(T\d+)\*\*\s*[—–:-]?\s*(.+)$", rest)
            if not tm:
                tm = re.match(r"^(T\d+)\s*[—–:-]?\s*(.+)$", rest)
            if tm:
                tid = tm.group(1)
                title = tm.group(2).strip()
            current = {
                "id": tid,
                "title": title,
                "state": state,
                "waiting_on": "",
                "waiting_note": "",
                "done_when": "",
            }
            continue

        if current is not None:
            wm = re.match(r"^\s*-\s*\*\*Waiting on:\*\*\s*(.+)$", raw, re.IGNORECASE)
            if wm:
                wraw = wm.group(1).strip()
                pm = re.match(r"^([^()]+?)(?:\s*\(([^)]+)\))?$", wraw)
                if pm:
                    current["waiting_on"] = pm.group(1).strip()
                    current["waiting_note"] = (pm.group(2) or "").strip()
                else:
                    current["waiting_on"] = wraw
                continue
            dm = re.match(r"^\s*-\s*\*\*Done when:\*\*\s*(.+)$", raw, re.IGNORECASE)
            if dm:
                current["done_when"] = dm.group(1).strip()
                continue

    if current is not None:
        tasks.append(current)
    return tasks


def is_legacy_spec(meta: dict[str, Any]) -> bool:
    """Return True for baseline/pre-GroundWork specs that do not cite an active RFC."""
    if meta.get("legacy") is True or meta.get("baseline") is True:
        return True
    rfc_val = str(meta.get("rfc") or "").strip().lower()
    if rfc_val in ("", "(none)", "none", "legacy"):
        return True
    return False


def run_check(repo: Path) -> dict[str, Any]:
    """Run all GroundWork validation rules and return errors, warnings, and stale count."""
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    people = parse_project_people(repo)
    known_names = {p["name"].lower() for p in people}
    approvals = load_approvals(repo)
    stale_count = 0

    def _is_known_person(name: str) -> bool:
        if not name:
            return True
        resolved = resolve_person(name, people)
        return resolved.lower() in known_names

    # 1. Check RFCs exist and validate front matter
    rfc_ids: set[str] = set()
    dec_dir = repo / "DECISIONS"
    if dec_dir.is_dir():
        for p in sorted(dec_dir.glob("RFC-*.md")):
            rel = p.relative_to(repo).as_posix()
            try:
                meta, body = split_front_matter(p.read_text(encoding="utf-8"))
            except FrontMatterError as exc:
                errors.append({"rule": "invalid-front-matter", "path": rel, "message": str(exc)})
                continue
            rfc_id = str(meta.get("id") or "").strip()
            if not rfc_id:
                m = re.match(r"^(RFC-\d+)", p.name)
                rfc_id = m.group(1) if m else p.stem
            rfc_ids.add(rfc_id)
            for app_name in meta.get("approvers") or []:
                if not _is_known_person(str(app_name)):
                    warnings.append(
                        {
                            "rule": "approver-unknown-person",
                            "path": rel,
                            "message": f"Named approver {app_name!r} is not listed in PROJECT.md",
                        }
                    )
            rec = approvals.get(rel)
            if isinstance(rec, dict) and rec.get("hash") and rec.get("hash") != body_hash(body):
                stale_count += 1

    # 2. Check specs (RFC citations, tasks Waiting on:, stale approvals, approvers)
    specs_dir = repo / "specs"
    if specs_dir.is_dir():
        for spec_folder in sorted(specs_dir.iterdir()):
            if not spec_folder.is_dir():
                continue
            spec_md = spec_folder / "spec.md"
            if spec_md.is_file():
                rel = spec_md.relative_to(repo).as_posix()
                try:
                    meta, body = split_front_matter(spec_md.read_text(encoding="utf-8"))
                    if not is_legacy_spec(meta):
                        rfc_ref = str(meta.get("rfc") or "").strip()
                        if rfc_ref and rfc_ref not in rfc_ids:
                            errors.append(
                                {
                                    "rule": "spec-missing-rfc",
                                    "path": rel,
                                    "message": f"Spec cites {rfc_ref!r} which does not exist",
                                }
                            )
                    for app_name in meta.get("approvers") or []:
                        if not _is_known_person(str(app_name)):
                            warnings.append(
                                {
                                    "rule": "approver-unknown-person",
                                    "path": rel,
                                    "message": f"Named approver {app_name!r} is not listed in PROJECT.md",
                                }
                            )
                    rec = approvals.get(rel)
                    if isinstance(rec, dict) and rec.get("hash") and rec.get("hash") != body_hash(body):
                        stale_count += 1
                except FrontMatterError as exc:
                    errors.append({"rule": "invalid-front-matter", "path": rel, "message": str(exc)})

            for sub_name in ("plan.md", "tasks.md", "evals.md"):
                sub_p = spec_folder / sub_name
                if not sub_p.is_file():
                    continue
                sub_rel = sub_p.relative_to(repo).as_posix()
                try:
                    _, sub_body = split_front_matter(sub_p.read_text(encoding="utf-8"))
                    rec = approvals.get(sub_rel)
                    if isinstance(rec, dict) and rec.get("hash") and rec.get("hash") != body_hash(sub_body):
                        stale_count += 1
                except FrontMatterError as exc:
                    errors.append({"rule": "invalid-front-matter", "path": sub_rel, "message": str(exc)})
                    continue

                if sub_name == "tasks.md":
                    for t in parse_tasks_lines(sub_body):
                        if t["state"] == "done":
                            continue
                        w_person = t.get("waiting_on", "")
                        if w_person:
                            if not _is_known_person(w_person):
                                warnings.append(
                                    {
                                        "rule": "waiting-on-unknown-person",
                                        "path": sub_rel,
                                        "message": f"Task {t['id'] or t['title']} Waiting on {w_person!r} not in PROJECT.md",
                                    }
                                )
                        else:
                            combined = f"{t.get('id', '')} {t.get('title', '')}"
                            if MANUAL_TASK_RE.search(combined):
                                warnings.append(
                                    {
                                        "rule": "manual-task-missing-waiting-on",
                                        "path": sub_rel,
                                        "message": f"Task {t['id'] or t['title']} looks manual but has no Waiting on: marker",
                                    }
                                )

    # 3. Check bugs
    bugs_dir = repo / "bugs"
    if bugs_dir.is_dir():
        for bp in sorted(bugs_dir.glob("*.md")):
            if bp.name.lower() == "readme.md":
                continue
            rel = bp.relative_to(repo).as_posix()
            try:
                split_front_matter(bp.read_text(encoding="utf-8"))
            except FrontMatterError as exc:
                errors.append({"rule": "invalid-front-matter", "path": rel, "message": str(exc)})

    # 4. Check requests converted links and backlinks
    req_dir = repo / "requests"
    if req_dir.is_dir():
        for rp in sorted(req_dir.glob("REQ-*.md")):
            rel = rp.relative_to(repo).as_posix()
            try:
                rmeta, _ = split_front_matter(rp.read_text(encoding="utf-8"))
            except FrontMatterError as exc:
                errors.append({"rule": "invalid-front-matter", "path": rel, "message": str(exc)})
                continue

            req_id = str(rmeta.get("id") or rp.stem.split("-")[0]).strip()
            if str(rmeta.get("status") or "").lower() == "converted":
                target = str(rmeta.get("converted_to") or "").strip()
                if not target:
                    errors.append(
                        {
                            "rule": "request-converted-target-missing",
                            "path": rel,
                            "message": f"Converted request {req_id} has empty converted_to",
                        }
                    )
                    continue
                target_file = _resolve_converted_target(repo, target)
                if target_file is None:
                    errors.append(
                        {
                            "rule": "request-converted-target-missing",
                            "path": rel,
                            "message": f"Converted request {req_id} points to {target!r} which does not exist",
                        }
                    )
                else:
                    try:
                        tmeta, _ = split_front_matter(target_file.read_text(encoding="utf-8"))
                        if str(tmeta.get("from_request") or "").strip() != req_id:
                            errors.append(
                                {
                                    "rule": "request-backlink-missing",
                                    "path": target_file.relative_to(repo).as_posix(),
                                    "message": f"Target {target!r} does not set from_request: {req_id}",
                                }
                            )
                    except FrontMatterError as exc:
                        errors.append(
                            {
                                "rule": "invalid-front-matter",
                                "path": target_file.relative_to(repo).as_posix(),
                                "message": str(exc),
                            }
                        )

    return {
        "errors": errors,
        "warnings": warnings,
        "check_errors": len(errors),
        "check_warnings": len(warnings),
        "stale_documents": stale_count,
    }


def _resolve_converted_target(repo: Path, target: str) -> Path | None:
    p = repo / target
    if p.is_file():
        return p
    if not target.endswith(".md"):
        p_md = repo / f"{target}.md"
        if p_md.is_file():
            return p_md
    if target.upper().startswith("RFC-"):
        matches = sorted((repo / "DECISIONS").glob(f"{target}*.md")) if (repo / "DECISIONS").is_dir() else []
        if matches:
            return matches[0]
    if target.startswith("bugs/"):
        slug = target[len("bugs/") :]
        matches = sorted((repo / "bugs").glob(f"{slug}*.md")) if (repo / "bugs").is_dir() else []
        if matches:
            return matches[0]
    return None
