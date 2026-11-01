# AI Brain — Design

Status: draft · 2026-11-01

## 1. Vision

AI Brain is a second brain that AI agents build and maintain themselves. You hand it documents. Agents read them, write notes, link them, and keep a tree of index notes up to date. Later, any agent can find what it needs by walking that tree, the way a person browses a well-kept Obsidian vault. It does not load the whole brain, and it does not rely on vector search.

The brain can grow to 100+ GB of sources while an agent only ever holds a few thousand tokens of it in context.

### Goals

- Agents ingest, file, summarize and link documents without manual work.
- Agents find information by navigating indexes and links. Search is only a fallback.
- The brain is **portable**: one folder, plain files, usable without this software.
- Agents and LLM providers can be swapped (Claude API, local models).
- A human can read and edit everything in Obsidian.

### Non-goals (for now)

- Not a RAG system. There is no embedding store as the primary retrieval path.
- No multi-user permissions or sharing.
- No real-time collaboration. One writer at a time is fine.
- The agent chat platform/UI comes later (see Roadmap).

## 2. Principles

1. **The vault is the source of truth.** Everything the brain knows is in markdown and source files inside one folder.
2. **Portable by construction.** You can copy, zip, sync or git-clone the folder to another machine, and it works. No external database. No absolute paths. Anything derived (caches, search indexes) lives in `.brain/cache/` and can be rebuilt from the files.
3. **Readable without the platform.** Opened in Obsidian or a text editor, the vault makes full sense on its own.
4. **Summaries all the way up.** Each level of the tree holds one-line summaries of the level below. Navigation costs O(depth), not O(size).
5. **Agents write, humans can override.** A human edit always wins. Agents never delete human-written content without a review step.
6. **Everything is versioned.** Every agent change is a git commit inside the vault, so it can be audited and undone.
7. **Documents are data, never instructions.** Content from sources can't change agent behavior (see §9).

## 3. Core concepts

| Concept | What it is |
| --- | --- |
| **Vault** | The brain folder. Portable unit. |
| **Source** | An original file (PDF, docx, md, image, audio, code) plus its extracted text and metadata. Never modified. |
| **Note** | A markdown file written by an agent or human: summary, key facts, decisions, links. Notes are about something: a source, project, person, topic or event. |
| **Index note** | A note that lists its children with one-line summaries. Every folder has one (`index.md`). Equivalent to an Obsidian "Map of Content". |
| **Link** | An Obsidian wikilink `[[note-name]]` between notes. Typed links go in frontmatter. |
| **Root index** | `index.md` at the vault root. Where every navigation starts. |

### Why navigation scales

With ≤ 50 entries per index, depth 4 reaches 50⁴ ≈ 6 million notes. Each step reads one index of about 2–4k tokens. So answering a question typically costs 4–6 reads of 2–4k tokens each, plus the final notes. That stays the same whether the vault holds 1 GB or 1 TB.

## 4. Vault layout

```
my-brain/                        ← the portable unit
  index.md                       ← root index: top-level areas
  projects/
    index.md
    kings-raid-clone/
      index.md                   ← overview, status, key decisions, children
      combat-system.md
      gacha-rates.md
  areas/                         ← ongoing responsibilities (health, finance, …)
  topics/                        ← knowledge not tied to a project
  people/
  journal/                       ← dated events, meetings, decisions
    2026/
      2026-11-01.md
  sources/
    index.md
    2026/11/
      2026-11-01-gdd-v3/
        original.pdf             ← untouched original
        extracted.md             ← extracted text (OCR/transcript), kept so it's never recomputed
        2026-11-01-gdd-v3.md     ← folder note: metadata + agent summary; links to the notes it fed
  conversations/                 ← agent chat transcripts (see §8.4)
    index.md
    2026/11/
      2026-11-01-design-ai-brain/
        transcript.md            ← cleaned transcript (tool noise stripped)
        2026-11-01-design-ai-brain.md  ← folder note: metadata + summary
  inbox/                         ← drop files here to be ingested
  .brain/
    brain.yaml                   ← vault config (conventions, limits); no secrets
    schema.md                    ← conventions for agents (note format, filing rules)
    log/                         ← append-only agent activity log (jsonl, per month)
    cache/                       ← derived, rebuildable, gitignored (search index, etc.)
  .git/                          ← version history (optional but default)
  .gitignore
```

Rules:

