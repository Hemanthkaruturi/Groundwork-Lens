"""GroundWork engine CLI entrypoint (Python standard library only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine.groundwork_approve import approve_document, evaluate_document
from engine.groundwork_board import build_board, format_board_text
from engine.groundwork_check import run_check
from engine.groundwork_common import (
    append_ledger,
    body_hash,
    dump_front_matter,
    parse_project_people,
    resolve_person,
    split_front_matter,
    utc_now_iso,
)
from engine.groundwork_doctor import run_doctor
from engine.groundwork_export import EXPORT_PATHS, export_repo
from engine.groundwork_requests import answer_request, load_fields_json, new_request


def _parse_sub_repos(raw_list: list[str] | None) -> list[tuple[str, Path]] | None:
    if not raw_list:
        return None
    out: list[tuple[str, Path]] = []
    for item in raw_list:
        if "=" in item:
            name, path_s = item.split("=", 1)
            out.append((name.strip(), Path(path_s.strip())))
    return out


def _cmd_export(args: argparse.Namespace) -> int:
    if args.paths:
        if args.json:
            print(json.dumps(EXPORT_PATHS))
        else:
            for p in EXPORT_PATHS:
                print(p)
        return 0
    sub_repos = _parse_sub_repos(args.sub_repo)
    snap = export_repo(args.repo, source_ref=args.ref, sub_repos=sub_repos)
    print(json.dumps(snap, indent=2))
    return 0


def _cmd_board(args: argparse.Namespace) -> int:
    board = build_board(args.repo)
    if args.json:
        print(json.dumps(board, indent=2))
    else:
        print(format_board_text(board))
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    res = run_check(Path(args.repo).resolve())
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for e in res["errors"]:
            print(f"ERROR [{e['rule']}] {e['path']}: {e['message']}")
        for w in res["warnings"]:
            print(f"WARN  [{w['rule']}] {w['path']}: {w['message']}")
        print(f"Summary: {res['check_errors']} errors, {res['check_warnings']} warnings, {res['stale_documents']} stale")
    return 1 if res["check_errors"] > 0 else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    res = run_doctor(Path(args.repo).resolve())
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"Stage {res['stage_number']}: {res['stage']}")
        for step in res["next_steps"]:
            print(f"  Next: {step}")
    return 0


def _cmd_new_request(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    fields = load_fields_json(args.fields)
    res = new_request(repo, args.kind, fields, attachments=args.attach or [])
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(res["path"])
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    payload = load_fields_json(args.fields)
    answers = payload.get("answers", payload)
    answered_by = payload.get("answered_by")
    if not isinstance(answers, dict):
        raise ValueError("Answers JSON must contain a mapping of question IDs to answers")
    res = answer_request(repo, args.req_id, {str(k): str(v) for k, v in answers.items()}, answered_by=answered_by)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(res["path"])
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    ws_repo = Path(args.workspace_repo).resolve() if args.workspace_repo else None
    res = approve_document(
        repo=repo,
        rel_path=args.path,
        who=args.who,
        via=args.via,
        expect_hash=args.expect_hash,
        change_note=args.change_note,
        workspace_repo=ws_repo,
    )
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        if res["ok"]:
            print(f"Approved {res['path']} ({res['state']})")
        else:
            print(res["error"], file=sys.stderr)
    return 0 if res.get("ok") else 1


def _cmd_read_doc(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    ws_repo = Path(args.workspace_repo).resolve() if args.workspace_repo else None
    doc_path = repo / args.path
    if not doc_path.is_file():
        print(json.dumps({"ok": False, "error": f"Document not found: {args.path}"}))
        return 1
    raw = doc_path.read_text(encoding="utf-8")
    meta, body = split_front_matter(raw)
    info = evaluate_document(repo, args.path, workspace_repo=ws_repo)
    info["ok"] = True
    info["body"] = body
    info["meta"] = meta
    print(json.dumps(info, indent=2))
    return 0


def _cmd_edit_doc(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    doc_path = repo / args.path
    if not doc_path.is_file():
        print(json.dumps({"ok": False, "error": f"Document not found: {args.path}"}))
        return 1
    raw = doc_path.read_text(encoding="utf-8")
    meta, old_body = split_front_matter(raw)
    current_hash = body_hash(old_body)
    if args.expect_hash and args.expect_hash != current_hash:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "This document changed since you opened it. Please review the updated version before saving.",
                    "expected_hash": args.expect_hash,
                    "actual_hash": current_hash,
                }
            )
        )
        return 1

    new_body = Path(args.body_file).read_text(encoding="utf-8").replace("\r\n", "\n")
    if args.change_note and args.change_note.strip():
        from engine.groundwork_approve import _append_change_note

        new_body = _append_change_note(new_body, args.change_note.strip())

    # Front matter is strictly locked (Section 9.7)
    doc_path.write_text(dump_front_matter(meta, new_body), encoding="utf-8")
    new_hash = body_hash(new_body)

    if args.who:
        append_ledger(
            repo,
            {
                "at": utc_now_iso(),
                "type": "edited",
                "item": args.path,
                "who": args.who,
            },
        )

    info = evaluate_document(repo, args.path)
    print(
        json.dumps(
            {
                "ok": True,
                "path": args.path,
                "body_hash": new_hash,
                "approval": info["approval"],
                "can_approve": info["can_approve"],
                "preflight_reason": info["preflight_reason"],
            },
            indent=2,
        )
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    snap = export_repo(repo)
    people = parse_project_people(repo)
    who = resolve_person(args.who, people) if args.who else ""

    waiting_tasks: list[dict[str, Any]] = []
    waiting_approvals: list[dict[str, Any]] = []
    for it in snap.get("items", []):
        for w in it.get("waiting_on", []):
            if not who or resolve_person(w["person"], people).lower() == who.lower():
                waiting_tasks.append({"item": it["id"], "person": w["person"], "what": w["what"], "inferred": w["inferred"]})
        for d in it.get("documents", []):
            if d["approval"] in ("draft", "in-review", "stale"):
                needs = d["still_needed"] if isinstance(d["still_needed"], list) else []
                if not who or any(resolve_person(n, people).lower() == who.lower() for n in needs):
                    waiting_approvals.append({"document": d["path"], "approval": d["approval"], "needs": needs})

    res = {"waiting_on_you": waiting_tasks, "waiting_on_your_approval": waiting_approvals}
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print("Waiting on you:")
        for t in waiting_tasks:
            print(f"  - {t['item']}: {t['what']} ({t['person']})")
        print("Waiting on your approval:")
        for a in waiting_approvals:
            print(f"  - {a['document']} ({a['approval']})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="groundwork.py", description="GroundWork engine CLI")
    sub = p.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", help="Export full GroundWork snapshot")
    p_exp.add_argument("repo", nargs="?", default=".")
    p_exp.add_argument("--json", action="store_true", help="Output JSON snapshot")
    p_exp.add_argument("--paths", action="store_true", help="Print paths read by GroundWork")
    p_exp.add_argument("--ref", default="main", help="Source ref label")
    p_exp.add_argument("--sub-repo", action="append", help="Sub-repo mapping name=path")
    p_exp.set_defaults(func=_cmd_export)

    p_brd = sub.add_parser("board", help="Show board sorted by git commit date")
    p_brd.add_argument("repo", nargs="?", default=".")
    p_brd.add_argument("--json", action="store_true")
    p_brd.set_defaults(func=_cmd_board)

    p_chk = sub.add_parser("check", help="Validate GroundWork rules")
    p_chk.add_argument("repo", nargs="?", default=".")
    p_chk.add_argument("--json", action="store_true")
    p_chk.set_defaults(func=_cmd_check)

    p_doc = sub.add_parser("doctor", help="Inspect GroundWork adoption stage")
    p_doc.add_argument("repo", nargs="?", default=".")
    p_doc.add_argument("--json", action="store_true")
    p_doc.set_defaults(func=_cmd_doctor)

    p_req = sub.add_parser("new-request", help="Create a new feature or bug request")
    p_req.add_argument("--kind", required=True, choices=["feature", "bug"])
    p_req.add_argument("--fields", required=True, help="JSON file with request fields")
    p_req.add_argument("--attach", action="append", help="Attachment image path")
    p_req.add_argument("--repo", default=".")
    p_req.add_argument("--json", action="store_true")
    p_req.set_defaults(func=_cmd_new_request)

    p_ans = sub.add_parser("answer", help="Answer interview questions on a request")
    p_ans.add_argument("req_id", help="Request ID (e.g. REQ-0001)")
    p_ans.add_argument("--fields", required=True, help="JSON file with answers")
    p_ans.add_argument("--repo", default=".")
    p_ans.add_argument("--json", action="store_true")
    p_ans.set_defaults(func=_cmd_answer)

    p_app = sub.add_parser("approve", help="Approve a governed GroundWork document")
    p_app.add_argument("path", help="Repo-relative path to document")
    p_app.add_argument("--who", required=True, help="Signer identity")
    p_app.add_argument("--via", default=None, help="Approval channel (e.g. lens)")
    p_app.add_argument("--expect-hash", default=None, help="Expected body SHA-256 hash")
    p_app.add_argument("--change-note", default=None, help="Dated change note for stale spec")
    p_app.add_argument("--workspace-repo", default=None, help="Workspace root for parent RFC checks")
    p_app.add_argument("--repo", default=".")
    p_app.add_argument("--json", action="store_true")
    p_app.set_defaults(func=_cmd_approve)

    p_rd = sub.add_parser("read-doc", help="Read a governed document with approval metadata")
    p_rd.add_argument("path")
    p_rd.add_argument("--workspace-repo", default=None)
    p_rd.add_argument("--repo", default=".")
    p_rd.add_argument("--json", action="store_true")
    p_rd.set_defaults(func=_cmd_read_doc)

    p_ed = sub.add_parser("edit-doc", help="Edit a document body while keeping front matter locked")
    p_ed.add_argument("path")
    p_ed.add_argument("--body-file", required=True)
    p_ed.add_argument("--expect-hash", default=None)
    p_ed.add_argument("--change-note", default=None)
    p_ed.add_argument("--who", default=None)
    p_ed.add_argument("--repo", default=".")
    p_ed.add_argument("--json", action="store_true")
    p_ed.set_defaults(func=_cmd_edit_doc)

    p_st = sub.add_parser("status", help="Show items waiting on a user")
    p_st.add_argument("repo", nargs="?", default=".")
    p_st.add_argument("--who", default="")
    p_st.add_argument("--json", action="store_true")
    p_st.set_defaults(func=_cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
