"""Document approval inspection, preflight checks, and recording."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from engine.groundwork_common import (
    FrontMatterError,
    append_ledger,
    body_hash,
    dump_front_matter,
    load_approvals,
    parse_project_people,
    resolve_person,
    save_approvals,
    split_front_matter,
    today_iso,
    utc_now_iso,
)
from engine.groundwork_requests import find_request_file, parse_request_questions
from engine.groundwork_stale import compute_changes_since_approval

_UNRESOLVED_MARKER_RE = re.compile(r"\[(?:TODO|NEEDS CLARIFICATION)\]", re.IGNORECASE)


def doc_type_from_path(rel_path: str) -> str:
    """Return GroundWork document type (rfc, adr, spec, plan, tasks, evals, handover, bug, request)."""
    p = rel_path.replace("\\", "/")
    if p.startswith("DECISIONS/RFC-"):
        return "rfc"
    if p.startswith("DECISIONS/ADR-"):
        return "adr"
    if p.startswith("bugs/"):
        return "bug"
    if p.startswith("requests/"):
        return "request"
    name = Path(p).name.lower()
    if name in ("spec.md", "plan.md", "tasks.md", "evals.md", "handover.md", "log.md", "changes.md"):
        return name[:-3]
    return "document"


def _normalize_signers(raw_signers: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in raw_signers:
        if isinstance(entry, str):
            normalized.append({"who": entry, "when": ""})
        elif isinstance(entry, dict):
            item: dict[str, Any] = {
                "who": str(entry.get("who") or ""),
                "when": str(entry.get("at") or entry.get("when") or ""),
            }
            if entry.get("via"):
                item["via"] = str(entry["via"])
            normalized.append(item)
    return normalized


def _normalize_approvers(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("approvers")
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _find_rfc_file(repo: Path, rfc_id: str) -> Path | None:
    dec_dir = repo / "DECISIONS"
    if not dec_dir.is_dir() or not rfc_id:
        return None
    clean = rfc_id.strip()
    matches = sorted(dec_dir.glob(f"{clean}*.md"))
    return matches[0] if matches else None


def _has_dated_change_note(body: str) -> bool:
    in_changes = False
    for line in (body or "").replace("\r\n", "\n").splitlines():
        s = line.strip()
        if re.match(r"^#{1,3}\s+changes\b", s, re.IGNORECASE):
            in_changes = True
            continue
        if in_changes and re.match(r"^#{1,3}\s+", s):
            break
        if in_changes and re.match(r"^[-*]\s+\d{4}-\d{2}-\d{2}\b", s):
            return True
    return False


def _append_change_note(body: str, note: str) -> str:
    note_line = f"- {today_iso()}: {note.strip()}"
    lines = (body or "").replace("\r\n", "\n").splitlines()
    for idx, line in enumerate(lines):
        if re.match(r"^#{1,3}\s+changes\b", line.strip(), re.IGNORECASE):
            insert_at = idx + 1
            while insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            lines.insert(insert_at, note_line)
            return "\n".join(lines).rstrip("\n") + "\n"
    base = body.rstrip("\n")
    if base:
        return base + f"\n\n## Changes\n\n{note_line}\n"
    return f"## Changes\n\n{note_line}\n"


def evaluate_document(
    repo: Path,
    rel_path: str,
    approvals_data: dict[str, Any] | None = None,
    people: list[dict[str, Any]] | None = None,
    workspace_repo: Path | None = None,
) -> dict[str, Any]:
    """Compute approval state, signers, preflight readiness, and stale diffs for a document."""
    if approvals_data is None:
        approvals_data = load_approvals(repo)
    if people is None:
        people = parse_project_people(repo)

    doc_path = repo / rel_path
    text = doc_path.read_text(encoding="utf-8")
    meta, body = split_front_matter(text)
    dtype = doc_type_from_path(rel_path)
    bhash = body_hash(body)

    approvers = _normalize_approvers(meta)
    has_explicit_req = "signoffs_required" in meta and meta["signoffs_required"] not in (None, "")
    signoffs_required = int(meta.get("signoffs_required") or 1)
    if signoffs_required < 1:
        signoffs_required = 1

    rec = approvals_data.get(rel_path)
    raw_signers = rec.get("signers", []) if isinstance(rec, dict) else []
    signed = _normalize_signers(raw_signers)
    signer_ids = [s["who"] for s in signed if s.get("who")]

    recorded_hash = rec.get("hash") if isinstance(rec, dict) else None
    is_stale = bool(recorded_hash and recorded_hash != bhash)

    if approvers:
        approver_canonical = [resolve_person(a, people) for a in approvers]
        signed_canonical = {resolve_person(w, people) for w in signer_ids}
        counted = [a for a in approver_canonical if a in signed_canonical]
        target = min(len(approver_canonical), signoffs_required) if has_explicit_req else len(approver_canonical)
        missing_approvers = [a for a in approver_canonical if a not in signed_canonical]
    else:
        counted = list(dict.fromkeys(resolve_person(w, people) for w in signer_ids))
        target = signoffs_required
        missing_approvers = []

    if is_stale:
        approval_state = "stale"
    elif rec and len(counted) >= target and target > 0:
        approval_state = "approved"
    elif str(meta.get("status") or "").lower() in ("in-review", "in_review", "ready") or (rec and len(counted) > 0):
        approval_state = "in-review"
    else:
        approval_state = "draft"

    still_needed: Any
    if approvers:
        still_needed = [] if approval_state == "approved" else missing_approvers
    else:
        remaining = max(0, target - len(counted))
        still_needed = 0 if approval_state == "approved" else remaining

    changes_since_approval: list[dict[str, Any]] = []
    if is_stale and recorded_hash:
        changes_since_approval = compute_changes_since_approval(repo, rel_path, recorded_hash, body)

    can_approve, preflight_reason, blocking_rfc = check_preflight(
        repo=repo,
        rel_path=rel_path,
        meta=meta,
        body=body,
        dtype=dtype,
        approval_state=approval_state,
        is_stale=is_stale,
        workspace_repo=workspace_repo,
    )

    return {
        "type": dtype,
        "path": rel_path,
        "title": str(meta.get("title") or Path(rel_path).stem),
        "author": resolve_person(str(meta.get("owner") or meta.get("requested_by") or ""), people),
        "approval": approval_state,
        "approvers": approvers,
        "signoffs_required": target,
        "signers": signer_ids,
        "signed": signed,
        "still_needed": still_needed,
        "can_approve": can_approve,
        "preflight_reason": preflight_reason,
        "blocking_rfc": blocking_rfc,
        "body_hash": bhash,
        "changes_since_approval": changes_since_approval,
        "meta": meta,
    }


def check_preflight(
    repo: Path,
    rel_path: str,
    meta: dict[str, Any],
    body: str,
    dtype: str,
    approval_state: str,
    is_stale: bool,
    workspace_repo: Path | None = None,
    change_note: str | None = None,
) -> tuple[bool, str, str]:
    """Check whether a document satisfies preflight rules for approval."""
    if _UNRESOLVED_MARKER_RE.search(body or ""):
        return False, "Resolve all [TODO] and [NEEDS CLARIFICATION] markers before approving.", ""

    # Open questions on linked request or inside document
    from_req = str(meta.get("from_request") or "").strip()
    if from_req:
        for search_root in [repo, workspace_repo] if workspace_repo else [repo]:
            if search_root is None:
                continue
            try:
                req_path = find_request_file(search_root, from_req)
                req_meta, req_body = split_front_matter(req_path.read_text(encoding="utf-8"))
                qs = parse_request_questions(req_body)
                unanswered = [q for q in qs if not q.get("answer")]
                if unanswered:
                    n = len(unanswered)
                    return False, f"{n} open question{'s' if n != 1 else ''} still need answers.", ""
                if str(req_meta.get("status") or "").lower() == "questions":
                    return False, "Open questions on the originating request still need answers.", ""
            except (FileNotFoundError, FrontMatterError):
                continue

    own_qs = parse_request_questions(body)
    unanswered_own = [q for q in own_qs if not q.get("answer")]
    if unanswered_own:
        n = len(unanswered_own)
        return False, f"{n} open question{'s' if n != 1 else ''} still need answers.", ""

    # Workspace RFC ordering check (E19)
    parent_ws_rfc = str(meta.get("workspace_rfc") or meta.get("parent_rfc") or "").strip()
    if parent_ws_rfc:
        search_root = workspace_repo or repo
        ws_rfc_path = _find_rfc_file(search_root, parent_ws_rfc)
        if ws_rfc_path is not None:
            ws_rel = ws_rfc_path.relative_to(search_root).as_posix()
            ws_approvals = load_approvals(search_root)
            ws_meta, ws_body = split_front_matter(ws_rfc_path.read_text(encoding="utf-8"))
            ws_rec = ws_approvals.get(ws_rel)
            ws_approved = bool(
                isinstance(ws_rec, dict)
                and ws_rec.get("hash") == body_hash(ws_body)
                and ws_rec.get("signers")
            )
            if not ws_approved:
                return (
                    False,
                    f"The workspace RFC ({parent_ws_rfc}) this belongs to must be approved first.",
                    parent_ws_rfc,
                )

    # Repo RFC ordering check for specs
    if dtype == "spec":
        rfc_ref = str(meta.get("rfc") or "").strip()
        if rfc_ref and rfc_ref.lower() not in ("(none)", "none", "legacy"):
            rfc_file = _find_rfc_file(repo, rfc_ref)
            if rfc_file is not None:
                rfc_rel = rfc_file.relative_to(repo).as_posix()
                rfc_approvals = load_approvals(repo)
                _, rfc_body = split_front_matter(rfc_file.read_text(encoding="utf-8"))
                rfc_rec = rfc_approvals.get(rfc_rel)
                rfc_approved = bool(
                    isinstance(rfc_rec, dict)
                    and rfc_rec.get("hash") == body_hash(rfc_body)
                    and rfc_rec.get("signers")
                )
                if not rfc_approved:
                    return False, f"The RFC ({rfc_ref}) this spec implements must be approved first.", rfc_ref

    if is_stale and dtype == "spec" and not change_note and not _has_dated_change_note(body):
        return (
            False,
            "This spec changed after a previous approval; add a dated note under ## Changes (or pass --change-note).",
            "",
        )

    return True, "", ""


def approve_document(
    repo: Path,
    rel_path: str,
    who: str,
    via: str | None = None,
    expect_hash: str | None = None,
    change_note: str | None = None,
    workspace_repo: Path | None = None,
) -> dict[str, Any]:
    """Run preflight checks and record an approval in .groundwork/approvals.json and front matter."""
    who_clean = (who or "").strip()
    if not who_clean:
        raise ValueError("--who is required")

    doc_path = repo / rel_path
    if not doc_path.is_file():
        raise FileNotFoundError(f"Document not found: {rel_path}")

    raw = doc_path.read_text(encoding="utf-8")
    meta, body = split_front_matter(raw)
    dtype = doc_type_from_path(rel_path)
    current_hash = body_hash(body)

    if expect_hash and expect_hash != current_hash:
        return {
            "ok": False,
            "error": "This document changed since you opened it. Please review the updated version before approving.",
            "expected_hash": expect_hash,
            "actual_hash": current_hash,
        }

    people = parse_project_people(repo)
    approvers = _normalize_approvers(meta)
    if approvers:
        who_canon = resolve_person(who_clean, people)
        allowed_canon = [resolve_person(a, people) for a in approvers]
        if who_canon not in allowed_canon and who_clean not in approvers:
            return {
                "ok": False,
                "error": f"Only named approvers ({', '.join(approvers)}) may approve this document.",
                "approvers": approvers,
            }

    approvals_data = load_approvals(repo)
    existing_rec = approvals_data.get(rel_path)
    is_stale = bool(isinstance(existing_rec, dict) and existing_rec.get("hash") and existing_rec.get("hash") != current_hash)

    ok, reason, blocking_rfc = check_preflight(
        repo=repo,
        rel_path=rel_path,
        meta=meta,
        body=body,
        dtype=dtype,
        approval_state="stale" if is_stale else str(meta.get("status") or "draft"),
        is_stale=is_stale,
        workspace_repo=workspace_repo,
        change_note=change_note,
    )
    if not ok:
        return {
            "ok": False,
            "error": reason,
            "blocking_rfc": blocking_rfc,
        }

    if change_note and change_note.strip():
        body = _append_change_note(body, change_note.strip())
        current_hash = body_hash(body)

    # Reset prior signers if body hash changed since previous approval
    signers: list[dict[str, Any]] = []
    if isinstance(existing_rec, dict) and existing_rec.get("hash") == current_hash:
        for entry in existing_rec.get("signers", []):
            if isinstance(entry, str):
                signers.append({"who": entry, "at": ""})
            elif isinstance(entry, dict):
                signers.append(dict(entry))

    who_canon = resolve_person(who_clean, people)
    already_signed = any(resolve_person(s.get("who", ""), people) == who_canon for s in signers)
    if not already_signed:
        signer_entry: dict[str, Any] = {"who": who_clean, "at": utc_now_iso()}
        if via:
            signer_entry["via"] = via
        signers.append(signer_entry)

    approvals_data[rel_path] = {
        "hash": current_hash,
        "signers": signers,
    }
    save_approvals(repo, approvals_data)

    # Evaluate resulting state and update front matter
    has_explicit_req = "signoffs_required" in meta and meta["signoffs_required"] not in (None, "")
    signoffs_required = max(1, int(meta.get("signoffs_required") or 1))
    if approvers:
        approver_canon = [resolve_person(a, people) for a in approvers]
        signed_canon = {resolve_person(s["who"], people) for s in signers}
        target = min(len(approver_canon), signoffs_required) if has_explicit_req else len(approver_canon)
        done_count = sum(1 for a in approver_canon if a in signed_canon)
        remaining = [a for a in approver_canon if a not in signed_canon]
    else:
        target = signoffs_required
        done_count = len({resolve_person(s["who"], people) for s in signers})
        remaining = []

    approved_now = done_count >= target
    meta["approved_by"] = [s["who"] for s in signers]
    meta["status"] = "approved" if approved_now else "in-review"
    doc_path.write_text(dump_front_matter(meta, body), encoding="utf-8")

    append_ledger(
        repo,
        {
            "at": utc_now_iso(),
            "type": "approved" if approved_now else "signed",
            "item": rel_path,
            "who": who_clean,
        },
    )

    return {
        "ok": True,
        "path": rel_path,
        "state": "approved" if approved_now else "in-review",
        "body_hash": current_hash,
        "signers": signers,
        "signed_count": done_count,
        "signoffs_required": target,
        "still_needed": remaining if approvers else max(0, target - done_count),
    }
