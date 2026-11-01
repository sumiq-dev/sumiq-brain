"""Parsing and rendering of brain markdown: frontmatter, wikilinks, sections, human blocks."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import yaml

# Canonical frontmatter key order; unknown keys follow in their original order.
KEY_ORDER = [
    "id", "type", "title", "summary", "tags", "sources", "related", "supersedes",
    "status", "created", "updated", "author",
]

WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(#[^\]\|]*)?(?:\|([^\]]*))?\]\]")
HUMAN_BLOCK_RE = re.compile(r"<!--\s*human\s*-->.*?<!--\s*/human\s*-->", re.DOTALL)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


class FormatError(ValueError):
    """A file does not follow the brain's markdown conventions."""


@dataclass
class Document:
    meta: dict = field(default_factory=dict)
    body: str = ""


def _normalize(value):
    """Turn YAML-parsed dates into ISO strings so metadata stays JSON-friendly."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def parse(text: str) -> Document:
    """Split a markdown file into frontmatter and body."""
    if not text.startswith("---"):
        return Document({}, text)
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return Document({}, text)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            try:
                meta = yaml.safe_load(raw) or {}
            except yaml.YAMLError as e:
                raise FormatError(f"invalid frontmatter YAML: {e}") from e
            if not isinstance(meta, dict):
                raise FormatError("frontmatter must be a YAML mapping")
            return Document(_normalize(meta), body.lstrip("\n"))
    raise FormatError("frontmatter opened with '---' but never closed")


def render(doc: Document) -> str:
    """Render frontmatter + body back into file text."""
    ordered = {k: doc.meta[k] for k in KEY_ORDER if k in doc.meta}
    ordered.update({k: v for k, v in doc.meta.items() if k not in ordered})
    for k, v in ordered.items():  # write dates unquoted, the way Obsidian shows them
        if isinstance(v, str) and DATE_RE.match(v):
            ordered[k] = dt.date.fromisoformat(v)
    fm = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=1000).strip()
    body = doc.body.strip("\n")
    return f"---\n{fm}\n---\n\n{body}\n" if body else f"---\n{fm}\n---\n"


@dataclass(frozen=True)
class Link:
    target: str          # what's inside [[...]] before '#' or '|'
    heading: str | None  # '#section' part, without '#'
    alias: str | None    # '|alias' part


def find_links(text: str) -> list[Link]:
    links = []
    for m in WIKILINK_RE.finditer(text):
        heading = m.group(2)[1:] if m.group(2) else None
        links.append(Link(m.group(1).strip(), heading, m.group(3)))
    return links


def meta_links(meta: dict) -> list[Link]:
    """Wikilinks stored in frontmatter values (e.g. related: ["[[x]]"])."""
    found: list[Link] = []

    def walk(v):
        if isinstance(v, str):
            found.extend(find_links(v))
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)

    for key, value in meta.items():
        if key not in ("id", "title", "summary"):
            walk(value)
    return found


def human_blocks(body: str) -> list[str]:
    return HUMAN_BLOCK_RE.findall(body)


def overlaps_human_block(body: str, start: int, end: int) -> bool:
    return any(m.start() < end and start < m.end() for m in HUMAN_BLOCK_RE.finditer(body))


def replace_section(body: str, heading: str, content: str, level: int = 2) -> str:
    """Replace the body of a '## heading' section (up to the next heading of the
    same or higher level), or append the section if it doesn't exist."""
    pattern = re.compile(rf"^{'#' * level}\s+{re.escape(heading)}\s*$", re.MULTILINE)
    m = pattern.search(body)
    block = f"{'#' * level} {heading}\n{content.rstrip()}\n"
    if not m:
        return body.rstrip("\n") + ("\n\n" if body.strip() else "") + block
    end = len(body)
    for h in HEADING_RE.finditer(body, m.end()):
        if len(h.group(1)) <= level:
            end = h.start()
            break
    tail = body[end:]
    return body[:m.start()] + block + ("\n" + tail if tail else "")


def get_section(body: str, heading: str) -> str | None:
    """Return the text of a section by heading name (any level), including subsections."""
    for m in HEADING_RE.finditer(body):
        if m.group(2).strip().lower() == heading.strip().lower():
            level = len(m.group(1))
            end = len(body)
            for h in HEADING_RE.finditer(body, m.end()):
                if len(h.group(1)) <= level:
                    end = h.start()
                    break
            return body[m.start():end].strip()
    return None


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"
