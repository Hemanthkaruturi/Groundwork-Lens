"""GroundWork CLI board view sorted by git commit dates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.groundwork_export import export_repo


def build_board(repo_path: Path | str) -> dict[str, Any]:
    """Return board rows sorted by git commit date (newest first), omitting converted requests."""
    snap = export_repo(repo_path)
    board_items = [
        item
        for item in snap.get("items", [])
        if not (item.get("kind") == "request" and item.get("stage") == "converted")
    ]
    board_items.sort(key=lambda it: it.get("last_activity") or "", reverse=True)
    return {
        "name": snap["name"],
        "kind": snap["kind"],
        "commit": snap["commit"],
        "items": board_items,
        "approvals_waiting": snap["approvals_waiting"],
        "health": snap["health"],
    }


def format_board_text(board: dict[str, Any]) -> str:
    """Format board summary for terminal users."""
    lines = [f"GroundWork Board: {board['name']} ({board['commit'] or 'no commits'})", ""]
    if not board["items"]:
        lines.append("No items recorded yet.")
        return "\n".join(lines)
    for it in board["items"]:
        tag = "[legacy] " if it.get("legacy") else ""
        lines.append(f"- {it['id']}: {tag}{it['state']} | {it['state_plain']} (last: {it.get('last_activity') or 'unknown'})")
        for w in it.get("waiting_on", []):
            inf = " (inferred)" if w.get("inferred") else ""
            lines.append(f"    waiting on {w['person']}{inf}: {w['what']}")
    return "\n".join(lines)
