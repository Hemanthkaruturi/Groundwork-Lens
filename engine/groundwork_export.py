"""GroundWork JSON snapshot exporter (schema_version 1.0)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from engine import ENGINE_VERSION, SCHEMA_VERSION
from engine.groundwork_approve import evaluate_document
from engine.groundwork_check import is_legacy_spec, MANUAL_TASK_RE, parse_tasks_lines, run_check
from engine.groundwork_common import (
    FrontMatterError,
    extract_section,
    load_approvals,
    load_config,
    load_ledger,
    parse_project_people,
    resolve_person,
    split_front_matter,
    utc_now_iso,
)
from engine.groundwork_doctor import run_doctor
from engine.groundwork_git import get_head_commit, last_commit_date_for_paths, link_commits, scan_git_history
from engine.groundwork_requests import parse_request_questions
from engine.groundwork_secrets import redact_lines

EXPORT_PATHS = [
    "DECISIONS/",
    "specs/",
    "bugs/",
    "requests/",
    ".groundwork/",
    "PROJECT.md",
    "ARCHITECTURE.md",
    "AGENTS.md",
    "CONSTITUTION.md",
    "CONTRACTS/",
    ".gitmodules",
]


def _parse_log_notes(log_text: str) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    for raw in (log_text or "").replace("\r\n", "\n").splitlines():
        s = raw.strip()
        if not s:
            continue
        m = re.match(r"^(?:[-*]\s+|#{2,3}\s+)?(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?)\s*[—–:-]?\s*(.+)$", s)
        if m:
            at = m.group(1).strip()
            if len(at) == 10:
                at = f"{at}T00:00:00Z"
            elif " " in at and "T" not in at:
                at = at.replace(" ", "T")
                if not at.endswith("Z") and "+" not in at[10:]:
                    at += "Z"
            txt = redact_lines(m.group(2).strip())
            if txt:
                notes.append({"at": at, "text": txt})
    return notes


def _parse_changes_md(changes_text: str, people: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in (changes_text or "").replace("\r\n", "\n").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("|---"):
            continue
        # Match bullet format: - 2026-11-07 by Hemanth (via claude-code): summary
        bm = re.match(
            r"^[-*]\s+(\d{4}-\d{2}-\d{2}(?:T[0-9:+-]+Z?)?)\s+(?:by\s+([^(:]+?))?(?:\s*\(via\s+([^)]+)\))?\s*[:—–-]\s*(.+)$",
            s,
            re.IGNORECASE,
        )
        if bm:
            at = bm.group(1).strip()
            if len(at) == 10:
                at += "T00:00:00Z"
            who = resolve_person((bm.group(2) or "Unknown").strip(), people)
            via = (bm.group(3) or "").strip()
            summary = redact_lines(bm.group(4).strip())
            row: dict[str, str] = {"at": at, "who": who, "summary": summary}
            if via:
                row["via"] = via
            rows.append(row)
            continue
        # Simple dated bullet: - 2026-11-07: summary (via claude-code)
        sm = re.match(r"^[-*]\s+(\d{4}-\d{2}-\d{2})\s*[:—–-]\s*(.+)$", s)
        if sm:
            txt = sm.group(2).strip()
            via = ""
            vm = re.search(r"\(via\s+([^)]+)\)\s*$", txt, re.IGNORECASE)
            if vm:
                via = vm.group(1).strip()
                txt = txt[: vm.start()].strip()
            row = {"at": f"{sm.group(1)}T00:00:00Z", "who": "Owner", "summary": redact_lines(txt)}
            if via:
                row["via"] = via
            rows.append(row)
    return rows


def _extract_interview_rows(body: str) -> list[str]:
    sec = extract_section(body, r"interview")
    if not sec:
        return []
    rows = []
    for line in sec.splitlines():
        s = line.strip()
        if s.startswith("|") and not re.match(r"^\|\s*-+", s) and not re.match(r"^\|\s*question\b", s, re.IGNORECASE):
            rows.append(s)
        elif re.match(r"^[-*]\s+", s):
            rows.append(s)
    return rows


def _parse_request_fields(kind: str, body: str) -> dict[str, str]:
    if kind == "feature":
        return {
            "need": redact_lines(extract_section(body, r"what is needed")),
            "done_looks_like": redact_lines(extract_section(body, r'what "done" would look like')),
            "why_now": redact_lines(extract_section(body, r"why now")),
            "product_area": redact_lines(extract_section(body, r"product area")),
        }
    return {
        "expected": redact_lines(extract_section(body, r"what i expected")),
        "actual": redact_lines(extract_section(body, r"what happened instead")),
        "where_when": redact_lines(extract_section(body, r"where and when")),
        "severity": redact_lines(extract_section(body, r"severity")),
        "links": redact_lines(extract_section(body, r"links")),
        "product_area": redact_lines(extract_section(body, r"product area")),
    }


def _enrich_people_aliases(
    repo: Path,
    people: list[dict[str, Any]],
    approvals: dict[str, Any],
    ledger: list[dict[str, Any]],
    commits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enrich people[].aliases with emails and git logins observed in records."""
    by_name = {p["name"].lower(): p for p in people}

    def _add_alias_to_match(candidate: str, extra: str = "") -> None:
        c = (candidate or "").strip()
        if not c:
            return
        low = c.lower()
        for p in people:
            aliases_low = [a.lower() for a in p["aliases"]]
            if p["name"].lower() == low or low in aliases_low:
                if extra and extra not in p["aliases"]:
                    p["aliases"].append(extra)
                return
        # Heuristic: match git author email local part or name without spaces
        compact = re.sub(r"\s+", "", low)
        for p in people:
            p_compact = re.sub(r"\s+", "", p["name"].lower())
            if compact == p_compact:
                if c not in p["aliases"]:
                    p["aliases"].append(c)
                if extra and extra not in p["aliases"]:
                    p["aliases"].append(extra)
                return

    for rec in approvals.values():
        if isinstance(rec, dict):
            for s in rec.get("signers", []):
                if isinstance(s, dict) and s.get("who"):
                    _add_alias_to_match(str(s["who"]))
    for ev in ledger:
        if ev.get("who"):
            _add_alias_to_match(str(ev["who"]))
    for c in commits:
        author = c.get("author", "")
        email = c.get("email", "")
        if author:
            _add_alias_to_match(author, email)
        if email:
            _add_alias_to_match(email, author)

    return list(by_name.values())


