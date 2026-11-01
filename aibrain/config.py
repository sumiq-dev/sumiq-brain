"""Vault-level configuration stored in <vault>/brain.yaml. Never holds secrets or machine paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_FILE = "brain.yaml"

# Fixed top-level structure: folder -> (title, summary). Agents organize inside these, never around them.
DEFAULT_TOP_LEVEL = {
    "projects": ("Projects", "Things with a goal and an end; one folder per project."),
    "areas": ("Areas", "Ongoing responsibilities without an end date, like health, finance or career."),
    "topics": ("Topics", "Knowledge and reference material not tied to a single project."),
    "people": ("People", "One note per person: who they are, context, interactions."),
    "journal": ("Journal", "Dated notes about events, meetings and decisions, by year."),
    "sources": ("Sources", "Original documents with extracted text and summaries, by year and month."),
    "conversations": ("Conversations", "Saved agent conversations with summaries, by year and month."),
    "inbox": ("Inbox", "Files waiting to be ingested."),
}


@dataclass
class Limits:
    index_max_entries: int = 50
    summary_max_words: int = 25
    note_max_words: int = 1500


@dataclass
class VaultConfig:
    name: str = "brain"
    version: int = 1
    top_level: list[str] = field(default_factory=lambda: list(DEFAULT_TOP_LEVEL))
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def load(cls, root: Path) -> "VaultConfig":
        path = root / CONFIG_FILE
        if not path.exists():
            raise FileNotFoundError(f"{root} is not a brain vault (missing {CONFIG_FILE})")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        limits = Limits(**(data.get("limits") or {}))
        return cls(
            name=data.get("name", root.name),
            version=data.get("version", 1),
            top_level=list(data.get("top_level") or DEFAULT_TOP_LEVEL),
            limits=limits,
        )

    def dump(self) -> str:
        data = {
            "version": self.version,
            "name": self.name,
            "top_level": self.top_level,
            "limits": vars(self.limits),
        }
        return yaml.safe_dump(data, sort_keys=False)
