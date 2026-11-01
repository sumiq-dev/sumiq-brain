"""Fallback full-text search. Navigation through indexes is the primary way to find things;
this is for when it dead-ends. Plain Python, no external index, so it works on any copy of the vault."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import markdown as md
from .vault import CONTENT_STEMS, INDEX, Vault, in_inbox

WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class Hit:
    path: str          # the note to open (for extracted text: its source note)
    title: str
    summary: str
    score: float
    snippet: str
    in_source_text: bool = False


def _terms(query: str) -> list[str]:
    return [t for t in WORD_RE.findall(query.lower()) if len(t) > 1]


def _snippet(text: str, terms: list[str], width: int = 200) -> str:
    lower = text.lower()
    positions = [lower.find(t) for t in terms if lower.find(t) >= 0]
    if not positions:
        return " ".join(text.split())[:width]
    start = max(0, min(positions) - width // 3)
    snippet = " ".join(text[start:start + width].split())
    return ("…" if start > 0 else "") + snippet + "…"


def search(vault: Vault, query: str, scope: str = ".", limit: int = 10) -> list[Hit]:
    terms = _terms(query)
    if not terms:
        return []
    scope = vault.norm(scope)
    base = vault.abs(scope)
    phrase = query.strip().lower()
    hits: dict[str, Hit] = {}

    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            full = Path(dirpath) / fn
            rel = full.relative_to(vault.root).as_posix()
            if in_inbox(rel):
                continue
            try:
                text = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            is_content = PurePosixPath(rel).stem in CONTENT_STEMS
            if is_content:
                folder = PurePosixPath(rel).parent
                note = f"{folder}/{folder.name}.md"
                meta, body = (vault._entries[note].meta if note in vault._entries else {}), text
            else:
                note = rel
                try:
                    doc = md.parse(text)
                except md.FormatError:
                    continue
                meta, body = doc.meta, doc.body

            title = str(meta.get("title") or PurePosixPath(note).stem)
            summary = str(meta.get("summary") or "")
            body_l, title_l, summary_l = body.lower(), title.lower(), summary.lower()
            score = 0.0
            matched = 0
            for t in terms:
                in_title, in_summary = t in title_l, t in summary_l
                count = body_l.count(t)
                if in_title or in_summary or count:
                    matched += 1
                score += 5 * in_title + 3 * in_summary + min(count, 20) ** 0.5
            if not matched:
                continue
            score *= matched / len(terms)          # favour documents matching all terms
            if len(terms) > 1 and phrase in body_l:
                score *= 1.5
            if is_content:
                score *= 0.6                       # notes outrank raw source text
            if PurePosixPath(note).name == INDEX:
                score *= 0.5                       # indexes are for navigating, not answers
            if meta.get("status") == "outdated":
                score *= 0.5

            hit = Hit(note, title, summary, round(score, 2), _snippet(body, terms), is_content)
            if note not in hits or hits[note].score < hit.score:
                hits[note] = hit

    return sorted(hits.values(), key=lambda h: h.score, reverse=True)[:limit]
