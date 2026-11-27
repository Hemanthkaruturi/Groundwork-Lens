"""Merge snapshots and build plain-language display models for Lens views."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_iso_dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        clean = ts.strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def relative_time(ts: str, now: datetime | None = None) -> str:
    dt = parse_iso_dt(ts)
    if dt is None:
        return "recently"
    ref = now or datetime.now(timezone.utc)
    secs = max(0, int((ref - dt).total_seconds()))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago" if mins != 1 else "1 min ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hours ago" if hours != 1 else "1 hour ago"
    days = hours // 24
    return f"{days} days ago" if days != 1 else "1 day ago"


def days_since(ts: str, now: datetime | None = None) -> int:
    dt = parse_iso_dt(ts)
    if dt is None:
        return 0
    ref = now or datetime.now(timezone.utc)
    return max(0, int((ref - dt).total_seconds() // 86400))


def user_aliases_set(user: dict[str, Any] | None, people: list[dict[str, Any]]) -> set[str]:
    """Build lowercase set of all identifiers matching the signed-in user via people[].aliases."""
    if not user:
        return set()
    tokens = {
        str(user.get("user_login") or "").strip().lower(),
        str(user.get("user_name") or "").strip().lower(),
        *(str(e).strip().lower() for e in user.get("verified_emails") or []),
    }
    tokens.discard("")
    expanded = set(tokens)
    for p in people:
        p_tokens = {str(p.get("name") or "").lower(), *(str(a).lower() for a in p.get("aliases") or [])}
        if tokens & p_tokens:
            expanded.update(p_tokens)
    expanded.discard("")
    return expanded


def format_change_actor(change: dict[str, Any]) -> str:
    """Format agent-assisted work transparently (E8): 'Hemanth, using Claude Code'."""
    who = str(change.get("who") or "Owner")
    via = str(change.get("via") or "").strip()
    if not via:
        return who
    nice_via = via.replace("-", " ").title()
    return f"{who}, using {nice_via}"


def merge_project_snapshots(
    project: dict[str, Any],
    main_snap_row: dict[str, Any] | None,
    pr_snap_rows: list[dict[str, Any]],
    send_backs: list[dict[str, Any]] | None = None,
    user: dict[str, Any] | None = None,
    only_mine: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Produce the complete display view-model from stored snapshots (zero markdown parsing)."""
    ref_now = now or datetime.now(timezone.utc)
    main_data: dict[str, Any] = (main_snap_row or {}).get("data") or {}
    people = list(main_data.get("people") or [])
    items_by_id: dict[str, dict[str, Any]] = {}

    for it in main_data.get("items") or []:
        copy_it = dict(it)
        copy_it["in_pr"] = None
        items_by_id[copy_it["id"]] = copy_it

    # Merge in-flight PR items (E2)
    for pr_row in pr_snap_rows:
        pr_data = pr_row.get("data") or {}
        for p in pr_data.get("people") or []:
            if p not in people:
                people.append(p)
        pr_meta = {
            "number": pr_row["pr_number"],
            "title": pr_row["pr_title"],
            "url": pr_row["pr_url"],
            "ref": pr_row["ref_name"],
        }
        for it in pr_data.get("items") or []:
            copy_it = dict(it)
            copy_it["in_pr"] = pr_meta
            existing = items_by_id.get(copy_it["id"])
            # Prefer PR snapshot only if item is new in PR or the PR's activity is newer than default branch
            if existing is None or (copy_it.get("last_activity") or "") > (existing.get("last_activity") or ""):
                items_by_id[copy_it["id"]] = copy_it

    user_tokens = user_aliases_set(user, people)
    send_back_by_doc: dict[str, list[dict[str, Any]]] = {}
    for sb in send_backs or []:
        send_back_by_doc.setdefault(sb["doc_path"], []).append(sb)

    def _matches_user(name_or_id: str) -> bool:
        return bool(name_or_id) and name_or_id.strip().lower() in user_tokens

    def _item_belongs_to_me(it: dict[str, Any]) -> bool:
        if not user_tokens:
            return False
        if _matches_user(it.get("requested_by", "")) or _matches_user(it.get("reported_by", "")):
            return True
        for w in it.get("waiting_on") or []:
            if _matches_user(w.get("person", "")):
                return True
        for d in it.get("documents") or []:
            sn = d.get("still_needed")
            if isinstance(sn, list) and any(_matches_user(n) for n in sn):
                return True
            if _matches_user(d.get("author", "")) and d.get("path") in send_back_by_doc:
                return True
        return False

    all_items = list(items_by_id.values())
    active_items = [it for it in all_items if not it.get("legacy")]
    legacy_items = [it for it in all_items if it.get("legacy")]

    if only_mine:
        active_items = [it for it in active_items if _item_belongs_to_me(it)]
        legacy_items = [it for it in legacy_items if _item_belongs_to_me(it)]

    idle_threshold = int(project.get("idle_days") or 14)

    waiting_list: list[dict[str, Any]] = []
    approvals_list: list[dict[str, Any]] = []
    building_list: list[dict[str, Any]] = []
    bugs_list: list[dict[str, Any]] = []
    shipped_list: list[dict[str, Any]] = []
    your_requests_list: list[dict[str, Any]] = []

    for it in active_items:
        it["last_activity_rel"] = relative_time(it.get("last_activity", ""), ref_now)
        it["waiting_days"] = days_since(it.get("last_activity", ""), ref_now)

        # Requests tracking ("Your requests")
        if it.get("kind") == "request":
            if not user_tokens or _matches_user(it.get("requested_by", "")):
                your_requests_list.append(it)
            # Requests in 'new' or 'questions' show in waiting_list
            if it.get("stage") in ("new", "questions"):
                for w in it.get("waiting_on") or []:
                    waiting_list.append(
                        {
                            "item": it,
                            "person": w["person"],
                            "what": w["what"],
                            "inferred": bool(w.get("inferred")),
                            "waiting_days": it["waiting_days"],
                            "kind_label": "questions",
                        }
                    )
            continue

        # Waiting on a person (explicit + inferred tasks, and fixed bugs awaiting check)
        for w in it.get("waiting_on") or []:
            waiting_list.append(
                {
                    "item": it,
                    "person": w["person"],
                    "what": w["what"],
                    "inferred": bool(w.get("inferred")),
                    "waiting_days": it["waiting_days"],
                    "kind_label": "task",
                }
            )

        # Sent-back documents under author's Waiting on me
        for d in it.get("documents") or []:
            for sb in send_back_by_doc.get(d["path"], []):
                waiting_list.append(
                    {
                        "item": it,
                        "person": d.get("author") or it.get("owner") or "Author",
                        "what": f"Sent back by {sb['by_name']}: {sb['note']}",
                        "inferred": False,
                        "waiting_days": days_since(sb.get("created_at", ""), ref_now),
                        "kind_label": "sent_back",
                    }
                )

            if d.get("approval") in ("draft", "in-review", "stale"):
                needs = d.get("still_needed")
                needs_list = needs if isinstance(needs, list) else ([f"{needs} sign-off(s)"] if needs else [])
                approvals_list.append(
                    {
                        "item": it,
                        "doc": d,
                        "needs": needs_list,
                        "is_stale": d.get("approval") == "stale",
                    }
                )

        # Open / active bugs
        if it.get("kind") == "bug":
            if it.get("stage") != "closed":
                bugs_list.append(it)
            continue

        # Shipped recently (handover/done or deployed in last 30 days)
        shipped_ts = it.get("shipped_at") or (it.get("last_activity") if it.get("stage") == "done" else "")
        if shipped_ts and days_since(shipped_ts, ref_now) <= 30:
            shipped_list.append(it)

        # Being built (features/RFCs not done and not legacy)
        if it.get("kind") == "feature" and it.get("stage") != "done":
            idle_d = days_since(it.get("last_activity", ""), ref_now)
            it["idle_notice"] = f"no activity for {idle_d} days" if idle_d >= idle_threshold else ""
            if it.get("last_note"):
                it["last_note_rel"] = relative_time(it["last_note"].get("at", ""), ref_now)
            building_list.append(it)

    # Sorting rules (Section 8.3)
    waiting_list.sort(key=lambda w: w["waiting_days"], reverse=True)  # oldest first
    building_list.sort(key=lambda it: it.get("last_activity") or "", reverse=True)  # newest first
    bugs_list.sort(key=lambda it: ({"critical": 0, "high": 1, "medium": 2, "low": 3}.get(it.get("severity", "medium"), 2), -(days_since(it.get("last_activity", ""), ref_now))))
    shipped_list.sort(key=lambda it: it.get("shipped_at") or it.get("last_activity") or "", reverse=True)

    # Detect N1-N9 state
    gw_block = main_data.get("groundwork") or {"stage": "not-started", "stage_number": 0, "found": [], "missing": [], "next_steps": [], "unreadable": []}
    setup_pr = None
    if gw_block.get("stage") == "not-started":
        for pr_row in pr_snap_rows:
            pr_gw = (pr_row.get("data") or {}).get("groundwork") or {}
            if pr_gw.get("stage") not in (None, "not-started"):
                setup_pr = {"number": pr_row["pr_number"], "title": pr_row["pr_title"], "url": pr_row["pr_url"]}
                break

    return {
        "snapshot": main_data,
        "commit": main_data.get("commit") or (main_snap_row or {}).get("commit_sha", "")[:7],
        "synced_rel": relative_time((main_snap_row or {}).get("created_at", ""), ref_now) if main_snap_row else "never",
        "people": people,
        "all_items": items_by_id,
        "waiting_list": waiting_list,
        "approvals_list": approvals_list,
        "building_list": building_list,
        "bugs_list": bugs_list,
        "shipped_list": shipped_list,
        "your_requests_list": your_requests_list,
        "legacy_items": legacy_items,
        "groundwork": gw_block,
        "sub_repo_groundwork": main_data.get("sub_repo_groundwork") or {},
        "sub_repo_errors": main_data.get("sub_repo_errors") or {},
        "setup_pr": setup_pr,
        "events": main_data.get("events") or [],
        "health": main_data.get("health") or {"check_errors": 0, "check_warnings": 0, "stale_documents": 0},
        "counts": {
            "waiting": len(waiting_list),
            "approvals": len(approvals_list),
            "building": len(building_list),
            "bugs": len(bugs_list),
        },
    }
