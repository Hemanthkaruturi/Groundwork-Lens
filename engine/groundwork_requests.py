"""Request document creation and interview question answering."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from engine.groundwork_common import (
    append_ledger,
    dump_front_matter,
    slugify,
    split_front_matter,
    utc_now_iso,
)


def next_request_number(repo: Path) -> int:
    """Return the next unused REQ-NNNN integer (numbers are never reused)."""
    req_dir = repo / "requests"
    max_num = 0
    if req_dir.is_dir():
        for p in req_dir.glob("REQ-*.md"):
            m = re.match(r"^REQ-(\d+)-", p.name)
            if m:
                max_num = max(max_num, int(m.group(1)))
    return max_num + 1


def new_request(
    repo: Path,
    kind: str,
    fields: dict[str, Any],
    attachments: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Create a GroundWork request document and ledger event."""
    if kind not in ("feature", "bug"):
        raise ValueError(f"Unsupported request kind: {kind!r}")

    title = str(fields.get("title") or "").strip()
    if not title:
        raise ValueError("Request title is required")

    requested_by = str(fields.get("requested_by") or "Unknown").strip()
    requested_at = str(fields.get("requested_at") or utc_now_iso()).strip()
    product_area = str(fields.get("product_area") or "Not sure").strip()

    req_num = next_request_number(repo)
    req_id = f"REQ-{req_num:04d}"
    slug = slugify(title)
    req_dir = repo / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)

    rel_path = f"requests/{req_id}-{slug}.md"
    full_path = repo / rel_path

    attachment_links: list[str] = []
    if attachments:
        attach_dir = req_dir / f"{req_id}-{slug}"
        attach_dir.mkdir(parents=True, exist_ok=True)
        for idx, src in enumerate(attachments, start=1):
            src_path = Path(src)
            if not src_path.is_file():
                raise FileNotFoundError(f"Attachment not found: {src}")
            ext = src_path.suffix.lower() or ".png"
            dest_name = f"{idx}{ext}"
            dest_path = attach_dir / dest_name
            shutil.copyfile(src_path, dest_path)
            attachment_links.append(f"![Attachment {idx}]({req_id}-{slug}/{dest_name})")

    meta: dict[str, Any] = {
        "id": req_id,
        "kind": kind,
        "title": title,
        "requested_by": requested_by,
        "requested_at": requested_at,
        "status": "new",
        "converted_to": "",
        "declined_reason": "",
    }

    sections: list[str] = [f"# {req_id}: {title}"]
    if kind == "feature":
        need = str(fields.get("need") or "").strip()
        done_looks_like = str(fields.get("done_looks_like") or "").strip()
        why_now = str(fields.get("why_now") or "").strip()
        if not need or not done_looks_like:
            raise ValueError("Feature requests require 'need' and 'done_looks_like'")
        sections.extend(
            [
                "## What is needed, and for whom",
                need,
                '## What "done" would look like',
                done_looks_like,
                "## Why now, and where it came from",
                why_now or "(none provided)",
                "## Product area",
                product_area,
            ]
        )
    else:
        expected = str(fields.get("expected") or "").strip()
        actual = str(fields.get("actual") or "").strip()
        where_when = str(fields.get("where_when") or "").strip()
        severity = str(fields.get("severity") or "medium").strip().lower()
        links = str(fields.get("links") or "").strip()
        if not expected or not actual or not where_when:
            raise ValueError("Bug requests require 'expected', 'actual', and 'where_when'")
        if severity not in ("low", "medium", "high", "critical"):
            raise ValueError(f"Invalid bug severity: {severity!r}")
        sections.extend(
            [
                "## What I expected",
                expected,
                "## What happened instead",
                actual,
                "## Where and when I saw it",
                where_when,
                "## Severity",
                severity,
                "## Links",
                links or "(none provided)",
                "## Product area",
                product_area,
            ]
        )

    if attachment_links:
        sections.extend(["## Attachments", "\n".join(attachment_links)])

    body = "\n\n".join(sections) + "\n"
    full_path.write_text(dump_front_matter(meta, body), encoding="utf-8")

    append_ledger(
        repo,
        {
            "at": requested_at,
            "type": "requested",
            "item": req_id,
            "who": requested_by,
        },
    )

    return {
        "id": req_id,
        "path": rel_path,
        "kind": kind,
        "title": title,
        "requested_by": requested_by,
        "requested_at": requested_at,
        "status": "new",
        "attachments": [f"requests/{req_id}-{slug}/{i}{Path(a).suffix.lower() or '.png'}" for i, a in enumerate(attachments or [], start=1)],
    }


def find_request_file(repo: Path, req_id: str) -> Path:
    """Locate the markdown file for a given REQ-NNNN identifier."""
    req_dir = repo / "requests"
    if not req_dir.is_dir():
        raise FileNotFoundError(f"Request {req_id} not found (no requests/ directory)")
    matches = sorted(req_dir.glob(f"{req_id}-*.md"))
    if not matches:
        matches = sorted(req_dir.glob(f"{req_id}.md"))
    if not matches:
        raise FileNotFoundError(f"Request {req_id} not found")
    return matches[0]