def _plain_state_for_docs_stale(docs: list[dict[str, Any]]) -> bool:
    return any(d.get("approval") == "stale" for d in docs)


def _format_names(names: list[str]) -> str:
    clean = [n for n in names if n]
    if not clean:
        return "reviewers"
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return ", ".join(clean[:-1]) + f", and {clean[-1]}"


def _export_single_repo(
    repo: Path,
    repo_name: str,
    source_ref: str = "main",
    workspace_repo: Path | None = None,
    id_prefix: str = "",
) -> dict[str, Any]:
    """Export one repository's GroundWork state."""
    cfg = load_config(repo)
    if not repo_name:
        repo_name = str(cfg.get("name") or repo.name)

    short_sha, _ = get_head_commit(repo)
    file_dates, commits = scan_git_history(repo)
    people = parse_project_people(repo)
    approvals = load_approvals(repo)
    ledger = load_ledger(repo)
    people = _enrich_people_aliases(repo, people, approvals, ledger, commits)

    doctor_block = run_doctor(repo)
    check_res = run_check(repo)

    items: list[dict[str, Any]] = []
    approvals_waiting: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for ev in ledger:
        ev_copy = {
            "at": str(ev.get("at") or ""),
            "type": str(ev.get("type") or "event"),
            "item": str(ev.get("item") or ""),
            "who": resolve_person(str(ev.get("who") or ""), people),
            "source": "ledger",
        }
        if ev.get("via"):
            ev_copy["via"] = str(ev["via"])
        events.append(ev_copy)

    # Index RFCs
    rfc_by_id: dict[str, dict[str, Any]] = {}
    dec_dir = repo / "DECISIONS"
    if dec_dir.is_dir():
        for rfc_path in sorted(dec_dir.glob("RFC-*.md")):
            rel = rfc_path.relative_to(repo).as_posix()
            try:
                meta, body = split_front_matter(rfc_path.read_text(encoding="utf-8"))
            except FrontMatterError:
                continue
            rfc_id = str(meta.get("id") or "").strip()
            if not rfc_id:
                m = re.match(r"^(RFC-\d+)", rfc_path.name)
                rfc_id = m.group(1) if m else rfc_path.stem
            doc_eval = evaluate_document(
                repo, rel, approvals_data=approvals, people=people, workspace_repo=workspace_repo
            )
            doc_summary = {k: v for k, v in doc_eval.items() if k != "meta"}
            summary_txt = redact_lines(extract_section(body, r"^(?:1\.?\s*)?summary\b"))
            need_txt = redact_lines(
                extract_section(body, r"^(?:3\.?\s*)?done means\b")
                or extract_section(body, r"^(?:2\.?\s*)?the need\b")
            )
            rfc_by_id[rfc_id] = {
                "id": rfc_id,
                "path": rel,
                "meta": meta,
                "body": body,
                "doc": doc_summary,
                "summary": summary_txt,
                "done_means": need_txt,
                "interview_rows": _extract_interview_rows(body),
                "last_activity": last_commit_date_for_paths(file_dates, [rel]),
                "commits": link_commits(commits, [rel], [rfc_id]),
                "claimed_by_spec": False,
            }

    # Export Features (specs/NNN-slug/)
    specs_dir = repo / "specs"
    if specs_dir.is_dir():
        for spec_folder in sorted(specs_dir.iterdir()):
            if not spec_folder.is_dir():
                continue
            spec_md = spec_folder / "spec.md"
            if not spec_md.is_file():
                continue
            spec_rel = spec_md.relative_to(repo).as_posix()
            try:
                spec_meta, spec_body = split_front_matter(spec_md.read_text(encoding="utf-8"))
            except FrontMatterError:
                continue

            slug = spec_folder.name
            item_id = f"{id_prefix}{slug}" if id_prefix else slug
            title = str(spec_meta.get("title") or re.sub(r"^\d+-", "", slug).replace("-", " ").title())
            rfc_ref = str(spec_meta.get("rfc") or "").strip()
            if rfc_ref.lower() in ("(none)", "none"):
                rfc_ref = ""
            legacy = is_legacy_spec(spec_meta)

            rfc_info = rfc_by_id.get(rfc_ref)
            if rfc_info is not None:
                rfc_info["claimed_by_spec"] = True

            docs: list[dict[str, Any]] = []
            if rfc_info is not None:
                docs.append(rfc_info["doc"])
            for doc_name in ("spec.md", "plan.md", "tasks.md", "evals.md", "handover.md"):
                dp = spec_folder / doc_name
                if dp.is_file():
                    drel = dp.relative_to(repo).as_posix()
                    try:
                        deval = evaluate_document(
                            repo, drel, approvals_data=approvals, people=people, workspace_repo=workspace_repo
                        )
                        docs.append({k: v for k, v in deval.items() if k != "meta"})
                    except FrontMatterError:
                        continue

            # Tasks and waiting_on
            tasks_list: list[dict[str, Any]] = []
            done_count = 0
            total_count = 0
            current_task = ""
            waiting_on: list[dict[str, Any]] = []
            owner = resolve_person(str(spec_meta.get("owner") or ""), people)
            requested_by = resolve_person(
                str(spec_meta.get("requested_by") or (rfc_info["meta"].get("requested_by") if rfc_info else "") or owner),
                people,
            )

            tasks_path = spec_folder / "tasks.md"
            if tasks_path.is_file():
                try:
                    _, tasks_body = split_front_matter(tasks_path.read_text(encoding="utf-8"))
                except FrontMatterError:
                    tasks_body = ""
                tasks_list = parse_tasks_lines(tasks_body)
                total_count = len(tasks_list)
                done_count = sum(1 for t in tasks_list if t["state"] == "done")
                for t in tasks_list:
                    if t["state"] in ("in_progress", "todo") and not current_task:
                        current_task = f"{t['id']} {t['title']}".strip()
                    if t["state"] != "done":
                        if t["waiting_on"]:
                            waiting_on.append(
                                {
                                    "person": resolve_person(t["waiting_on"], people),
                                    "task": t["id"],
                                    "what": t["waiting_note"] or t["title"],
                                    "inferred": False,
                                }
                            )
                        elif MANUAL_TASK_RE.search(f"{t['id']} {t['title']}"):
                            waiting_on.append(
                                {
                                    "person": owner or "Owner",
                                    "task": t["id"],
                                    "what": t["title"],
                                    "inferred": True,
                                }
                            )

            # Log notes
            notes: list[dict[str, str]] = []
            log_path = spec_folder / "log.md"
            if log_path.is_file():
                notes = _parse_log_notes(log_path.read_text(encoding="utf-8", errors="replace"))
            last_note = notes[-1] if notes else None
            for n in notes:
                events.append(
                    {
                        "at": n["at"],
                        "type": "note",
                        "item": item_id,
                        "who": owner or "",
                        "source": "note",
                        "detail": n["text"],
                    }
                )

            # Change logs
            changes: list[dict[str, str]] = []
            ch_path = spec_folder / "changes.md"
            if ch_path.is_file():
                changes = _parse_changes_md(ch_path.read_text(encoding="utf-8", errors="replace"), people)
            for ch in changes:
                ev_ch: dict[str, Any] = {
                    "at": ch["at"],
                    "type": "change",
                    "item": item_id,
                    "who": ch["who"],
                    "source": "change-log",
                    "detail": ch["summary"],
                }
                if ch.get("via"):
                    ev_ch["via"] = ch["via"]
                events.append(ev_ch)

            # Relations / blocked_by
            blocked_by: list[str] = []
            for rel_key in ("depends_on", "blocked_by"):
                val = spec_meta.get(rel_key)
                if isinstance(val, list):
                    blocked_by.extend(str(x) for x in val if str(x).strip())
                elif isinstance(val, str) and val.strip():
                    blocked_by.append(val.strip())

            # Stage and plain state
            has_handover = (spec_folder / "handover.md").is_file()
            handover_done = False
            if has_handover:
                try:
                    hm, _ = split_front_matter((spec_folder / "handover.md").read_text(encoding="utf-8"))
                    handover_done = str(hm.get("status") or "").lower() in ("done", "complete", "completed", "approved") or bool(hm)
                except FrontMatterError:
                    handover_done = True

            if str(spec_meta.get("status") or "").lower() == "done" or handover_done:
                stage = "done"
                state = "handover done"
                state_plain = "Done"
            elif total_count > 0 and done_count == total_count and not has_handover:
                stage = "handover"
                state = "tasks all done, handover missing"
                state_plain = "Built, being handed over"
            elif total_count > 0 and (done_count > 0 or any(t["state"] == "in_progress" for t in tasks_list)):
                stage = "build"
                state = f"building — tasks {done_count}/{total_count}"
                state_plain = f"Being built: {done_count} of {total_count} steps done"
            elif (spec_folder / "evals.md").is_file():
                stage = "evals"
                state = "evals — in progress"
                state_plain = "Being planned (evals)"
            elif tasks_path.is_file():
                stage = "tasks"
                state = "tasks — in progress"
                state_plain = "Being planned (tasks)"
            elif (spec_folder / "plan.md").is_file():
                stage = "plan"
                state = "plan — in progress"
                state_plain = "Being planned (plan)"
            else:
                stage = "spec"
                state = "spec — draft"
                state_plain = "Being planned (spec)"

            if _plain_state_for_docs_stale(docs):
                state_plain = "Changed after approval, needs re-approval"
            elif blocked_by:
                state_plain = f"Waiting for {', '.join(blocked_by)} first"

            if legacy and str(spec_meta.get("status") or "").lower() in ("draft", "in-progress", ""):
                # N4: older project being documented
                if not any(d.get("approval") == "approved" for d in docs):
                    state_plain = "Being documented"

            # Approval waiting entries
            for d in docs:
                if d["approval"] in ("draft", "in-review", "stale"):
                    needs = d["still_needed"] if isinstance(d["still_needed"], list) else [f"{d['still_needed']} sign-off(s)"]
                    if not needs and owner:
                        needs = [owner]
                    approvals_waiting.append(
                        {
                            "document": f"{repo_name}/{d['path']}" if id_prefix else d["path"],
                            "state": d["approval"],
                            "needs": needs,
                        }
                    )
                for sig in d.get("signed", []):
                    if sig.get("when"):
                        ev_ap: dict[str, Any] = {
                            "at": sig["when"],
                            "type": "approved",
                            "item": d["path"],
                            "who": resolve_person(sig["who"], people),
                            "source": "approval",
                        }
                        if sig.get("via"):
                            ev_ap["via"] = sig["via"]
                        events.append(ev_ap)

            # Summary & Done means
            summary_txt = redact_lines(extract_section(spec_body, r"^(?:1\.?\s*)?summary\b"))
            if not summary_txt and rfc_info:
                summary_txt = rfc_info["summary"]
            done_means_txt = redact_lines(extract_section(spec_body, r"done means"))
            if not done_means_txt and rfc_info:
                done_means_txt = rfc_info["done_means"]

            spec_num_m = re.match(r"^(\d+)-", slug)
            tokens = [slug]
            if spec_num_m:
                tokens.append(f"spec {spec_num_m.group(1)}")
            if rfc_ref:
                tokens.append(rfc_ref)
            folder_prefix = spec_folder.relative_to(repo).as_posix()
            linked_commits = link_commits(commits, [folder_prefix], tokens)
            for c in linked_commits:
                events.append(
                    {
                        "at": c["at"],
                        "type": "commit",
                        "item": item_id,
                        "who": resolve_person(c["author"], people),
                        "source": "git",
                        "detail": c["subject"],
                        "hash": c["hash"],
                    }
                )

            last_act = last_commit_date_for_paths(file_dates, [], [folder_prefix])
            if not last_act and last_note:
                last_act = last_note["at"]

            # Shipped date (handover or deployed ledger event)
            shipped_at = ""
            if stage == "done":
                shipped_at = last_commit_date_for_paths(file_dates, [f"{folder_prefix}/handover.md"]) or last_act
            for ev in ledger:
                if ev.get("type") == "deployed" and ev.get("item") in (item_id, slug, rfc_ref):
                    shipped_at = str(ev.get("at") or shipped_at)

            item_obj: dict[str, Any] = {
                "id": item_id,
                "kind": "feature",
                "repo": repo_name,
                "title": title,
                "rfc": rfc_ref,
                "from_request": str(spec_meta.get("from_request") or (rfc_info["meta"].get("from_request") if rfc_info else "") or ""),
                "stage": stage,
                "state": state,
                "state_plain": state_plain,
                "legacy": legacy,
                "owner": owner,
                "requested_by": requested_by,
                "last_activity": last_act,
                "shipped_at": shipped_at,
                "progress": {"done": done_count, "total": total_count, "current": current_task},
                "tasks": tasks_list,
                "waiting_on": waiting_on,
                "last_note": last_note,
                "summary": summary_txt,
                "done_means": done_means_txt,
                "blocked_by": blocked_by,
                "relations": {
                    k: spec_meta[k]
                    for k in ("depends_on", "builds_against", "extends", "amends", "supersedes")
                    if spec_meta.get(k)
                },
                "documents": docs,
                "commits": linked_commits,
                "changes": changes,
                "source_ref": source_ref,
            }
            items.append(item_obj)

    # Export unclaimed RFCs (RFCs without a spec in this repo yet)
    for rfc_id, rfc_info in rfc_by_id.items():
        if rfc_info["claimed_by_spec"]:
            continue
        meta = rfc_info["meta"]
        doc = rfc_info["doc"]
        app_state = doc["approval"]
        owner = resolve_person(str(meta.get("owner") or meta.get("requested_by") or ""), people)
        requested_by = resolve_person(str(meta.get("requested_by") or owner), people)
        if app_state == "stale":
            state_plain = "Changed after approval, needs re-approval"
        elif app_state == "approved":
            state_plain = "Approved, not started"
        elif app_state == "in-review":
            needs = doc["still_needed"] if isinstance(doc["still_needed"], list) else []
            state_plain = f"Ready for approval by {_format_names(needs)}"
        else:
            state_plain = "Being discussed" if rfc_info["interview_rows"] or str(meta.get("status") or "").lower() == "draft" else "Being discussed"

        if app_state in ("draft", "in-review", "stale"):
            needs = doc["still_needed"] if isinstance(doc["still_needed"], list) else [f"{doc['still_needed']} sign-off(s)"]
            if not needs and owner:
                needs = [owner]
            approvals_waiting.append(
                {
                    "document": f"{repo_name}/{doc['path']}" if id_prefix else doc["path"],
                    "state": app_state,
                    "needs": needs,
                }
            )
        for sig in doc.get("signed", []):
            if sig.get("when"):
                ev_ap = {
                    "at": sig["when"],
                    "type": "approved",
                    "item": doc["path"],
                    "who": resolve_person(sig["who"], people),
                    "source": "approval",
                }
                if sig.get("via"):
                    ev_ap["via"] = sig["via"]
                events.append(ev_ap)

        item_id = f"{id_prefix}{rfc_id}" if id_prefix else rfc_id
        items.append(
            {
                "id": item_id,
                "kind": "rfc",
                "repo": repo_name,
                "title": str(meta.get("title") or rfc_id),
                "rfc": rfc_id,
                "from_request": str(meta.get("from_request") or ""),
                "stage": "rfc",
                "state": f"rfc — {app_state}",
                "state_plain": state_plain,
                "legacy": False,
                "owner": owner,
                "requested_by": requested_by,
                "last_activity": rfc_info["last_activity"],
                "summary": rfc_info["summary"],
                "done_means": rfc_info["done_means"],
                "blocked_by": [],
                "waiting_on": [],
                "documents": [doc],
                "commits": rfc_info["commits"],
                "changes": [],
                "source_ref": source_ref,
            }
        )

    # Export Bugs (bugs/NNN-slug.md)
    bugs_dir = repo / "bugs"
    if bugs_dir.is_dir():
        for bp in sorted(bugs_dir.glob("*.md")):
            if bp.name.lower() == "readme.md":
                continue
            rel = bp.relative_to(repo).as_posix()
            try:
                meta, body = split_front_matter(bp.read_text(encoding="utf-8"))
            except FrontMatterError:
                continue
            bug_stage = str(meta.get("status") or "open").strip().lower()
            if bug_stage not in ("open", "diagnosed", "fixed", "closed"):
                bug_stage = "open"
            plain_map = {
                "open": "Reported, not looked at yet",
                "diagnosed": "Cause found, being fixed",
                "fixed": "Fixed, waiting to be checked",
                "closed": "Closed",
            }
            bug_num_m = re.match(r"^(\d+)-", bp.stem)
            bug_tok = f"bug {bug_num_m.group(1)}" if bug_num_m else bp.stem
            reported_by = resolve_person(str(meta.get("reported_by") or ""), people)
            owner = resolve_person(str(meta.get("owner") or meta.get("fixed_by") or ""), people)
            fixed_by = resolve_person(str(meta.get("fixed_by") or ""), people)
            expected = redact_lines(extract_section(body, r"expected"))
            actual = redact_lines(extract_section(body, r"actual|what happened"))
            where_when = redact_lines(extract_section(body, r"where"))
            item_id = f"{id_prefix}bugs/{bp.stem}" if id_prefix else f"bugs/{bp.stem}"
            waiting_bug: list[dict[str, Any]] = []
            if bug_stage == "fixed" and reported_by:
                waiting_bug.append(
                    {
                        "person": reported_by,
                        "task": f"BUG-{bug_num_m.group(1)}" if bug_num_m else bp.stem,
                        "what": "Verify fix, then set status: closed",
                        "inferred": True,
                    }
                )
            linked_commits = link_commits(commits, [rel], [bug_tok])
            items.append(
                {
                    "id": item_id,
                    "kind": "bug",
                    "repo": repo_name,
                    "title": str(meta.get("title") or re.sub(r"^\d+-", "", bp.stem).replace("-", " ").title()),
                    "rfc": str(meta.get("rfc") or ""),
                    "feature": str(meta.get("feature") or ""),
                    "from_request": str(meta.get("from_request") or ""),
                    "stage": bug_stage,
                    "state": f"{bug_stage}" + (f" ({meta['cause']})" if meta.get("cause") else ""),
                    "state_plain": plain_map[bug_stage],
                    "severity": str(meta.get("severity") or "medium").lower(),
                    "legacy": False,
                    "owner": owner,
                    "reported_by": reported_by,
                    "fixed_by": fixed_by,
                    "expected": expected,
                    "actual": actual,
                    "where": where_when,
                    "last_activity": last_commit_date_for_paths(file_dates, [rel]),
                    "waiting_on": waiting_bug,
                    "documents": [],
                    "commits": linked_commits,
                    "changes": [],
                    "source_ref": source_ref,
                }
            )

    # Export Requests (requests/REQ-NNNN-slug.md)
    req_dir = repo / "requests"
    if req_dir.is_dir():
        for rp in sorted(req_dir.glob("REQ-*.md")):
            rel = rp.relative_to(repo).as_posix()
            try:
                meta, body = split_front_matter(rp.read_text(encoding="utf-8"))
            except FrontMatterError:
                continue
            req_id = str(meta.get("id") or "").strip()
            if not req_id:
                m = re.match(r"^(REQ-\d+)", rp.name)
                req_id = m.group(1) if m else rp.stem
            req_kind = str(meta.get("kind") or "feature").lower()
            req_stage = str(meta.get("status") or "new").lower()
            if req_stage not in ("new", "questions", "interviewing", "converted", "declined"):
                req_stage = "new"
            req_by = resolve_person(str(meta.get("requested_by") or "Requester"), people)
            questions = parse_request_questions(body)
            unanswered = [q for q in questions if not q.get("answer")]
            if unanswered and req_stage in ("new", "questions", "interviewing"):
                req_stage = "questions"

            converted_to = str(meta.get("converted_to") or "").strip()
            declined_reason = str(meta.get("declined_reason") or "").strip()
            waiting_req: list[dict[str, Any]] = []
            if req_stage == "questions":
                n_q = len(unanswered) or len(questions)
                state_plain = f"Waiting on {req_by} to answer {n_q} question{'s' if n_q != 1 else ''}"
                waiting_req.append(
                    {
                        "person": req_by,
                        "task": req_id,
                        "what": f"Answer {n_q} question{'s' if n_q != 1 else ''} about your request",
                        "inferred": False,
                    }
                )
            elif req_stage == "interviewing":
                state_plain = "Being discussed"
            elif req_stage == "converted":
                state_plain = f"Became {converted_to}" if converted_to else "Converted"
            elif req_stage == "declined":
                state_plain = f"Declined: {declined_reason}" if declined_reason else "Declined"
            else:
                state_plain = f"Asked for by {req_by}, waiting for the interview"

            attach_dir = repo / "requests" / rp.stem
            attachments = []
            if attach_dir.is_dir():
                attachments = [p.relative_to(repo).as_posix() for p in sorted(attach_dir.iterdir()) if p.is_file()]

            item_id = f"{id_prefix}{req_id}" if id_prefix else req_id
            items.append(
                {
                    "id": item_id,
                    "req_id": req_id,
                    "kind": "request",
                    "request_kind": req_kind,
                    "repo": repo_name,
                    "title": str(meta.get("title") or req_id),
                    "stage": req_stage,
                    "state": f"request — {req_stage}",
                    "state_plain": state_plain,
                    "legacy": False,
                    "requested_by": req_by,
                    "requested_at": str(meta.get("requested_at") or ""),
                    "owner": "",
                    "last_activity": last_commit_date_for_paths(file_dates, [rel]) or str(meta.get("requested_at") or ""),
                    "fields": _parse_request_fields(req_kind, body),
                    "questions": questions,
                    "converted_to": converted_to,
                    "declined_reason": declined_reason,
                    "attachments": attachments,
                    "waiting_on": waiting_req,
                    "documents": [
                        {
                            "type": "request",
                            "path": rel,
                            "title": str(meta.get("title") or req_id),
                            "approval": "draft",
                            "approvers": [],
                            "signers": [],
                            "signed": [],
                            "still_needed": [],
                            "can_approve": False,
                            "preflight_reason": "Requests are not approved directly",
                            "body_hash": "",
                            "changes_since_approval": [],
                        }
                    ],
                    "commits": link_commits(commits, [rel], [req_id]),
                    "changes": [],
                    "source_ref": source_ref,
                }
            )

    events.sort(key=lambda e: e.get("at") or "", reverse=True)

    return {
        "schema_version": SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "kind": "repo",
        "name": repo_name,
        "commit": short_sha,
        "generated_at": utc_now_iso(),
        "repos": [repo_name],
        "people": people,
        "items": items,
        "approvals_waiting": approvals_waiting,
        "events": events,
        "health": {
            "check_errors": check_res["check_errors"],
            "check_warnings": check_res["check_warnings"],
            "stale_documents": check_res["stale_documents"],
        },
        "groundwork": doctor_block,
        "sub_repo_groundwork": {},
        "sub_repo_errors": {},
    }


