"""The vault: one portable folder of markdown notes, index notes and sources.

All reads and writes go through this class. Writes happen inside a transaction, which
serializes writers (also across processes), rolls back on error, logs the change and
commits it to git.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import markdown as md
from .config import VaultConfig
from .extractors import extract_text, guess_mime
from .gitstore import GitStore
from .scaffold import index_document
from .ulid import new_ulid

try:
    import fcntl
except ImportError:  # Windows: in-process locking only
    fcntl = None

INDEX = "index.md"
CONTENT_STEMS = {"original", "extracted", "transcript"}  # files inside source/conversation folders, not notes
RESERVED_STEMS = {"index"} | CONTENT_STEMS
REQUIRED_FIELDS = ("id", "type", "title", "summary", "created", "updated", "author")
PROTECTED_META = {"id", "type", "title", "summary", "status", "created", "updated", "author", "tags"}
STATUSES = {"current", "outdated", "draft"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RESCAN_SECONDS = 2.0
INBOX = "inbox"


def in_inbox(rel: str) -> bool:
    """Raw files waiting in the inbox are not part of the brain yet (only inbox/index.md is)."""
    return rel.startswith(f"{INBOX}/") and rel != f"{INBOX}/{INDEX}"


class VaultError(Exception):
    """A request the vault refuses; the message tells the caller how to fix it."""


def today() -> str:
    return dt.date.today().isoformat()


@dataclass
class Entry:
    path: str                       # vault-relative posix path of the .md file
    meta: dict = field(default_factory=dict)
    links: list[md.Link] = field(default_factory=list)
    error: str | None = None

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).stem

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.name)

    @property
    def summary(self) -> str:
        return str(self.meta.get("summary") or "")


@dataclass
class Transaction:
    actor: str
    message: str
    snapshots: dict[str, bytes | None] = field(default_factory=dict)  # None = file didn't exist
    new_dirs: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    committed: bool = False
    commit_error: str | None = None

    def touch(self, rel: str) -> None:
        if rel not in self.changed:
            self.changed.append(rel)


class Vault:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.config = VaultConfig.load(self.root)
        self.git = GitStore(self.root)
        self._entries: dict[str, Entry] = {}
        self._names: dict[str, set[str]] = {}
        self._scanned_at = 0.0
        self._lock = threading.RLock()
        self._tx: Transaction | None = None
        self.scan()

    # ------------------------------------------------------------------ paths

    def norm(self, path: str) -> str:
        """Normalize a vault-relative path; refuse anything outside the vault or in hidden folders."""
        p = (path or ".").strip().replace("\\", "/")
        pp = PurePosixPath(p)
        if pp.is_absolute():
            raise VaultError(f"'{path}': use a path relative to the vault root")
        parts = [x for x in pp.parts if x not in ("", ".")]
        if ".." in parts:
            raise VaultError(f"'{path}': paths may not contain '..'")
        if parts and parts[0].startswith("."):
            raise VaultError(f"'{path}': hidden folders like {parts[0]} are off limits")
        return "/".join(parts) or "."

    def abs(self, rel: str) -> Path:
        return self.root if rel == "." else self.root / rel

    @staticmethod
    def parent(rel: str) -> str:
        parent = str(PurePosixPath(rel).parent)
        return "." if parent in ("", ".") else parent

    @staticmethod
    def index_of(folder: str) -> str:
        return INDEX if folder == "." else f"{folder}/{INDEX}"

    def is_leaf_folder(self, folder: str) -> bool:
        """A folder represented by a folder note (e.g. a source folder) instead of an index."""
        if folder == ".":
            return False
        d = self.abs(folder)
        return not (d / INDEX).exists() and (d / f"{d.name}.md").is_file()

    # --------------------------------------------------------------- registry

    def scan(self) -> None:
        """(Re)build the in-memory registry of notes, names and links."""
        with self._lock:
            self._entries.clear()
            self._names.clear()
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for fn in filenames:
                    if fn.endswith(".md") and fn[:-3] not in CONTENT_STEMS:
                        rel = (Path(dirpath) / fn).relative_to(self.root).as_posix()
                        if in_inbox(rel):
                            continue
                        try:
                            self._register(rel, (Path(dirpath) / fn).read_text(encoding="utf-8"))
                        except OSError:
                            continue
            self._scanned_at = time.monotonic()

    def _fresh(self, force: bool = False) -> None:
        """Pick up external edits (e.g. a human in Obsidian) by rescanning now and then."""
        if self._tx is None and (force or time.monotonic() - self._scanned_at > RESCAN_SECONDS):
            self.scan()

    def _register(self, rel: str, text: str) -> None:
        self._unregister(rel)
        try:
            doc = md.parse(text)
            entry = Entry(rel, doc.meta, md.find_links(doc.body) + md.meta_links(doc.meta))
        except md.FormatError as e:
            entry = Entry(rel, error=str(e))
        self._entries[rel] = entry
        stem = PurePosixPath(rel).stem
        if stem not in RESERVED_STEMS:
            self._names.setdefault(stem.lower(), set()).add(rel)

    def _unregister(self, rel: str) -> None:
        if self._entries.pop(rel, None) is not None:
            stem = PurePosixPath(rel).stem.lower()
            paths = self._names.get(stem)
            if paths:
                paths.discard(rel)
                if not paths:
                    del self._names[stem]

    def entry(self, rel: str) -> Entry:
        e = self._entries.get(rel)
        if e is None:
            raise VaultError(f"'{rel}' is not a note")
        return e

    def entries(self) -> list[Entry]:
        self._fresh()
        return list(self._entries.values())

    # -------------------------------------------------------------- resolving

    def resolve_link(self, target: str) -> str | None:
        """Resolve a wikilink target (name or vault path) to a note path, or None if broken."""
        t = target.strip()
        if t.endswith(".md"):
            t = t[:-3]
        if t == "index":
            return INDEX
        if "/" in t:
            try:
                rel = self.norm(t)
            except VaultError:
                return None
            if self.abs(rel).is_dir():
                rel = self.index_of(rel)[:-3]
            return f"{rel}.md" if self.abs(f"{rel}.md").is_file() else None
        paths = self._names.get(t.lower())
        return sorted(paths)[0] if paths else None

    def resolve(self, ref: str) -> str:
        """Resolve a note reference given by a caller: a note name, a note path or a folder path."""
        ref = (ref or "").strip()
        if ref in ("", ".", "/"):
            return INDEX
        for attempt in range(2):
            found = self.resolve_link(ref)
            if found:
                return found
            with contextlib.suppress(VaultError):
                rel = self.norm(ref.rstrip("/"))
                if (self.abs(rel) / INDEX).is_file():
                    return self.index_of(rel)
            if attempt == 0:
                self._fresh(force=True)
        close = difflib.get_close_matches(PurePosixPath(ref).stem.lower(), list(self._names), n=3)
        hint = f"; did you mean: {', '.join(close)}?" if close else ""
        raise VaultError(f"no note or folder '{ref}'{hint}")

    def link_for(self, rel: str) -> str:
        """The wikilink text pointing at a note: [[name]] when the name is unique, else a path link."""
        p = PurePosixPath(rel)
        if p.name == INDEX:
            title = self._entries[rel].title if rel in self._entries else p.parent.name
            return "[[index]]" if rel == INDEX else f"[[{rel[:-3]}|{title}]]"
        if len(self._names.get(p.stem.lower(), ())) == 1:
            return f"[[{p.stem}]]"
        return f"[[{rel[:-3]}]]"

    # ------------------------------------------------------------ file access

    def read_text(self, rel: str) -> str:
        return self.abs(rel).read_text(encoding="utf-8")

    def load(self, rel: str) -> md.Document:
        try:
            return md.parse(self.read_text(rel))
        except md.FormatError as e:
            raise VaultError(f"{rel}: {e}") from e

    def _snapshot(self, rel: str) -> None:
        tx = self._tx
        if tx is not None and rel not in tx.snapshots:
            p = self.abs(rel)
            tx.snapshots[rel] = p.read_bytes() if p.exists() else None
        if tx is not None:
            tx.touch(rel)

    def _mkdirs(self, rel_dir: str) -> None:
        missing = []
        d = self.abs(rel_dir)
        while not d.exists():
            missing.append(d)
            d = d.parent
        for d in reversed(missing):
            d.mkdir()
            if self._tx is not None:
                self._tx.new_dirs.append(d.relative_to(self.root).as_posix())

    def _write_bytes(self, rel: str, data: bytes) -> None:
        self._snapshot(rel)
        self._mkdirs(self.parent(rel))
        p = self.abs(rel)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        if rel.endswith(".md") and PurePosixPath(rel).stem not in CONTENT_STEMS and not in_inbox(rel):
            self._register(rel, data.decode("utf-8"))

    def _write(self, rel: str, text: str) -> None:
        self._write_bytes(rel, text.encode("utf-8"))

    def _save(self, rel: str, doc: md.Document) -> None:
        self._write(rel, md.render(doc))

    def _copy_in(self, src: Path, rel: str) -> None:
        self._snapshot(rel)
        self._mkdirs(self.parent(rel))
        shutil.copy2(src, self.abs(rel))

    def _remove(self, rel: str) -> None:
        self._snapshot(rel)
        self.abs(rel).unlink()
        self._unregister(rel)

    # ----------------------------------------------------------- transactions

    @contextlib.contextmanager
    def _process_lock(self):
        if fcntl is None:
            yield
            return
        path = self.root / ".brain" / "cache" / "write.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def transaction(self, actor: str, message: str):
        """Group writes into one atomic, logged, committed change. Nested calls join the outer one."""
        with self._lock:
            if self._tx is not None:
                yield self._tx
                return
            with self._process_lock():
                self.scan()  # another process may have written while we waited
                tx = Transaction(actor, message)
                self._tx = tx
                try:
                    yield tx
                except BaseException:
                    self._rollback(tx)
                    raise
                finally:
                    self._tx = None
                self._finish(tx)

    def _rollback(self, tx: Transaction) -> None:
        for rel, data in reversed(list(tx.snapshots.items())):
            p = self.abs(rel)
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
        for rel in reversed(tx.new_dirs):
            with contextlib.suppress(OSError):
                self.abs(rel).rmdir()
        self.scan()

    def _finish(self, tx: Transaction) -> None:
        if not tx.changed:
            return
        now = dt.datetime.now().astimezone()
        log_rel = f".brain/log/{now:%Y-%m}.jsonl"
        log_path = self.root / log_rel
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": now.isoformat(timespec="seconds"), "actor": tx.actor,
                  "message": tx.message, "paths": tx.changed}
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            tx.committed = self.git.commit(tx.changed + [log_rel], f"{tx.actor}: {tx.message}", tx.actor)
        except Exception as e:  # files are written and logged; a git problem must not undo that
            tx.commit_error = str(e)

    def _actor(self) -> str:
        return self._tx.actor if self._tx else "agent:brain"

    # ---------------------------------------------------------------- indexes

    def _listing_folder(self, rel: str) -> str | None:
        """The folder whose index lists this note."""
        if rel == INDEX:
            return None
        folder = self.parent(rel)
        if PurePosixPath(rel).name == INDEX:
            return self.parent(folder)
        if self.is_leaf_folder(folder) and PurePosixPath(rel).stem == PurePosixPath(folder).name:
            return self.parent(folder)
        return folder

    def _children(self, folder: str) -> list[str]:
        """Index lines for a folder: subfolders first, then notes, each sorted by title."""
        folders, notes = [], []
        if folder == INBOX:
            return []

        def line(rel: str) -> tuple[str, str]:
            e = self._entries.get(rel) or Entry(rel)
            text = f"- {self.link_for(rel)}"
            if e.summary:
                text += f" — {e.summary}"
            if e.meta.get("status") == "outdated":
                text += " *(outdated)*"
            return e.title.lower(), text

        for child in sorted(self.abs(folder).iterdir()):
            if child.name.startswith("."):
                continue
            rel = child.relative_to(self.root).as_posix()
            if child.is_dir():
                if (child / INDEX).is_file():
                    folders.append(line(f"{rel}/{INDEX}"))
                elif (child / f"{child.name}.md").is_file():
                    notes.append(line(f"{rel}/{child.name}.md"))
            elif child.suffix == ".md" and child.name != INDEX and child.stem not in CONTENT_STEMS:
                notes.append(line(rel))
        if folder == ".":  # the fixed top level keeps its configured order
            order = {f"- [[{name}/index": i for i, name in enumerate(self.config.top_level)}
            folders = [(order.get(t.split("|")[0], 99), t) for _, t in folders]
        return [t for _, t in sorted(folders)] + [t for _, t in sorted(notes)]

    def refresh_index(self, folder: str) -> dict:
        """Regenerate a folder index's '## Children' section from its children's summaries."""
        folder = self.norm(folder)
        if PurePosixPath(folder).name == INDEX:
            folder = self.parent(folder)
        rel = self.index_of(folder)
        if not self.abs(rel).is_file():
            raise VaultError(f"folder '{folder}' has no index")
        doc = self.load(rel)
        lines = self._children(folder)
        body = md.replace_section(doc.body, "Children", "\n".join(lines))
        changed = body.strip() != doc.body.strip()
        if changed:
            doc.body = body
            doc.meta["updated"] = today()
            self._save(rel, doc)
        return {"index": rel, "entries": len(lines), "changed": changed,
                "oversized": len(lines) > self.config.limits.index_max_entries}

    def refresh_all(self) -> list[dict]:
        """Refresh every index, deepest first, so summaries propagate upward."""
        indexes = sorted((e.path for e in self.entries() if PurePosixPath(e.path).name == INDEX),
                         key=lambda p: p.count("/"), reverse=True)
        return [self.refresh_index(self.parent(p)) for p in indexes]

    def _after_change(self, rel: str) -> None:
        folder = self._listing_folder(rel)
        if folder is not None:
            self.refresh_index(folder)

    # ------------------------------------------------------------- validation

    def _check_summary(self, summary: str) -> None:
        if not summary or not summary.strip():
            raise VaultError("summary is required: one line telling a reader whether to open the note")
        if "\n" in summary.strip():
            raise VaultError("summary must be a single line")
        limit = self.config.limits.summary_max_words
        if md.word_count(summary) > limit:
            raise VaultError(f"summary has {md.word_count(summary)} words; the limit is {limit}")

    def _broken_links(self, text: str, meta: dict | None = None) -> list[str]:
        links = md.find_links(text) + (md.meta_links(meta) if meta else [])
        return sorted({lk.target for lk in links if self.resolve_link(lk.target) is None})

    def _as_links(self, items: list[str]) -> list[str]:
        out = []
        for item in items:
            item = item.strip()
            out.append(item if item.startswith("[[") else f"[[{item}]]")
        return out

    # ------------------------------------------------------------ operations

    def create_folder(self, path: str, title: str, summary: str, overview: str = "") -> str:
        rel = self.norm(path)
        parts = rel.split("/")
        if rel == "." or len(parts) == 1:
            raise VaultError("top-level folders are fixed; create the folder inside one of: "
                             + ", ".join(self.config.top_level))
        if parts[0] not in self.config.top_level:
            raise VaultError(f"'{parts[0]}' is not a top-level folder; use one of: "
                             + ", ".join(self.config.top_level))
        bad = [p for p in parts if not NAME_RE.match(p)]
        if bad:
            raise VaultError(f"folder name '{bad[0]}' must be lowercase kebab-case (a-z, 0-9, -)")
        parent = self.parent(rel)
        if not self.abs(self.index_of(parent)).is_file():
            raise VaultError(f"parent folder '{parent}' does not exist; create it first")
        if self.abs(rel).exists():
            raise VaultError(f"'{rel}' already exists")
        if not title.strip():
            raise VaultError("title is required")
        self._check_summary(summary)
        doc = index_document(title.strip(), summary.strip(), overview)
        doc.meta["author"] = self._actor()
        self._save(self.index_of(rel), doc)
        self.refresh_index(rel)
        self.refresh_index(parent)
        return self.index_of(rel)

    def write_note(self, path: str, title: str, summary: str, body: str, type: str | None = None,
                   tags: list[str] | None = None, related: list[str] | None = None,
                   sources: list[str] | None = None, status: str | None = None) -> tuple[str, list[str]]:
        """Create or replace a note. Returns (path, warnings)."""
        rel = self.norm(path)
        if not rel.endswith(".md"):
            rel += ".md"
        p = PurePosixPath(rel)
        folder = self.parent(rel)
        if folder == ".":
            raise VaultError("notes can't live at the vault root; use a folder inside one of: "
                             + ", ".join(self.config.top_level))
        if p.name == INDEX:
            raise VaultError("index notes are managed by create_folder, edit_note and refresh_index")
        if p.stem in RESERVED_STEMS:
            raise VaultError(f"'{p.stem}' is a reserved name")
        if not NAME_RE.match(p.stem):
            raise VaultError(f"note name '{p.stem}' must be lowercase kebab-case (a-z, 0-9, -)")
        is_folder_note = self.is_leaf_folder(folder) and p.stem == PurePosixPath(folder).name
        if not self.abs(self.index_of(folder)).is_file() and not is_folder_note:
            raise VaultError(f"folder '{folder}' does not exist; create it with create_folder first")
        taken = self._names.get(p.stem.lower(), set()) - {rel}
        if taken:
            raise VaultError(f"the name '{p.stem}' is already used by {sorted(taken)[0]}; "
                             "note names must be unique across the vault")
        if not title.strip():
            raise VaultError("title is required")
        self._check_summary(summary)
        if type == "index":
            raise VaultError("type 'index' is reserved for folder indexes")
        if status is not None and status not in STATUSES:
            raise VaultError(f"status must be one of: {', '.join(sorted(STATUSES))}")
        body = body.strip()
        if body.startswith("---"):
            raise VaultError("body must not contain frontmatter; pass metadata as arguments")

        if self.abs(rel).exists():
            old = self.load(rel)
            if old.meta.get("author") == "human":
                raise VaultError(f"{rel} is written by a human; you may only append to it (edit_note with append)")
            for block in md.human_blocks(old.body):
                if block not in body:
                    raise VaultError(f"{rel} contains a human-written block that must be kept verbatim:\n{block}")
            meta = dict(old.meta)
        else:
            meta = {"id": new_ulid(), "type": type or "note", "created": today(),
                    "author": self._actor(), "status": "current"}
        meta.update(title=title.strip(), summary=summary.strip(), updated=today())
        if type:
            meta["type"] = type
        if status:
            meta["status"] = status
        if tags is not None:
            meta["tags"] = [t.strip().lstrip("#") for t in tags if t.strip()]
        if related is not None:
            meta["related"] = self._as_links(related)
        if sources is not None:
            meta["sources"] = self._as_links(sources)
        if not body.startswith("# "):
            body = f"# {title.strip()}\n\n{body}"

        self._save(rel, md.Document(meta, body))
        self._after_change(rel)

        warnings = []
        words, limit = md.word_count(body), self.config.limits.note_max_words
        if words > limit:
            warnings.append(f"note has {words} words (limit {limit}); consider splitting it into a subfolder")
        broken = self._broken_links(body, meta)
        if broken:
            warnings.append(f"links to notes that don't exist yet: {', '.join(broken)}")
        return rel, warnings

    def edit_note(self, ref: str, find: str | None = None, replace: str | None = None,
                  append: str | None = None, title: str | None = None, summary: str | None = None,
                  tags: list[str] | None = None, status: str | None = None) -> str:
        """Targeted edit: replace one exact passage, append text and/or change metadata."""
        rel = self.resolve(ref)
        doc = self.load(rel)
        if doc.meta.get("author") == "human" and any(
                v is not None for v in (find, title, summary, tags, status)):
            raise VaultError(f"{rel} is written by a human; you may only append to it")
        if not any(v is not None for v in (find, append, title, summary, tags, status)):
            raise VaultError("nothing to change")

        if find is not None:
            if replace is None:
                raise VaultError("'replace' is required together with 'find'")
            count = doc.body.count(find)
            if count != 1:
                raise VaultError("text to find was not found" if count == 0 else
                                 f"text to find occurs {count} times; include more context so it is unique")
            start = doc.body.index(find)
            if md.overlaps_human_block(doc.body, start, start + len(find)):
                raise VaultError("that text is inside a human-written block, which agents may not change")
            doc.body = doc.body.replace(find, replace, 1)
        if append is not None and append.strip():
            if doc.meta.get("type") == "index":
                # Keep appended text out of the generated Children section.
                m = re.search(r"^## Children\s*$", doc.body, re.MULTILINE)
                if m:
                    doc.body = f"{doc.body[:m.start()].rstrip()}\n\n{append.strip()}\n\n{doc.body[m.start():]}"
                else:
                    doc.body = f"{doc.body.rstrip()}\n\n{append.strip()}\n"
            else:
                doc.body = f"{doc.body.rstrip()}\n\n{append.strip()}\n"
        if title is not None:
            if not title.strip():
                raise VaultError("title can't be empty")
            doc.meta["title"] = title.strip()
        if summary is not None:
            self._check_summary(summary)
            doc.meta["summary"] = summary.strip()
        if tags is not None:
            doc.meta["tags"] = [t.strip().lstrip("#") for t in tags if t.strip()]
        if status is not None:
            if status not in STATUSES:
                raise VaultError(f"status must be one of: {', '.join(sorted(STATUSES))}")
            doc.meta["status"] = status
        doc.meta["updated"] = today()
        self._save(rel, doc)
        if doc.meta.get("type") == "index":
            self.refresh_index(self.parent(rel))
        self._after_change(rel)
        return rel

    def link(self, source: str, target: str, kind: str = "related") -> tuple[str, bool]:
        """Add a typed link (a frontmatter list) from source to target. Returns (source path, added)."""
        if not re.match(r"^[a-z][a-z0-9_]*$", kind) or kind in PROTECTED_META:
            raise VaultError(f"'{kind}' can't be used as a link kind; use e.g. related, sources, supersedes")
        src, tgt = self.resolve(source), self.resolve(target)
        if src == tgt:
            raise VaultError("a note can't link to itself")
        doc = self.load(src)
        if doc.meta.get("author") == "human":
            raise VaultError(f"{src} is written by a human; you may only append to it")
        items = doc.meta.get(kind) or []
        if not isinstance(items, list):
            raise VaultError(f"frontmatter field '{kind}' in {src} is not a list")
        if any(self.resolve_link(lk.target) == tgt for lk in md.find_links(" ".join(map(str, items)))):
            return src, False
        doc.meta[kind] = items + [self.link_for(tgt)]
        doc.meta["updated"] = today()
        self._save(src, doc)
        return src, True

    def move_note(self, ref: str, folder: str) -> str:
        src = self.resolve(ref)
        p = PurePosixPath(src)
        if p.name == INDEX:
            raise VaultError("moving folders is not supported yet")
        if self.is_leaf_folder(self.parent(src)):
            raise VaultError("source and conversation folders can't be moved")
        dest_folder = self.norm(folder)
        if dest_folder == ".":
            raise VaultError("notes can't live at the vault root")
        if not self.abs(self.index_of(dest_folder)).is_file():
            raise VaultError(f"folder '{dest_folder}' does not exist; create it with create_folder first")
        dest = f"{dest_folder}/{p.name}"
        if dest == src:
            raise VaultError("note is already in that folder")
        if self.abs(dest).exists():
            raise VaultError(f"'{dest}' already exists")
        old_folder = self._listing_folder(src)
        self._write_bytes(dest, self.abs(src).read_bytes())
        self._remove(src)
        self.refresh_index(old_folder)
        self.refresh_index(dest_folder)
        return dest

    def mark_outdated(self, ref: str, superseded_by: str | None = None) -> str:
        rel = self.resolve(ref)
        doc = self.load(rel)
        if doc.meta.get("type") == "index":
            raise VaultError("folder indexes can't be marked outdated")
        newer = self.resolve(superseded_by) if superseded_by else None
        doc.meta["status"] = "outdated"
        doc.meta["updated"] = today()
        if newer:
            doc.meta["superseded_by"] = self.link_for(newer)
        self._save(rel, doc)
        if newer:
            self.link(newer, rel, "supersedes")
        self._after_change(rel)
        return rel

    def _ensure_folder(self, rel: str, title: str, summary: str) -> None:
        if not self.abs(self.index_of(rel)).is_file():
            self.create_folder(rel, title, summary)

    def add_source(self, file: str, title: str | None = None, summary: str | None = None,
                   import_roots: list[Path] | tuple = ()) -> dict:
        """Copy a file into sources/<year>/<month>/<slug>/, extract its text and write its folder note."""
        src = Path(file).expanduser()
        if not src.is_absolute():
            src = self.root / src
        src = src.resolve()
        allowed = [self.root / INBOX] + [Path(r).expanduser().resolve() for r in import_roots]
        if not any(src.is_relative_to(r) for r in allowed):
            raise VaultError("files can only be added from the vault's inbox/ or an allowed import root: "
                             + ", ".join(str(r) for r in allowed))
        if not src.is_file():
            raise VaultError(f"'{file}' is not a file")

        h = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        for e in self._entries.values():
            if e.meta.get("sha256") == digest:
                if src.is_relative_to(self.root / INBOX):
                    self._remove(src.relative_to(self.root).as_posix())
                return {"duplicate": True, "name": e.name, "path": e.path}

        now = dt.date.today()
        title = (title or src.stem.replace("_", " ").replace("-", " ")).strip()
        base = f"{now.isoformat()}-{md.slugify(title, 50)}"
        slug, n = base, 2
        while slug in self._names:
            slug, n = f"{base}-{n}", n + 1

        year, month = f"sources/{now:%Y}", f"sources/{now:%Y}/{now:%m}"
        self._ensure_folder(year, f"Sources {now:%Y}", f"Documents ingested in {now:%Y}, by month.")
        self._ensure_folder(month, f"Sources {now:%Y-%m}", f"Documents ingested in {now:%B %Y}.")
        folder = f"{month}/{slug}"
        ext = src.suffix.lower()
        self._copy_in(src, f"{folder}/original{ext}")
        text = extract_text(src)
        if text is not None:
            self._write(f"{folder}/extracted.md", text)

        if summary:
            self._check_summary(summary)
        meta = {
            "id": new_ulid(), "type": "source", "title": title,
            "summary": summary.strip() if summary else f"Not yet summarized: {src.name}",
            "status": "current" if summary else "draft",
            "created": today(), "updated": today(), "author": self._actor(),
            "original": f"original{ext}", "original_name": src.name, "sha256": digest,
            "mime": guess_mime(src), "size_bytes": src.stat().st_size,
            "extraction": "done" if text is not None else "pending", "notes": [],
        }
        body = f"# {title}\n\n" + ("" if summary else "Summary pending.\n")
        note = f"{folder}/{slug}.md"
        self._save(note, md.Document(meta, body))
        self._after_change(note)
        if src.is_relative_to(self.root / INBOX):
            self._remove(src.relative_to(self.root).as_posix())
        return {"duplicate": False, "name": slug, "path": note, "extraction": meta["extraction"],
                "chars": len(text) if text is not None else 0}

    # ------------------------------------------------------------------ reads

    def read_source(self, ref: str, start: int = 0, max_chars: int = 16000) -> dict:
        rel = self.resolve(ref)
        e = self.entry(rel)
        if e.meta.get("type") not in ("source", "conversation"):
            raise VaultError(f"{rel} is not a source")
        folder = self.abs(self.parent(rel))
        content = next((folder / f"{s}.md" for s in ("extracted", "transcript")
                        if (folder / f"{s}.md").is_file()), None)
        if content is None:
            return {"path": rel, "total_chars": 0, "text": "",
                    "note": f"no extracted text (extraction: {e.meta.get('extraction', 'unknown')})"}
        text = content.read_text(encoding="utf-8")
        start = max(0, start)
        chunk = text[start:start + max(1, max_chars)]
        end = start + len(chunk)
        return {"path": rel, "total_chars": len(text), "start": start, "end": end,
                "next_start": end if end < len(text) else None, "text": chunk}

    def outgoing(self, rel: str) -> list[tuple[str, str | None]]:
        seen, out = set(), []
        for lk in self.entry(rel).links:
            if lk.target not in seen:
                seen.add(lk.target)
                out.append((lk.target, self.resolve_link(lk.target)))
        return out

    def backlinks(self, rel: str) -> list[str]:
        return sorted(e.path for e in self.entries() if e.path != rel
                      and any(self.resolve_link(lk.target) == rel for lk in e.links))

    def recent(self, n: int = 20) -> list[dict]:
        out = []
        for log in sorted((self.root / ".brain" / "log").glob("*.jsonl"), reverse=True):
            for line in reversed(log.read_text(encoding="utf-8").splitlines()):
                if line.strip():
                    out.append(json.loads(line))
                    if len(out) >= n:
                        return out
        return out

    def inbox(self) -> list[str]:
        base = self.root / INBOX
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*")
                      if p.is_file() and p.name != INDEX and not p.name.startswith("."))

    def conventions(self) -> str:
        path = self.root / ".brain" / "schema.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # ------------------------------------------------------------------ check

    def check(self) -> list[dict]:
        """Find convention violations. Returns a list of {kind, path, message}."""
        self._fresh(force=True)
        issues = []

        def issue(kind, path, message):
            issues.append({"kind": kind, "path": path, "message": message})

        for name, paths in self._names.items():
            if len(paths) > 1:
                issue("duplicate-name", sorted(paths)[0], f"name '{name}' used by: {', '.join(sorted(paths))}")
        limit = self.config.limits.summary_max_words
        for e in sorted(self._entries.values(), key=lambda e: e.path):
            if e.error:
                issue("invalid-frontmatter", e.path, e.error)
                continue
            missing = [f for f in REQUIRED_FIELDS if not e.meta.get(f)]
            if missing:
                issue("missing-fields", e.path, f"missing: {', '.join(missing)}")
            if e.summary and md.word_count(e.summary) > limit:
                issue("long-summary", e.path, f"summary has {md.word_count(e.summary)} words (limit {limit})")
            for target in sorted({lk.target for lk in e.links}):
                if self.resolve_link(target) is None:
                    issue("broken-link", e.path, f"[[{target}]] does not resolve")
            if self.parent(e.path) == "." and e.path != INDEX:
                issue("root-note", e.path, "notes belong in a top-level folder, not the vault root")
            if PurePosixPath(e.path).name == INDEX:
                folder = self.parent(e.path)
                expected = self._children(folder)
                current = md.get_section(md.parse(self.read_text(e.path)).body, "Children") or ""
                if [ln for ln in current.splitlines()[1:] if ln.strip()] != expected:
                    issue("stale-index", e.path, "Children section is out of date; run refresh_index")
                if len(expected) > self.config.limits.index_max_entries:
                    issue("oversized-index", e.path,
                          f"{len(expected)} entries (limit {self.config.limits.index_max_entries}); "
                          "group children into subfolders")
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel = Path(dirpath).relative_to(self.root).as_posix()
            if rel == INBOX:
                dirnames[:] = []
            if rel != "." and not (Path(dirpath) / INDEX).exists() and not self.is_leaf_folder(rel):
                issue("missing-index", rel, "folder has no index.md")
        for folder in self.config.top_level:
            if not self.abs(folder).is_dir():
                issue("missing-top-level", folder, "fixed top-level folder is missing")
        return issues