- **Top-level folders are fixed**: `projects/`, `areas/`, `topics/`, `people/`, `journal/`, `sources/`, `conversations/`, `inbox/`. They're defined in `brain.yaml`, and agents can't create, rename or remove top-level folders. Agents organize freely *inside* them. If the structure doesn't work in practice, a human changes `brain.yaml` and the gardener migrates.
- **Filenames are unique across the vault** (kebab-case slugs). Obsidian wikilinks resolve by name, so links survive moving files between folders.
- **Each folder has exactly one `index.md`.**
- **Sources are folders**, so the original, extracted text and metadata travel together. The folder note is named after the folder (`<slug>.md`), so `[[slug]]` links resolve. `index`, `original`, `extracted` and `transcript` are reserved names and are never notes.
- **Large binaries**: the vault's git repo tracks `sources/**/original.*` with **git-LFS**, so history stays small while the vault stays complete and portable. Markdown (notes, indexes, `extracted.md`, transcripts) stays in plain git. This applies only to vaults, not to the AI Brain code repository.
- **Machine-specific settings** (API keys, model endpoints) live **outside** the vault, in the platform config. `brain.yaml` holds only vault-level settings.

## 5. File formats

### 5.1 Note

```markdown
---
id: 01JBX7K3Q8...            # ULID, stable across renames
type: note                   # note | index | source | person | project | journal
title: Gacha rates
summary: Pull rates, pity system and banner rules for the gacha; decided 2026-10-12.
tags: [game-design, monetization]
sources: ["[[2026-11-01-gdd-v3]]"]
related: ["[[combat-system]]", "[[economy]]"]
supersedes: []               # notes this one replaces
status: current              # current | outdated | draft
created: 2026-11-01
updated: 2026-11-01
author: agent:librarian      # or human
---

# Gacha rates

Short, factual body. Key facts and decisions first, with dates.
Every claim that came from a source cites it: ([[2026-11-01-gdd-v3]] p. 12).
```

- `summary` is a single line of ≤ 25 words. That's what parent indexes show.
- Target note size: ≤ 1,500 words. Longer notes get split into child notes.

### 5.2 Index note

```markdown
---
id: 01JBX7...
type: index
title: Kings Raid clone
summary: Gacha RPG project; combat, gacha, economy, art pipeline.
updated: 2026-11-01
---

# Kings Raid clone

<!-- human-written overview: agents never overwrite this block -->
Mobile gacha RPG inspired by King's Raid. Currently in vertical-slice phase.
<!-- /human -->

## Children
- [[combat-system]] — Real-time auto-battle with manual skill timing; damage formulas.
- [[gacha-rates]] — Pull rates, pity system, banner rules; decided 2026-10-12.
- [[art-pipeline/index|Art pipeline]] — Character model workflow with TitanForge.

## See also
- [[economy]] — Currency sinks and sources shared with other projects.
```

- One line per child: `[[link]] — summary`. Agents regenerate `## Children` from the children's `summary` fields, so indexes never drift.
- **Limit: 50 entries or ~3k tokens.** When an index goes over, the gardener groups its children into subfolders, each with its own index.
- Text between `<!-- human -->` markers is never touched by agents.

### 5.3 Source folder note (`<slug>.md`)

```markdown
---
id: 01JBX8...
type: source
title: Game design document v3
summary: Full GDD v3 for the Kings Raid clone; covers combat, gacha, economy.
original: original.pdf
sha256: 9f2c...
mime: application/pdf
size_bytes: 4812331
ingested: 2026-11-01
extractor: docling 2.x
notes: ["[[gacha-rates]]", "[[combat-system]]"]
---

Agent summary of the source (≤ 300 words) and a table of contents with page refs.
```

The `sha256` is used for deduplication. The same file dropped twice is detected and linked, not re-ingested.

## 6. Architecture

```
                    ┌──────────────────────────────────────────┐
  Claude Code ───┐  │               AI Brain service           │
  Your platform ─┼─▶│  MCP server / HTTP API (tool layer)      │
  CLI ───────────┘  │        │                                 │
                    │        ▼                                 │
                    │  Agent runtime                           │
                    │   ├─ Librarian  (ingest + file)          │
                    │   ├─ Navigator  (answer questions)       │
                    │   └─ Gardener   (maintenance, scheduled) │
                    │        │                                 │
                    │        ▼                                 │
                    │  LLM provider layer (Claude / Ollama /…) │
                    │        │                                 │
                    │  Job queue (single writer per vault)     │
                    │        │                                 │
                    │  Vault store ── extractors ── git        │
                    └────────┼─────────────────────────────────┘
                             ▼
                      my-brain/  (plain files — portable)
```