def _discover_gitmodules(workspace: Path) -> list[tuple[str, Path]]:
    gm = workspace / ".gitmodules"
    if not gm.is_file():
        return []
    sub_repos: list[tuple[str, Path]] = []
    current_name = ""
    current_path = ""
    for raw in gm.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        m = re.match(r'^\[submodule\s+"([^"]+)"\]$', s)
        if m:
            if current_path:
                sub_repos.append((Path(current_path).name, workspace / current_path))
            current_name = m.group(1)
            current_path = ""
            continue
        if "=" in s:
            k, v = [x.strip() for x in s.split("=", 1)]
            if k == "path":
                current_path = v
    if current_path:
        sub_repos.append((Path(current_path).name or current_name, workspace / current_path))
    return sub_repos


def _merge_people(all_people_lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for plist in all_people_lists:
        for p in plist:
            key = p["name"].lower()
            if key not in merged:
                merged[key] = {
                    "name": p["name"],
                    "role": p.get("role", ""),
                    "contact": p.get("contact", ""),
                    "aliases": list(p.get("aliases", [])),
                }
            else:
                if not merged[key]["role"] and p.get("role"):
                    merged[key]["role"] = p["role"]
                if not merged[key]["contact"] and p.get("contact"):
                    merged[key]["contact"] = p["contact"]
                for a in p.get("aliases", []):
                    if a not in merged[key]["aliases"]:
                        merged[key]["aliases"].append(a)
    return list(merged.values())


def export_repo(
    repo_path: Path | str,
    source_ref: str = "main",
    sub_repos: list[tuple[str, Path | str]] | None = None,
) -> dict[str, Any]:
    """Export a single repository or workspace + sub-repos to schema v1.0 JSON."""
    repo = Path(repo_path).resolve()
    cfg = load_config(repo)
    is_workspace = cfg.get("kind") == "workspace" or (repo / ".gitmodules").is_file() or bool(sub_repos)
    ws_name = str(cfg.get("name") or repo.name)

    if not is_workspace:
        return _export_single_repo(repo, ws_name, source_ref=source_ref)

    ws_export = _export_single_repo(repo, ws_name, source_ref=source_ref)
    ws_export["kind"] = "workspace"
    ws_export["repos"] = []

    resolved_subs: list[tuple[str, Path]] = []
    if sub_repos is not None:
        resolved_subs = [(n, Path(p).resolve()) for n, p in sub_repos]
    else:
        for sub_name, sub_p in _discover_gitmodules(repo):
            if sub_p.exists():
                resolved_subs.append((sub_name, sub_p))

    people_lists = [ws_export["people"]]
    items = list(ws_export["items"])
    approvals_waiting = list(ws_export["approvals_waiting"])
    events = list(ws_export["events"])
    sub_gw: dict[str, Any] = {}
    sub_err: dict[str, str] = {}
    total_errors = ws_export["health"]["check_errors"]
    total_warnings = ws_export["health"]["check_warnings"]
    total_stale = ws_export["health"]["stale_documents"]

    for sub_name, sub_p in resolved_subs:
        ws_export["repos"].append(sub_name)
        try:
            if not sub_p.exists():
                raise FileNotFoundError(f"Repository directory missing: {sub_p}")
            sub_exp = _export_single_repo(
                sub_p,
                sub_name,
                source_ref=source_ref,
                workspace_repo=repo,
                id_prefix=f"{sub_name}/",
            )
            people_lists.append(sub_exp["people"])
            items.extend(sub_exp["items"])
            approvals_waiting.extend(sub_exp["approvals_waiting"])
            events.extend(sub_exp["events"])
            sub_gw[sub_name] = sub_exp["groundwork"]
            total_errors += sub_exp["health"]["check_errors"]
            total_warnings += sub_exp["health"]["check_warnings"]
            total_stale += sub_exp["health"]["stale_documents"]
        except Exception as exc:
            sub_err[sub_name] = str(exc)

    # N5: workspace stage reflects weakest repo when sub-repos exist
    if sub_gw:
        all_blocks = [ws_export["groundwork"], *sub_gw.values()]
        weakest = min(all_blocks, key=lambda b: b.get("stage_number", 0))
        ws_export["groundwork"]["effective_stage"] = weakest["stage"]
        ws_export["groundwork"]["effective_stage_number"] = weakest["stage_number"]

    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    ws_export["people"] = _merge_people(people_lists)
    ws_export["items"] = items
    ws_export["approvals_waiting"] = approvals_waiting
    ws_export["events"] = events
    ws_export["health"] = {
        "check_errors": total_errors,
        "check_warnings": total_warnings,
        "stale_documents": total_stale,
    }
    ws_export["sub_repo_groundwork"] = sub_gw
    ws_export["sub_repo_errors"] = sub_err
    return ws_export
