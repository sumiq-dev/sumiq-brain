"""Brain tools: the operations agents use on a vault. Transport-independent, so the same
tools serve the MCP server now and the agent runtime later."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from . import markdown as md
from .search import search
from .vault import INDEX, Vault, VaultError


@dataclass
class Context:
    actor: str = "agent:unknown"
    import_roots: list[Path] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[Vault, dict, Context], str]
    writes: bool = False
    message: Callable[[dict], str] | None = None  # commit message for write tools


TOOLS: dict[str, Tool] = {}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


STR = {"type": "string"}
STR_LIST = {"type": "array", "items": {"type": "string"}}


def tool(name: str, description: str, props: dict, required: list[str] | None = None,
         writes: bool = False, message: Callable[[dict], str] | None = None):
    def register(fn):
        TOOLS[name] = Tool(name, description, _schema(props, required), fn, writes, message)
        return fn
    return register


def call(vault: Vault, name: str, args: dict, ctx: Context) -> str:
    """Run a tool. Raises VaultError for refused requests (the message says how to fix them)."""
    t = TOOLS.get(name)
    if t is None:
        raise VaultError(f"unknown tool '{name}'")
    missing = [k for k in t.schema["required"] if args.get(k) in (None, "")]
    if missing:
        raise VaultError(f"missing required argument(s): {', '.join(missing)}")
    unknown = set(args) - set(t.schema["properties"])
    if unknown:
        raise VaultError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    if not t.writes:
        return t.handler(vault, args, ctx)
    with vault.transaction(ctx.actor, t.message(args) if t.message else name) as tx:
        result = t.handler(vault, args, ctx)
    if tx.commit_error:
        result += f"\n(warning: saved, but the git commit failed: {tx.commit_error})"
    return result


def _with_warnings(text: str, warnings: list[str]) -> str:
    return text + "".join(f"\nwarning: {w}" for w in warnings)


# ---------------------------------------------------------------------- reading

@tool("conventions", "Return the vault's conventions for agents. Read this once before writing to the brain.", {})
def _conventions(vault, args, ctx):
    return vault.conventions() or "(no conventions file)"


@tool("read_index",
      "Read a folder's index: its overview plus one line per child (subfolders and notes) with a summary. "
      "Start navigation with path '.' (the root) and drill down by following the child links.",
      {"path": {**STR, "description": "Folder path relative to the vault root, e.g. '.', 'projects', "
                                       "'projects/kings-raid-clone'. Default '.'"}})
def _read_index(vault, args, ctx):
    rel = vault.resolve(args.get("path") or ".")
    if PurePosixPath(rel).name != INDEX:
        raise VaultError(f"'{args.get('path')}' is a note, not a folder; use read_note")
    return f"[{rel}]\n{vault.read_text(rel)}"


@tool("read_note",
      "Read a note by name (e.g. 'gacha-rates') or vault path. Optionally read only one section by its heading.",
      {"name": {**STR, "description": "Note name or vault-relative path"},
       "section": {**STR, "description": "Heading text of a single section to return"}},
      ["name"])
def _read_note(vault, args, ctx):
    rel = vault.resolve(args["name"])
    text = vault.read_text(rel)
    if args.get("section"):
        doc = vault.load(rel)
        part = md.get_section(doc.body, args["section"])
        if part is None:
            headings = [h.group(2) for h in md.HEADING_RE.finditer(doc.body)]
            raise VaultError(f"no section '{args['section']}' in {rel}; sections: {', '.join(headings) or 'none'}")
        return f"[{rel} § {args['section']}]\n{part}"
    return f"[{rel}]\n{text}"


@tool("read_source",
      "Read a slice of a source's extracted text (or a conversation transcript). Returns at most max_chars "
      "characters starting at 'start'; continue with the returned next_start.",
      {"name": {**STR, "description": "Source name, e.g. '2026-11-01-gdd-v3'"},
       "start": {"type": "integer", "description": "Character offset to start at (default 0)"},
       "max_chars": {"type": "integer", "description": "Maximum characters to return (default 16000)"}},
      ["name"])
def _read_source(vault, args, ctx):
    r = vault.read_source(args["name"], int(args.get("start") or 0), int(args.get("max_chars") or 16000))
    if not r.get("text"):
        return f"[{r['path']}] {r.get('note', 'empty')}"
    more = f"; continue with start={r['next_start']}" if r["next_start"] is not None else "; end of text"
    return f"[{r['path']} chars {r['start']}-{r['end']} of {r['total_chars']}{more}]\n{r['text']}"


@tool("follow_links",
      "List a note's links with summaries: 'out' = notes it links to, 'in' = notes linking to it (backlinks).",
      {"name": STR, "direction": {"type": "string", "enum": ["out", "in"], "description": "Default 'out'"}},
      ["name"])
def _follow_links(vault, args, ctx):
    rel = vault.resolve(args["name"])
    direction = args.get("direction") or "out"
    lines = []
    if direction == "in":
        for path in vault.backlinks(rel):
            e = vault.entry(path)
            lines.append(f"- {vault.link_for(path)} — {e.summary}")
    else:
        for target, path in vault.outgoing(rel):
            if path is None:
                lines.append(f"- [[{target}]] — (missing note)")
            else:
                e = vault.entry(path)
                lines.append(f"- {vault.link_for(path)} — {e.summary}")
    label = "linked from" if direction == "in" else "links to"
    return f"[{rel} {label}]\n" + ("\n".join(lines) or "(none)")


@tool("search",
      "Fallback full-text search over notes and source text. Prefer navigating indexes; use this when "
      "navigation dead-ends. Returns note names, summaries and snippets.",
      {"query": STR,
       "scope": {**STR, "description": "Folder to search in (default: whole vault)"},
       "limit": {"type": "integer", "description": "Maximum results (default 10)"}},
      ["query"])
def _search(vault, args, ctx):
    hits = search(vault, args["query"], args.get("scope") or ".", int(args.get("limit") or 10))
    if not hits:
        return "no results"
    out = []
    for h in hits:
        where = " (in source text)" if h.in_source_text else ""
        out.append(f"- {vault.link_for(h.path)} — {h.summary}{where}\n  {h.snippet}")
    return "\n".join(out)


@tool("recent", "List the most recent changes to the brain (who changed what, and when).",
      {"n": {"type": "integer", "description": "Number of entries (default 20)"}})
def _recent(vault, args, ctx):
    items = vault.recent(int(args.get("n") or 20))
    return "\n".join(f"- {r['ts']} {r['actor']}: {r['message']}" for r in items) or "(no changes yet)"


@tool("list_inbox", "List files waiting in the inbox to be ingested with add_source.", {})
def _list_inbox(vault, args, ctx):
    return "\n".join(f"- {p}" for p in vault.inbox()) or "(inbox is empty)"


@tool("check", "Check the vault for convention problems: broken links, stale or oversized indexes, "
               "duplicate names, missing fields.", {})
def _check(vault, args, ctx):
    issues = vault.check()
    return "\n".join(f"- [{i['kind']}] {i['path']}: {i['message']}" for i in issues) or "no problems found"


# ---------------------------------------------------------------------- writing

@tool("create_folder",
      "Create a subfolder with its index inside a top-level folder (top-level folders are fixed). "
      "Use when a subject has several notes, or to split an index that grew too large.",
      {"path": {**STR, "description": "e.g. 'projects/kings-raid-clone'"},
       "title": STR,
       "summary": {**STR, "description": "One line: what this folder holds"},
       "overview": {**STR, "description": "Optional short overview paragraph for the index"}},
      ["path", "title", "summary"], writes=True, message=lambda a: f"create folder {a['path']}")
def _create_folder(vault, args, ctx):
    rel = vault.create_folder(args["path"], args["title"], args["summary"], args.get("overview") or "")
    return f"created {rel}"


@tool("write_note",
      "Create a note, or replace an agent-written note completely. The parent folder must exist. "
      "Indexes are updated automatically. For small changes use edit_note instead.",
      {"path": {**STR, "description": "Vault path, e.g. 'projects/kings-raid-clone/gacha-rates.md'"},
       "title": STR,
       "summary": {**STR, "description": "One specific line (word limit applies); shown in the parent index"},
       "body": {**STR, "description": "Markdown body without frontmatter; key facts first, cite sources"},
       "type": {**STR, "description": "note (default), project, person, journal, topic, source, conversation"},
       "tags": STR_LIST,
       "related": {**STR_LIST, "description": "Names of related notes"},
       "sources": {**STR_LIST, "description": "Names of sources this note draws on"},
       "status": {"type": "string", "enum": ["current", "draft", "outdated"]}},
      ["path", "title", "summary", "body"], writes=True, message=lambda a: f"write {a['path']}")
def _write_note(vault, args, ctx):
    rel, warnings = vault.write_note(
        args["path"], args["title"], args["summary"], args["body"], args.get("type"),
        args.get("tags"), args.get("related"), args.get("sources"), args.get("status"))
    return _with_warnings(f"saved {rel}; folder index updated", warnings)


@tool("edit_note",
      "Make a targeted change to a note: replace one exact passage (find/replace), append text, "
      "and/or change title, summary, tags or status. Human-written notes only accept append.",
      {"name": STR,
       "find": {**STR, "description": "Exact text to replace; must occur exactly once"},
       "replace": STR,
       "append": {**STR, "description": "Text to add at the end (for indexes: above the Children list)"},
       "title": STR, "summary": STR, "tags": STR_LIST,
       "status": {"type": "string", "enum": ["current", "draft", "outdated"]}},
      ["name"], writes=True, message=lambda a: f"edit {a['name']}")
def _edit_note(vault, args, ctx):
    rel = vault.edit_note(args["name"], args.get("find"), args.get("replace"), args.get("append"),
                          args.get("title"), args.get("summary"), args.get("tags"), args.get("status"))
    return f"saved {rel}; folder index updated"


@tool("link",
      "Add a typed link from one note to another, stored in the source note's frontmatter.",
      {"source": STR, "target": STR,
       "kind": {**STR, "description": "related (default), sources, supersedes, or another lowercase name"}},
      ["source", "target"], writes=True,
      message=lambda a: f"link {a['source']} -> {a['target']} ({a.get('kind') or 'related'})")
def _link(vault, args, ctx):
    rel, added = vault.link(args["source"], args["target"], args.get("kind") or "related")
    return f"linked from {rel}" if added else f"{rel} already links there"


@tool("move_note", "Move a note to another folder. Links keep working because names are unique.",
      {"name": STR, "folder": STR}, ["name", "folder"], writes=True,
      message=lambda a: f"move {a['name']} to {a['folder']}")
def _move_note(vault, args, ctx):
    return f"moved to {vault.move_note(args['name'], args['folder'])}"


@tool("mark_outdated",
      "Mark a note as outdated instead of deleting it, optionally pointing to the note that replaces it.",
      {"name": STR, "superseded_by": {**STR, "description": "Name of the newer note"}},
      ["name"], writes=True, message=lambda a: f"mark {a['name']} outdated")
def _mark_outdated(vault, args, ctx):
    return f"marked {vault.mark_outdated(args['name'], args.get('superseded_by'))} outdated"


@tool("refresh_index",
      "Regenerate a folder index's Children list from its children's summaries. Use all=true to refresh "
      "every index in the vault (e.g. after a human edited files in Obsidian).",
      {"path": {**STR, "description": "Folder path (default '.')"},
       "all": {"type": "boolean"}},
      writes=True, message=lambda a: "refresh all indexes" if a.get("all") else f"refresh index {a.get('path') or '.'}")
def _refresh_index(vault, args, ctx):
    results = vault.refresh_all() if args.get("all") else [vault.refresh_index(args.get("path") or ".")]
    lines = []
    for r in results:
        if r["changed"] or r["oversized"] or not args.get("all"):
            flag = " — OVERSIZED, split into subfolders" if r["oversized"] else ""
            lines.append(f"- {r['index']}: {r['entries']} entries, {'updated' if r['changed'] else 'unchanged'}{flag}")
    return "\n".join(lines) or "all indexes are up to date"


@tool("add_source",
      "Add a file to the brain as a source: copies it into sources/<year>/<month>/<slug>/, extracts its text "
      "and creates its source note. Files must come from the inbox or an allowed import root. "
      "Duplicates (same content) are detected. Afterwards, read it and write a summary and notes.",
      {"file": {**STR, "description": "Path to the file, e.g. 'inbox/gdd-v3.pdf'"},
       "title": STR,
       "summary": {**STR, "description": "One line; leave out if you haven't read it yet"}},
      ["file"], writes=True, message=lambda a: f"add source {Path(a['file']).name}")
def _add_source(vault, args, ctx):
    r = vault.add_source(args["file"], args.get("title"), args.get("summary"), ctx.import_roots)
    if r["duplicate"]:
        return f"already in the brain as [[{r['name']}]] ({r['path']})"
    extraction = (f"extracted {r['chars']} characters" if r["extraction"] == "done"
                  else "no text extractor for this file type yet")
    return f"added [[{r['name']}]] at {r['path']}; {extraction}"
