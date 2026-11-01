import threading

import pytest

from aibrain import librarian as lib
from aibrain.engines import Result
from aibrain.librarian import MAX_ATTEMPTS, Librarian
from aibrain.settings import Settings
from aibrain.tools import Context, call

from conftest import git_log

SETTINGS = Settings.parse({"providers": {"p": {"kind": "anthropic"}}, "agents": {"default": {"provider": "p"}}})


def source_name(prompt: str) -> str:
    return prompt.split("[[")[1].split("]]")[0]


def good_agent(role, prompt, vault, settings, provider=None, model=None, import_roots=None):
    """Behaves like a librarian: writes a note citing the source and summarizes the source."""
    assert role == "librarian"
    name = source_name(prompt)
    ctx = Context(actor="agent:librarian")
    text = call(vault, "read_source", {"name": name}, ctx)
    call(vault, "write_note", {"path": "topics/pity.md", "title": "Pity", "summary": "Pity rules.",
                               "body": f"{text.splitlines()[-1]} ([[{name}]])", "sources": [name]}, ctx)
    call(vault, "edit_note", {"name": name, "summary": "Design notes about pity.", "status": "current",
                              "find": "Summary pending.", "replace": "Filed into [[pity]]."}, ctx)
    return Result("filed it")


def lazy_agent(role, prompt, vault, settings, provider=None, model=None, import_roots=None):
    return Result("I did nothing", "max_turns")


@pytest.fixture
def librarian(vault):
    logs = []
    lib_ = Librarian(vault, SETTINGS, log=logs.append)
    lib_.logs = logs
    return lib_


def drop(vault, name, content):
    path = vault.root / "inbox" / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return f"inbox/{name}"


def test_files_inbox_document(vault, librarian, monkeypatch):
    monkeypatch.setattr(lib, "run_agent", good_agent)
    drop(vault, "notes.md", "# Notes\n\nHard pity at 90.")
    [outcome] = librarian.run_once()
    assert outcome.status == "filed"
    meta = vault.load(vault.resolve(outcome.source)).meta
    assert meta["status"] == "current" and meta["summary"] == "Design notes about pity."
    assert meta["notes"] == ["[[pity]]"] and meta["ingest_attempts"] == 1
    assert vault.inbox() == []
    assert vault.check() == []
    authors = {line.split("|")[0] for line in git_log(vault)}
    assert "agent:librarian" in authors


def test_incomplete_filing_is_retried_then_given_up(vault, librarian, monkeypatch):
    monkeypatch.setattr(lib, "run_agent", lazy_agent)
    drop(vault, "notes.md", "# Notes\n\nSomething.")
    [first] = librarian.run_once()
    assert first.status == "incomplete"
    assert librarian.pending_sources() == [first.source]
    for _ in range(MAX_ATTEMPTS - 1):
        assert [o.status for o in librarian.run_once()] == ["incomplete"]
    assert librarian.pending_sources() == []  # attempts used up: left as a draft for a human
    assert librarian.run_once() == []

    monkeypatch.setattr(lib, "run_agent", good_agent)
    vault.set_meta(first.source, ingest_attempts=0)  # a human resets it
    assert [o.status for o in librarian.run_once()] == ["filed"]


def test_file_without_text_is_stored_without_agent(vault, librarian, monkeypatch):
    monkeypatch.setattr(lib, "run_agent", lambda *a, **k: pytest.fail("agent must not run"))
    drop(vault, "photo.png", b"\x89PNG\r\n\x1a\n")
    [outcome] = librarian.run_once()
    assert outcome.status == "no-text"
    meta = vault.load(vault.resolve(outcome.source)).meta
    assert meta["status"] == "current" and "no text could be extracted" in meta["summary"]


def test_duplicates_are_skipped(vault, librarian, monkeypatch):
    monkeypatch.setattr(lib, "run_agent", good_agent)
    drop(vault, "a.md", "# Same\n\nText.")
    librarian.run_once()
    drop(vault, "b.md", "# Same\n\nText.")
    [outcome] = librarian.run_once()
    assert outcome.status == "duplicate" and vault.inbox() == []


def test_failed_files_are_parked(vault, librarian, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk on fire")
    monkeypatch.setattr(vault, "add_source", broken)
    drop(vault, "x.md", "x")
    [outcome] = librarian.run_once()
    assert outcome.status == "failed"
    failed = vault.root / "inbox" / "failed"
    assert (failed / "x.md").exists() and "disk on fire" in (failed / "x.md.error.txt").read_text()
    assert vault.inbox() == []  # parked files are not picked up again


def test_agent_errors_dont_stop_the_run(vault, librarian, monkeypatch):
    def crash(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(lib, "run_agent", crash)
    drop(vault, "a.md", "# A\n\none")
    drop(vault, "b.md", "# B\n\ntwo")
    assert [o.status for o in librarian.run_once()] == ["failed", "failed"]
    assert len(librarian.pending_sources()) == 2  # both will be retried


def test_watch_waits_for_files_to_settle(vault, librarian, monkeypatch):
    monkeypatch.setattr(lib, "run_agent", good_agent)
    drop(vault, "a.md", "# A\n\nHard pity at 90.")
    assert librarian.run_once(wait_for_stable=True) == []  # first sighting: maybe still copying
    assert [o.status for o in librarian.run_once(wait_for_stable=True)] == ["filed"]


def test_watch_thread_stops(vault, librarian):
    stop = threading.Event()
    t = threading.Thread(target=librarian.watch, args=(0.05, stop))
    t.start()
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_librarian_role_cannot_add_sources():
    from aibrain.agents import ROLES
    assert "add_source" not in ROLES["librarian"].tools and "write_note" in ROLES["librarian"].tools


def test_provider_outage_uses_no_attempts(vault, librarian, monkeypatch):
    from aibrain.engines import EngineError

    def not_signed_in(*a, **k):
        raise EngineError("Claude Code is not signed in")
    monkeypatch.setattr(lib, "run_agent", not_signed_in)
    drop(vault, "a.md", "# A\n\none")
    drop(vault, "b.md", "# B\n\ntwo")
    outcomes = librarian.run_once()
    assert [o.status for o in outcomes] == ["failed", "queued"]  # both added, filing waits
    assert librarian.provider_error == "Claude Code is not signed in"
    assert vault.inbox() == []
    for _ in range(5):
        librarian.run_once()
    assert len(librarian.pending_sources()) == 2  # still drafts, no attempts used up

    monkeypatch.setattr(lib, "run_agent", good_agent)
    assert {o.status for o in librarian.run_once()} == {"filed"}
    assert librarian.pending_sources() == [] and vault.inbox() == []
