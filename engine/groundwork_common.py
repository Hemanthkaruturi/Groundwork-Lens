"""Common GroundWork helpers: YAML front-matter, hashing, people & config."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class FrontMatterError(ValueError):
    """Raised when a GroundWork markdown file has invalid YAML front matter."""


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    """Return today's UTC date in YYYY-MM-DD format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def slugify(text: str, max_len: int = 48) -> str:
    """Convert text into a lowercase URL/filename-safe slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "item"


def body_hash(body: str) -> str:
    """Return deterministic SHA-256 hex digest of normalized markdown body."""
    normalized = (body or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_scalar(raw: str) -> Any:
    val = raw.strip()
    if val == "":
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        if len(val) < 2:
            raise FrontMatterError("Unterminated quoted string")
        return val[1:-1]
    if val.startswith('"') or val.startswith("'"):
        raise FrontMatterError(f"Unterminated quote in value: {val}")
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if val.startswith("["):
        if not val.endswith("]"):
            raise FrontMatterError(f"Unclosed inline list: {val}")
        inner = val[1:-1].strip()
        if not inner:
            return []
        items: list[Any] = []
        for part in _split_csv_respecting_quotes(inner):
            items.append(_parse_scalar(part))
        return items
    return val


def _split_csv_respecting_quotes(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in text:
        if ch in ('"', "'"):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            buf.append(ch)
        elif ch == "," and quote is None:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if quote is not None:
        raise FrontMatterError("Unterminated quote inside list")
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Parse optional YAML front matter and return (metadata, body)."""
    norm = (text or "").replace("\r\n", "\n")
    if not norm.startswith("---\n") and norm.strip() != "---":
        return {}, norm

    lines = norm.split("\n")
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise FrontMatterError("Front matter starting with '---' is missing closing '---'")

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :])
    if body.startswith("\n"):
        body = body[1:]

    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in fm_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line.startswith((" ", "\t")) and stripped.startswith("- "):
            if current_list_key is None or not isinstance(meta.get(current_list_key), list):
                raise FrontMatterError(f"Unexpected list item without key: {raw_line}")
            meta[current_list_key].append(_parse_scalar(stripped[2:]))
            continue
        if ":" not in raw_line:
            raise FrontMatterError(f"Invalid front matter line (missing ':'): {raw_line}")
        key, rest = raw_line.split(":", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise FrontMatterError(f"Invalid front matter key: {key!r}")
        rest_stripped = rest.strip()
        if rest_stripped == "":
            meta[key] = []
            current_list_key = key
        else:
            meta[key] = _parse_scalar(rest_stripped)
            current_list_key = None

    return meta, body


def _format_scalar(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, list):
        formatted = ", ".join(_format_scalar(v) for v in val)
        return f"[{formatted}]"
    s = str(val)
    if s == "" or any(ch in s for ch in (":", "#", "[", "]", "{", "}", "\n")) or s.startswith((" ", "-", "'", '"')):
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    return s


def dump_front_matter(meta: dict[str, Any], body: str) -> str:
    """Serialize metadata dict and body back into front-mattered markdown."""
    lines = ["---"]
    for key, val in meta.items():
        lines.append(f"{key}: {_format_scalar(val)}")
    lines.append("---")
    lines.append("")
    clean_body = (body or "").replace("\r\n", "\n").lstrip("\n")
    if clean_body and not clean_body.endswith("\n"):
        clean_body += "\n"
    return "\n".join(lines) + clean_body


def read_json_safe(path: Path, default: Any) -> Any:
    """Read a JSON file if it exists, returning default otherwise."""
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(repo: Path) -> dict[str, Any]:
    """Load .groundwork/config.json if present."""
    cfg_path = repo / ".groundwork" / "config.json"
    if not cfg_path.is_file():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_approvals(repo: Path) -> dict[str, Any]:
    """Load .groundwork/approvals.json."""
    app_path = repo / ".groundwork" / "approvals.json"
    if not app_path.is_file():
        return {}
    try:
        data = json.loads(app_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_approvals(repo: Path, data: dict[str, Any]) -> None:
    """Persist .groundwork/approvals.json cleanly."""
    gw_dir = repo / ".groundwork"
    gw_dir.mkdir(parents=True, exist_ok=True)
    app_path = gw_dir / "approvals.json"
    app_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_ledger(repo: Path) -> list[dict[str, Any]]:
    """Load .groundwork/ledger.jsonl events."""
    ledger_path = repo / ".groundwork" / "ledger.jsonl"
    if not ledger_path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for raw in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                events.append(obj)
        except json.JSONDecodeError:
            continue
    return events


def append_ledger(repo: Path, event: dict[str, Any]) -> None:
    """Append an event JSON line to .groundwork/ledger.jsonl."""
    gw_dir = repo / ".groundwork"
    gw_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = gw_dir / "ledger.jsonl"
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, separators=(",", ":")) + "\n")


def parse_project_people(repo: Path) -> list[dict[str, Any]]:
    """Parse PROJECT.md 'Who works on what' table and build alias sets."""
    proj_path = repo / "PROJECT.md"
    people: list[dict[str, Any]] = []
    if proj_path.is_file():
        lines = proj_path.read_text(encoding="utf-8", errors="replace").splitlines()
        in_section = False
        header_cols: list[str] = []
        for line in lines:
            stripped = line.strip()
            if re.match(r"^#{1,6}\s+who works on what\b", stripped, re.IGNORECASE):
                in_section = True
                header_cols = []
                continue
            if in_section and re.match(r"^#{1,6}\s+", stripped):
                break
            if not in_section:
                continue
            if not stripped.startswith("|"):
                if header_cols and stripped:
                    break
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not header_cols:
                header_cols = [c.lower() for c in cells]
                continue
            if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c):
                continue
            row = {header_cols[i]: cells[i] if i < len(cells) else "" for i in range(len(header_cols))}
            name = row.get("name", "").strip()
            if not name:
                continue
            role = row.get("role", "").strip()
            contact = row.get("contact", "").strip()
            aliases: list[str] = []
            for token in re.split(r"[,;/]+", contact):
                t = token.strip()
                if t and t not in aliases:
                    aliases.append(t)
            if name not in aliases:
                aliases.append(name)
            first_contact = aliases[0] if aliases else ""
            people.append(
                {
                    "name": name,
                    "role": role,
                    "contact": first_contact,
                    "aliases": aliases,
                }
            )
    return people


def resolve_person(identity: str, people: list[dict[str, Any]]) -> str:
    """Resolve an email, login, or name to canonical display name via people[].aliases."""
    raw = (identity or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    for p in people:
        if p.get("name", "").lower() == low:
            return p["name"]
        for alias in p.get("aliases", []):
            if alias.lower() == low:
                return p["name"]
    return raw


def extract_section(body: str, heading_regex: str) -> str:
    """Extract markdown section content under the first heading matching heading_regex."""
    lines = (body or "").replace("\r\n", "\n").splitlines()
    capturing = False
    capture_level = 0
    buf: list[str] = []
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if capturing and level <= capture_level:
                break
            if not capturing and re.search(heading_regex, title, re.IGNORECASE):
                capturing = True
                capture_level = level
                continue
        elif capturing:
            buf.append(line)
    return "\n".join(buf).strip()


def extract_headings(body: str) -> list[dict[str, Any]]:
    """Extract markdown headings with level and text."""
    headings: list[dict[str, Any]] = []
    in_fence = False
    for line in (body or "").replace("\r\n", "\n").splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if m:
            headings.append({"level": len(m.group(1)), "text": m.group(2).strip()})
    return headings
