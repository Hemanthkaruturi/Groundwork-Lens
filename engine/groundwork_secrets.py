"""Deterministic secrets scanner for GroundWork documents."""

from __future__ import annotations

import re

_SECRET_PATTERNS = [
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("GitHub Fine-Grained Token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("GitLab Token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Private Key Header", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    (
        "Credential Assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|client[_-]?secret|auth[_-]?token|access[_-]?token|secret[_-]?key|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+=._-]{12,}['\"]?"
        ),
    ),
]


def is_secret_line(line: str) -> str | None:
    """Return the matched secret rule label if a line appears to contain a secret."""
    for label, pat in _SECRET_PATTERNS:
        if pat.search(line):
            return label
    return None


def scan_text(text: str) -> list[dict[str, object]]:
    """Return list of findings {line_number, label} without echoing secret values."""
    findings: list[dict[str, object]] = []
    for idx, line in enumerate((text or "").splitlines(), start=1):
        label = is_secret_line(line)
        if label:
            findings.append({"line_number": idx, "label": label})
    return findings


def redact_lines(text: str) -> str:
    """Drop any line flagged by the secrets scanner (Section 11)."""
    kept: list[str] = []
    for line in (text or "").splitlines():
        if is_secret_line(line) is None:
            kept.append(line)
    return "\n".join(kept)