| Component | Responsibility | Tech (proposed) |
| --- | --- | --- |
| Vault store | Read/write notes, parse frontmatter, resolve wikilinks, rebuild indexes, enforce conventions | Python, `python-frontmatter`, `ruamel.yaml` |
| Extractors | File → text, no LLM | Own parsers for docx/pptx/xlsx/HTML (stdlib), pypdf for PDF; OCR and audio later |
| Fallback search | Full-text search when navigation fails | ripgrep over markdown; optional SQLite FTS5 in `.brain/cache/` |
| Tool layer | Exposes vault tools to agents and external clients | Own MCP server: stdio + Streamable HTTP with bearer auth (no SDK) |
| Agent runtime | Runs agent loops with tools, budgets, logging | Own thin loop over the provider layer |
| LLM provider layer | Engines per provider kind, per-agent model choice | See §9 |
| Job queue | Serializes writes, retries, resumable ingestion | SQLite-backed queue stored outside the vault |
| Versioning | Commits each agent transaction | git CLI / `pygit2` |

The service is stateless apart from its job queue. Point it at any vault folder and it works.

## 7. Tools

These tools are used by internal agents and exposed over MCP. All paths are vault-relative.

### Read

| Tool | Purpose |
| --- | --- |
| `read_index(path=".")` | Return an index note (children + summaries). Entry point for navigation. |
| `read_note(name, section=None)` | Return a note by wikilink name, optionally a single section. |
| `read_source(name, pages=None, max_tokens=4000)` | Return a slice of a source's extracted text. Never the whole thing by default. |
| `follow_links(name, direction="out"\|"in")` | List outgoing links/backlinks with summaries. |
| `search(query, scope=None, limit=10)` | Fallback full-text search; returns names + summaries + snippets. |
| `recent(n=20)` | Recently changed notes (from the log). |

### Write

| Tool | Purpose |
| --- | --- |
| `write_note(path, frontmatter, body)` | Create/replace an agent-owned note. Validates the format. |
| `edit_note(name, find, replace)` | Small targeted edit; preserves human blocks. |
| `link(from, to, kind="related")` | Add a typed link (frontmatter) and an inline mention if needed. |
| `move_note(name, new_folder)` | Move; links keep working because names are unique. |
| `mark_outdated(name, superseded_by)` | Set `status: outdated` and the `supersedes` chain. |
| `refresh_index(path)` | Regenerate `## Children` from child summaries. |
| `add_source(file)` | Copy a file into `sources/…`, extract text, write the source folder note. |

Every write call belongs to a **transaction**. The runtime commits once per transaction with a message like `librarian: file 2026-11-01-gdd-v3 (3 notes, 2 indexes)`.

## 8. Agents

### 8.1 Librarian — ingestion

Triggered when files land in `inbox/` or come in through `add_source`.

1. **Dedup:** hash the file. If already known, link and stop.
2. **Extract** text (no LLM) → `sources/<date>-<slug>/`.
3. **Triage** (cheap model): what is it, which areas does it belong to, is it worth notes or just a source entry?
4. **Navigate** from the root index to find where it belongs. Read only the relevant indexes.
5. **Write** or update notes: a new note per distinct subject, or an update to existing notes (with dated additions and source citations).
6. **Link** to related notes, people and projects.
7. **Refresh** the affected indexes up to the root.
8. **Commit** the transaction and append to the log.

For large sources, the librarian reads the source in chunks and maintains a running outline. It never puts the whole file in context.

