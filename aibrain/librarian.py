"""The librarian: turns files dropped into the inbox into filed, linked, indexed knowledge.

Code does what must be reliable (dedup, extraction, file moves, bookkeeping, retries); the agent
does what needs judgment (reading, deciding where things belong, writing and linking notes).
Interrupted work resumes: a source whose summary is still a draft gets filed again on the next
run, up to a few attempts.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .agents import run_agent
from .engines import EngineError
from .settings import Settings, SettingsError
from .vault import FAILED, INBOX, Vault

ACTOR = "agent:librarian"
MAX_ATTEMPTS = 3


@dataclass
class Outcome:
    file: str
    status: str          # filed | queued | duplicate | no-text | incomplete | failed
    source: str | None = None
    detail: str = ""


def _log(msg: str) -> None:
    print(f"[librarian] {msg}", file=sys.stderr, flush=True)


class Librarian:
    def __init__(self, vault: Vault, settings: Settings, provider: str | None = None,
                 model: str | None = None, import_roots: list[Path] | None = None,
                 log: Callable[[str], None] = _log):
        self.vault = vault
        self.settings = settings
        self.provider = provider
        self.model = model
        self.import_roots = import_roots or []
        self.log = log
        self._seen: dict[str, tuple[int, float]] = {}  # inbox file -> (size, mtime) at last poll
        self.provider_error: str | None = None  # set when the model provider can't be used at all

    # ------------------------------------------------------------------ queue

    def stable_inbox_files(self) -> list[str]:
        """Inbox files whose size and mtime didn't change since the last poll (fully copied)."""
        ready, current = [], {}
        for rel in self.vault.inbox():
            st = (self.vault.root / rel).stat()
            current[rel] = (st.st_size, st.st_mtime)
            if self._seen.get(rel) == current[rel]:
                ready.append(rel)
        self._seen = current
        return ready

    def pending_sources(self) -> list[str]:
        """Sources added but not filed yet (draft summary, text available, attempts left)."""
        return sorted(e.name for e in self.vault.entries()
                      if e.meta.get("type") == "source" and e.meta.get("status") == "draft"
                      and e.meta.get("extraction") == "done"
                      and int(e.meta.get("ingest_attempts") or 0) < MAX_ATTEMPTS)

    # ------------------------------------------------------------------- steps

    def ingest_file(self, rel: str) -> Outcome:
        """Add one inbox file as a source, then file it."""
        name = PurePosixPath(rel).name
        try:
            with self.vault.lock, self.vault.transaction(ACTOR, f"add source {name}"):
                r = self.vault.add_source(rel, import_roots=self.import_roots)
        except Exception as e:
            self._park_failed(rel, f"could not add as a source: {e}")
            return Outcome(rel, "failed", detail=str(e))
        if r["duplicate"]:
            self.log(f"{name}: already in the brain as [[{r['name']}]]")
            return Outcome(rel, "duplicate", r["name"])
        if r["extraction"] != "done":
            with self.vault.lock, self.vault.transaction(ACTOR, f"record {r['name']} without text"):
                self.vault.set_meta(r["name"], status="current",
                                    summary=f"{name}: stored, but no text could be extracted yet")
            self.log(f"{name}: stored as [[{r['name']}]] without text (no extractor for this file yet)")
            return Outcome(rel, "no-text", r["name"])
        if self.provider_error:  # added and extracted; filing waits until the provider works
            return Outcome(rel, "queued", r["name"])
        return self.file_source(r["name"], rel)

    def file_source(self, name: str, file: str | None = None) -> Outcome:
        """Have the librarian agent read a source and file its content into the brain."""
        file = file or name
        with self.vault.lock:
            meta = self.vault.entry(self.vault.resolve(name)).meta
            chars = self.vault.read_source(name, max_chars=1)["total_chars"]
        attempts = int(meta.get("ingest_attempts") or 0) + 1
        self.log(f"filing [[{name}]] ({chars} characters, attempt {attempts}/{MAX_ATTEMPTS})")

        prompt = (f"A new source was added to the brain: [[{name}]] — \"{meta.get('title')}\" "
                  f"(original file: {meta.get('original_name')}, {chars} characters of text). "
                  "File it following your procedure.")
        try:
            result = run_agent("librarian", prompt, self.vault, self.settings, self.provider,
                               self.model, self.import_roots)
        except (EngineError, SettingsError) as e:
            # The provider is unusable (not signed in, no key, unreachable): not this document's
            # fault, so no attempt is used up. The source stays a draft and is retried later.
            self.provider_error = str(e)
            self.log(f"[[{name}]]: model provider unavailable: {e}")
            return Outcome(file, "failed", name, str(e))
        except Exception as e:
            self._count_attempt(name, attempts)
            self.log(f"[[{name}]]: agent failed: {e}")
            return Outcome(file, "failed", name, str(e))
        self._count_attempt(name, attempts)

        with self.vault.lock, self.vault.transaction(ACTOR, f"link notes to {name}"):
            notes = self.vault.sync_source_notes(name)
            done = self.vault.entry(self.vault.resolve(name)).meta.get("status") != "draft"
        if not done:
            self.log(f"[[{name}]]: agent finished without summarizing the source ({result.status}); will retry")
            return Outcome(file, "incomplete", name, result.text)
        self.log(f"filed [[{name}]] into {len(notes)} note(s): {', '.join(notes) or '-'}")
        return Outcome(file, "filed", name, result.text)

    def _count_attempt(self, name: str, attempts: int) -> None:
        with self.vault.lock, self.vault.transaction(ACTOR, f"filing attempt {attempts} for {name}"):
            self.vault.set_meta(name, ingest_attempts=attempts)

    def _park_failed(self, rel: str, reason: str) -> None:
        src = self.vault.root / rel
        dest_dir = self.vault.root / INBOX / FAILED
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        n = 2
        while dest.exists():
            dest = dest_dir / f"{src.stem}-{n}{src.suffix}"
            n += 1
        if src.exists():
            shutil.move(str(src), dest)
        dest.with_name(dest.name + ".error.txt").write_text(reason + "\n", encoding="utf-8")
        self.log(f"{src.name}: moved to {INBOX}/{FAILED}/ ({reason})")

    # -------------------------------------------------------------------- runs

    def run_once(self, wait_for_stable: bool = False) -> list[Outcome]:
        """Process everything waiting: inbox files first, then unfinished sources."""
        self.provider_error = None
        files = self.stable_inbox_files() if wait_for_stable else self.vault.inbox()
        outcomes = [self.ingest_file(rel) for rel in files]
        handled = {o.source for o in outcomes}
        for name in self.pending_sources():
            if name not in handled:
                outcomes.append(self.file_source(name))
                if self.provider_error:
                    break
        return outcomes

    def watch(self, interval: float = 10.0, stop: threading.Event | None = None) -> None:
        """Poll the inbox forever (or until `stop` is set). Errors are logged, never fatal."""
        stop = stop or threading.Event()
        self.log(f"watching {self.vault.root / INBOX} every {interval:g}s")
        backoff = interval
        while not stop.is_set():
            try:
                self.run_once(wait_for_stable=True)
            except Exception:
                self.log("error during ingestion:\n" + traceback.format_exc())
            if self.provider_error:  # wait longer and longer (up to 10 minutes) until it's fixed
                backoff = min(max(backoff * 2, 60.0), 600.0)
                self.log(f"pausing {backoff:g}s until the model provider works again")
            else:
                backoff = interval
            stop.wait(backoff)


def start_watcher(vault: Vault, settings: Settings, interval: float,
                  import_roots: list[Path] | None = None) -> threading.Event:
    """Run the librarian in a background thread; returns the event that stops it."""
    stop = threading.Event()
    librarian = Librarian(vault, settings, import_roots=import_roots)
    threading.Thread(target=librarian.watch, args=(interval, stop), daemon=True, name="librarian").start()
    return stop
