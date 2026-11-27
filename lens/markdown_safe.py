"""Safe markdown rendering (no raw HTML, no JS links, TOC extraction)."""

from __future__ import annotations

import html
import re
from typing import Any

from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark", {"html": False, "breaks": False, "linkify": False}).enable("table")

_HEADING_RE = re.compile(r"<h([1-6])>(.*?)</h\1>", re.DOTALL)
_A_TAG_RE = re.compile(r'<a\s+href="([^"]*)"([^>]*)>', re.IGNORECASE)
_MARKER_RE = re.compile(r"(\[(?:TODO|NEEDS CLARIFICATION)\])", re.IGNORECASE)


def _slugify_heading(raw_html: str) -> str:
    plain = re.sub(r"<[^>]+>", "", raw_html)
    plain = html.unescape(plain).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    return slug or "section"


def _sanitize_link(match: re.Match[str]) -> str:
    href = html.unescape(match.group(1)).strip()
    rest = match.group(2)
    low = href.lower()
    if low.startswith(("javascript:", "vbscript:", "data:")):
        return '<a href="#blocked-link">'
    safe_href = html.escape(href, quote=True)
    if low.startswith(("http://", "https://")):
        return f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer"{rest}>'
    return f'<a href="{safe_href}"{rest}>'


def render_markdown_safe(
    text: str,
    highlight_markers: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Render markdown with HTML disabled, external link hardening, and TOC extraction.

    Returns (safe_html, toc_list).
    """
    rendered = _MD.render(text or "")
    rendered = _A_TAG_RE.sub(_sanitize_link, rendered)

    toc: list[dict[str, Any]] = []
    seen_slugs: dict[str, int] = {}

    def _inject_heading(match: re.Match[str]) -> str:
        level = int(match.group(1))
        inner = match.group(2)
        base_slug = _slugify_heading(inner)
        cnt = seen_slugs.get(base_slug, 0) + 1
        seen_slugs[base_slug] = cnt
        slug = base_slug if cnt == 1 else f"{base_slug}-{cnt}"
        plain = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if level <= 3:
            toc.append({"level": level, "text": plain, "slug": slug})
        return f'<h{level} id="{slug}">{inner}</h{level}>'

    rendered = _HEADING_RE.sub(_inject_heading, rendered)

    if highlight_markers:
        rendered = _MARKER_RE.sub(r'<mark class="marker-unresolved">\1</mark>', rendered)

    return rendered, toc