**Implementation (Stage 2):** code handles the steps that must be reliable, and the agent handles the steps that need judgment.
- **Code:** inbox polling (it waits until a file's size and modification time stop changing), dedup, extraction, file moves, the source's `notes:` list (computed from backlinks), attempt counting and retries.
- **Agent:** reading, placement, writing and linking, and the source summary.

A source whose summary is still `draft` is filed again on the next run, up to 3 attempts. A provider outage (not signed in, no key, unreachable) uses up no attempts. Files are still added and extracted, filing waits, and the watcher backs off for up to 10 minutes. Files that can't be added at all are parked in `inbox/failed/` with an `.error.txt`.

**Budget:** each ingestion has a token budget set per file type and size. Bulk imports run as a resumable batch with a cost estimate up front ("12,400 files, ~X M tokens, ~$Y with Haiku").

### 8.2 Navigator — retrieval

Used by any agent that needs knowledge (including external clients via MCP).

1. Read the root index.
2. Choose the branch(es) by summary. Read those indexes, and repeat.
3. Read candidate notes, follow links if needed, and open sources for exact quotes.
4. If the path dead-ends, run `search` as a fallback, then **record the miss** so the gardener can fix the structure.
5. Answer with citations (`[[note]]`, source + page).

For broad questions ("what do I know about X across all projects"), it fans out sub-navigators per branch and merges their findings.

### 8.3 Gardener — maintenance

Runs on a schedule (e.g. nightly) or on demand.

- Split indexes over the limit. Merge tiny folders.
- Find duplicates (same subject, different notes) and propose or perform merges.
- Fix broken links and orphan notes (notes no index points to).
- Check for contradictions between notes. Mark outdated notes and chain them via `supersedes`.
- Use the navigator's miss log: when something was found only by search, add the link or index entry that would have found it.
- Re-sync `## Children` summaries.

Gardener changes that delete or merge content go into a reviewable commit or branch first (configurable).

### 8.4 Conversation memory

Agent conversations (in the platform, or Claude Code via MCP) can be saved into the brain, so what was discussed and decided is not lost.

1. When a conversation ends (or every N turns for long ones), the runtime writes a **cleaned transcript** to `conversations/<date>-<slug>/transcript.md`. It keeps user and assistant messages, collapses tool calls to one-line summaries, and redacts secrets (API keys, tokens, passwords) with pattern matching before anything is written.
2. The **librarian ingests it like a source**: it writes the folder note with a summary, then extracts decisions, facts, todos and preferences into the relevant notes, citing the conversation (`[[2026-11-01-design-ai-brain]]`).
3. A line is added to the day's `journal/` note: what was discussed, with a link.

Settings (in the platform config):

```yaml
conversations:
  save: ask            # always | ask | never — per agent overridable
  keep_transcript: true  # false = only distilled notes are kept, transcript discarded
  min_turns: 3         # skip trivial chats
```

Conversation transcripts fall under the same `local_only_paths` rules when they touch sensitive areas.

## 9. LLM providers

Every agent role gets its model from the platform settings. An **engine** runs one agent task on one kind of provider. There are two families:

- **API engines** (`anthropic`, `openai`). Our own tool-use loop calls the vendor API through the official SDK and runs brain tools in-process. `openai` also covers every OpenAI-compatible server, so open-weight models (Ollama, vLLM, llama.cpp, LM Studio) and routers like OpenRouter work through it.
- **CLI engines** (`claude_code`, `codex`). The official, unmodified vendor CLI runs the agent loop, signed in with your own subscription, and reaches the brain through our MCP server (`brain serve --tools …`). Built-in shell/file tools are disabled (Claude Code) or sandboxed read-only in an empty folder (Codex). The only way into the vault is the brain tools.

| Kind | Billing | Notes |
| --- | --- | --- |
| `claude_code` | Claude Pro/Max subscription (or API key) | Sign in via `claude` /login or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` |
| `codex` | ChatGPT Plus/Pro subscription (or API key) | Sign in via `codex login --device-auth` |
| `anthropic` | API key | Prompt caching on tools + system; server-side refusal fallback on models that support it |
| `openai` | API key, or none for local servers | Chat Completions with function tools |

**Why subscriptions go through the CLIs:** Anthropic allows subscription sign-in only in its own apps. Third-party software may not take or reuse subscription credentials. Running the unmodified Claude Code binary signed in with your own plan is explicitly allowed. Codex supports ChatGPT sign-in for `codex exec`. So the platform never touches subscription tokens. It starts the vendor's CLI, and the CLI authenticates itself. Subscriptions are for personal use. If other people use an instance, each needs their own sign-in or API key. Subscription usage limits assume ordinary individual use, so bulk ingestion should use API or local providers.

Platform settings live outside the vault: `$BRAIN_CONFIG`, default `~/.config/ai-brain/config.yaml`, in Docker `/config/config.yaml`. `brain config-template` prints a starter file.

```yaml
providers:
  claude-subscription: { kind: claude_code }
  chatgpt-subscription: { kind: codex }
  anthropic: { kind: anthropic, api_key_env: ANTHROPIC_API_KEY }
  local: { kind: openai, base_url: http://ollama:11434/v1 }

agents:
  default:   { provider: claude-subscription, model: opus }
  navigator: { provider: local, model: qwen3:32b }
  librarian: { provider: anthropic, model: claude-opus-5-5, effort: medium }

privacy:
  local_only_paths: ["areas/health/**", "areas/finance/**"]   # planned: never sent to cloud providers
```

Agent roles so far: `navigator` (read-only tools, answers questions), `worker` (all tools) and `librarian` (everything except `add_source`, files new sources). The gardener follows in Stage 4.

`local_only_paths` routes sensitive parts of the vault to local models only.

## 10. Safety, concurrency, privacy

- **Prompt injection:** source text and note bodies are passed to models inside clearly delimited data blocks. System prompts state that content is data, never instructions. Write tools validate paths and formats, and agents can't touch `.brain/` config or human blocks. Agents have no network or shell access unless explicitly granted.
- **Single writer:** all writes for a vault go through one queue. Reads are concurrent. If a human edits a file during a transaction, the runtime detects the changed hash, aborts that write, and retries with the new content.
- **Undo:** every transaction is one git commit, so `git revert` undoes any agent action.
- **Privacy:** cloud providers only see what is sent in a request. `local_only_paths` plus per-agent provider choice control what leaves the machine. API keys never live in the vault.

## 11. Deployment

- **One Docker image, any host** (x86_64 or arm64): `docker compose up -d`. The image contains the brain service, git/git-LFS and, optionally (`WITH_CLIS`), the Claude Code and Codex CLIs. An optional `local` profile adds Ollama.
- **Three mounted folders hold all state:** `data/vault` (the brain), `data/config` (platform settings) and `data/home` (CLI sign-ins). Moving the brain means moving `data/`. The container runs as an unprivileged user that matches the host user (`PUID`/`PGID`), so the files stay editable on the host and in Obsidian.
- **Remote access:** MCP over HTTP (Streamable HTTP transport) at `/mcp`, protected by a bearer token (`BRAIN_TOKEN`). Each client session is attributed separately in the vault's history. Put it behind HTTPS before exposing it to the internet. Local clients can still use stdio (`brain serve`).
- **Heavy work:** bulk extraction (OCR, transcription) and local models need real CPU/GPU, so for those, host the container on a capable machine.
- **Readers elsewhere:** the vault can be synced to other devices (Syncthing, git, Obsidian Sync) for reading in Obsidian. Only one brain service should write to a vault at a time.

## 12. Roadmap

| Stage | Deliverable | Done when |
| --- | --- | --- |
| 0 | This design + vault conventions (`schema.md`) | Conventions agreed |
| 1 | Vault store + tools + MCP server, no agents | Claude Code can navigate and write a hand-made vault through the tools |
| 2 | Librarian + extractors + inbox watcher | Dropping 50 mixed documents yields a sensible, linked, indexed vault |
| 3 | Navigator with fan-out + miss log + conversation saving (§8.4) | Answers to a test set of questions land on the right notes in ≤ 6 reads |
| 4 | Gardener | A messy vault converges: no oversized indexes, orphans or broken links |
| 5 | Bulk import with cost estimates and resume | 10k+ file import runs unattended and survives restarts |
| 6 | Agent platform (Next.js UI + Python backend) | Chat agents use the brain as their memory |

## 13. Decisions

| Date | Decision |
| --- | --- |
| 2026-11-01 | Top-level folders are fixed (§4). Revisit if the structure doesn't work in practice. |
| 2026-11-01 | Vaults use git + git-LFS for source originals (§4). The code repo is unaffected. |
| 2026-11-01 | Agent conversations can be saved into the brain and distilled into notes (§8.4). |
| 2026-11-01 | The brain must be portable: one folder, plain files, no external state (§2). |
| 2026-11-01 | LLM providers configurable per agent: Claude and OpenAI via subscription or API, plus open-weight models (§9). |
| 2026-11-01 | Subscriptions only through the official vendor CLIs, never by handling their tokens (§9). |
| 2026-11-01 | The whole platform ships as one Docker image that can be hosted anywhere (§11). |

## 14. Open questions

- **Images and video:** describe them with a vision model at ingest (costly), or store with filename and metadata only?
- **Multiple vaults:** one brain, or separate vaults (work/personal) with optional cross-links?
- **Human review:** which agent actions need approval before landing (merges, deletions, moves)?
