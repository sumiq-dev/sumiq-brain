"""Create a new, empty vault with the fixed top-level structure."""

from __future__ import annotations

import datetime as dt
from importlib import resources
from pathlib import Path

from . import markdown as md
from .config import CONFIG_FILE, DEFAULT_TOP_LEVEL, VaultConfig
from .gitstore import GitStore
from .ulid import new_ulid

GITIGNORE = """\
.brain/cache/
.obsidian/workspace*.json
.trash/
.DS_Store
"""

GITATTRIBUTES_LFS = """\
sources/**/original.* filter=lfs diff=lfs merge=lfs -text
conversations/**/original.* filter=lfs diff=lfs merge=lfs -text
"""


def conventions_text() -> str:
    return resources.files("aibrain").joinpath("conventions.md").read_text(encoding="utf-8")


def index_document(title: str, summary: str, overview: str = "") -> md.Document:
    today = dt.date.today().isoformat()
    meta = {
        "id": new_ulid(), "type": "index", "title": title, "summary": summary,
        "created": today, "updated": today, "author": "agent:brain",
    }
    body = f"# {title}\n\n"
    if overview:
        body += f"{overview.strip()}\n\n"
    body += "## Children\n"
    return md.Document(meta, body)


def init_vault(path: Path, name: str | None = None, git: bool = True) -> list[str]:
    """Create a vault at `path`. Returns a list of human-readable notices."""
    from .vault import Vault  # local import: vault depends on this module

    path = path.resolve()
    if (path / CONFIG_FILE).exists():
        raise FileExistsError(f"{path} is already a brain vault")
    if path.exists() and any(p for p in path.iterdir() if p.name != ".git"):
        raise FileExistsError(f"{path} is not empty")

    notices = []
    path.mkdir(parents=True, exist_ok=True)
    config = VaultConfig(name=name or path.name)
    (path / CONFIG_FILE).write_text(config.dump(), encoding="utf-8")
    (path / ".brain" / "log").mkdir(parents=True)
    (path / ".brain" / "cache").mkdir()
    (path / ".brain" / "schema.md").write_text(conventions_text(), encoding="utf-8")
    (path / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

    for folder in config.top_level:
        title, summary = DEFAULT_TOP_LEVEL.get(folder, (folder.title(), f"{folder.title()}."))
        (path / folder).mkdir()
        (path / folder / "index.md").write_text(md.render(index_document(title, summary)), encoding="utf-8")

    root_overview = "Second brain maintained by AI agents. Start here and follow the summaries down."
    (path / "index.md").write_text(
        md.render(index_document(config.name, f"Root of the {config.name} brain.", root_overview)),
        encoding="utf-8",
    )

    vault = Vault(path)
    for folder in config.top_level:
        vault.refresh_index(folder)
    vault.refresh_index(".")

    store = GitStore(path)
    if git:
        if not store.installed():
            notices.append("git not found: vault created without version history")
        else:
            if not (path / ".git").exists():
                store.init()
            if store.lfs_installed():
                (path / ".gitattributes").write_text(GITATTRIBUTES_LFS, encoding="utf-8")
            else:
                notices.append("git-lfs not found: source originals will be stored in plain git")
            store.commit(["."], f"Initialize brain '{config.name}'", "agent:brain")
    return notices
