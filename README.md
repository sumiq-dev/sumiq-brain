# AI Brain

A portable second brain that AI agents build and maintain themselves. Agents file documents into an
Obsidian-compatible vault of markdown notes and folder indexes, then find things by navigating those
indexes. No vector search needed, and context stays small no matter how big the brain gets.

Design: [docs/DESIGN.md](docs/DESIGN.md)

## Status

Stage 1: vault store, agent tools, MCP server. Librarian, navigator and gardener agents come next.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Usage

```bash
brain init ~/my-brain            # create a vault (fixed folders, git repo)
brain check ~/my-brain           # report broken links, stale indexes, …
brain refresh ~/my-brain         # regenerate all indexes (e.g. after editing in Obsidian)
brain serve ~/my-brain           # MCP server on stdio
```

### Use it from Claude Code

```bash
claude mcp add brain -- /path/to/ai_brain/.venv/bin/brain serve ~/my-brain
```

Drop files into `~/my-brain/inbox/` and ask Claude to ingest them. To let agents import files from
elsewhere, add `--import-root ~/Documents` (repeatable) to `serve`.

## Tools

| Read | Write |
| --- | --- |
| `conventions`, `read_index`, `read_note`, `read_source`, `follow_links`, `search`, `recent`, `list_inbox`, `check` | `create_folder`, `write_note`, `edit_note`, `link`, `move_note`, `mark_outdated`, `refresh_index`, `add_source` |

Every write is atomic (rolled back on error), logged to `.brain/log/` and committed to the vault's git
repository under the agent's name.

## Tests

```bash
.venv/bin/pytest
```