def parse_request_questions(body: str) -> list[dict[str, Any]]:
    """Parse questions and options under '## Questions for <name>'."""
    lines = (body or "").replace("\r\n", "\n").splitlines()
    in_q_section = False
    questions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in lines:
        stripped = line.strip()
        hm = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if hm:
            if in_q_section and len(hm.group(1)) <= 2:
                break
            if re.match(r"^questions for\b", hm.group(2).strip(), re.IGNORECASE):
                in_q_section = True
            continue
        if not in_q_section:
            continue

        qm = re.match(r"^(?:\d+\.|[-*])\s+\*\*(Q\d+)\*\*\s*[—:-]?\s*(.+)$", stripped)
        if not qm:
            qm = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if qm:
            if current is not None:
                questions.append(current)
            qid = qm.group(1) if qm.group(1).startswith("Q") else f"Q{qm.group(1)}"
            current = {"id": qid, "text": qm.group(2).strip(), "options": [], "answer": ""}
            continue

        if current is not None:
            am = re.match(r"^[-*]?\s*\*\*Answer:\*\*\s*(.+)$", stripped, re.IGNORECASE)
            if am:
                current["answer"] = am.group(1).strip()
                continue
            om = re.match(r"^(?:[-*]|\(?[a-zA-Z0-9]+\)?[.)])\s+(.+)$", stripped)
            if om:
                opt_text = om.group(1).strip()
                if not opt_text.lower().startswith("**answer:**"):
                    current["options"].append(opt_text)

    if current is not None:
        questions.append(current)
    return questions


def answer_request(
    repo: Path,
    req_id: str,
    answers: dict[str, str],
    answered_by: str | None = None,
) -> dict[str, Any]:
    """Write answers into a request's '## Questions for <name>' section."""
    req_path = find_request_file(repo, req_id)
    raw = req_path.read_text(encoding="utf-8")
    meta, body = split_front_matter(raw)

    lines = body.replace("\r\n", "\n").splitlines()
    out_lines: list[str] = []
    in_q_section = False
    current_qid: str | None = None
    answered_count = 0

    def _inject_answer(qid: str | None, buf: list[str]) -> None:
        nonlocal answered_count
        if qid and qid in answers and str(answers[qid]).strip():
            ans_val = str(answers[qid]).strip()
            # Replace existing Answer line if present inside this question's trailing lines
            replaced = False
            for idx in range(len(buf) - 1, -1, -1):
                if re.match(r"^\s*[-*]?\s*\*\*Answer:\*\*", buf[idx], re.IGNORECASE):
                    buf[idx] = f"  - **Answer:** {ans_val}"
                    replaced = True
                    break
                if re.match(r"^(?:\d+\.|[-*])\s+", buf[idx].strip()):
                    break
            if not replaced:
                buf.append(f"  - **Answer:** {ans_val}")
            answered_count += 1

    for line in lines:
        stripped = line.strip()
        hm = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if hm:
            if in_q_section and len(hm.group(1)) <= 2:
                _inject_answer(current_qid, out_lines)
                current_qid = None
                in_q_section = False
            elif re.match(r"^questions for\b", hm.group(2).strip(), re.IGNORECASE):
                in_q_section = True
            out_lines.append(line)
            continue

        if in_q_section:
            qm = re.match(r"^(?:\d+\.|[-*])\s+\*\*(Q\d+)\*\*\s*[—:-]?\s*(.+)$", stripped)
            if not qm:
                qm = re.match(r"^(\d+)\.\s+(.+)$", stripped)
            if qm:
                _inject_answer(current_qid, out_lines)
                current_qid = qm.group(1) if qm.group(1).startswith("Q") else f"Q{qm.group(1)}"
        out_lines.append(line)

    if in_q_section:
        _inject_answer(current_qid, out_lines)

    new_body = "\n".join(out_lines) + "\n"
    remaining_questions = parse_request_questions(new_body)
    all_answered = bool(remaining_questions) and all(q["answer"] for q in remaining_questions)
    if all_answered:
        meta["status"] = "interviewing"
    elif meta.get("status") == "questions" and answered_count > 0:
        unanswered = [q for q in remaining_questions if not q["answer"]]
        meta["status"] = "interviewing" if not unanswered else "questions"

    who = answered_by or str(meta.get("requested_by") or "Requester")
    req_path.write_text(dump_front_matter(meta, new_body), encoding="utf-8")

    append_ledger(
        repo,
        {
            "at": utc_now_iso(),
            "type": "answered",
            "item": req_id,
            "who": who,
        },
    )

    return {
        "id": req_id,
        "path": req_path.relative_to(repo).as_posix(),
        "status": meta.get("status"),
        "answered": answered_count,
        "questions": remaining_questions,
    }


def load_fields_json(path: str | Path) -> dict[str, Any]:
    """Load a JSON file passed via --fields."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Fields JSON must be an object")
    return data
