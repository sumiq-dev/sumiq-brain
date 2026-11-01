# AI Brain

A portable second brain that AI agents build and maintain themselves. Agents file documents into an
Obsidian-compatible vault of markdown notes and folder indexes, then find things by navigating those
indexes. No vector search needed, and context stays small no matter how big the brain gets.

Design: [docs/DESIGN.md](docs/DESIGN.md)

## Run it with Docker (anywhere)

```bash
git clone git@github.com:AK6601/ai_brain.git && cd ai_brain
cp .env.example .env                 # set BRAIN_TOKEN (openssl rand -hex 32), PUID/PGID (id -u / id -g)
mkdir -p data                        # create it yourself so it's owned by you, not root
docker compose up -d
```

This starts one container that:
- creates the brain in `./data/vault` on first start (plain files: open it in Obsidian, back it up, move it),
- writes default settings to `./data/config/config.yaml`,
- serves the brain over MCP at `http://<host>:8765/mcp` (bearer token required).

Moving the brain to another machine means copying the repo's `data/` folder (or just `data/vault`).

### Connect Claude Code (or any MCP client)

```bash
claude mcp add --transport http brain http://<host>:8765/mcp --header "Authorization: Bearer <BRAIN_TOKEN>"
```

Put the brain behind HTTPS (e.g. a reverse proxy) before exposing it to the internet.

## Models: subscription, API or open-weight

Agents get their model from `data/config/config.yaml`. Four provider kinds, mixable per agent:

| Kind | What you pay with | How |
| --- | --- | --- |
| `claude_code` | Claude subscription (Pro/Max) | Official Claude Code CLI, signed in with your account |
| `codex` | ChatGPT subscription (Plus/Pro) | Official Codex CLI, signed in with your account |
| `anthropic` | Anthropic API key | Direct API calls |
| `openai` | OpenAI API key, or nothing for open-weight | OpenAI API or any compatible server: Ollama, vLLM, llama.cpp, LM Studio, OpenRouter |

Sign in to a subscription once; the login is kept in `./data/home`:

```bash
docker compose exec -u brain brain claude                     # then /login
docker compose exec -u brain brain codex login --device-auth
```

For Claude without an interactive login, put a token from `claude setup-token` in `.env` as
`CLAUDE_CODE_OAUTH_TOKEN`. API keys go in `.env` too (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`).
For local models: `docker compose --profile local up -d` starts Ollama next to the brain.

Subscriptions are used only through the vendors' own unmodified CLIs, which is how Anthropic and OpenAI
permit personal use. They are for your own use: if other people use your instance, each of them needs
their own sign-in or API key.

### Feed it documents

Drop files into `data/vault/inbox/`. The librarian picks them up within seconds. It stores the
original, extracts the text, reads it, and files what matters into the right notes. It updates
existing notes rather than duplicating them, links people and projects, and keeps the indexes current.

| Supported now | Coming later |
| --- | --- |
| PDF (with text), Word (.docx), PowerPoint (.pptx), Excel (.xlsx), HTML, Markdown, text, code | OCR for scans, audio/video transcripts, images |

Files without extractable text are stored and listed but not read yet. Files that can't be added at
all move to `inbox/failed/` with an `.error.txt` explaining why. If the model provider is unavailable
(for example, not signed in yet), files are still stored, and filing resumes once it works again.
Without Docker: `brain ingest` (once) or `brain watch` (keep going).

### Try it

```bash
docker compose exec -u brain brain brain ask "What projects do I have?"
docker compose exec -u brain brain brain run "File everything in the inbox" --provider anthropic
```

`ask` uses a read-only agent; `run` uses an agent that may change the brain. `--provider` and `--model`
override the settings for one call.

## Harness: chat with agents (work in progress, local)

Agents that use the brain as memory and, if their recipe allows, work on files, run commands and
use the web. Every agent has full access to the brain.

```bash
brain chat "What's the hard pity in my game?"                 # standard agent
brain chat "And who's our art lead?" --thread <id>            # continue a conversation
brain chat "Fix the failing test" --recipe coder              # use an agent recipe
brain harness                                                 # HTTP API for the web UI (127.0.0.1:8780)
brain recipe-example > ~/.config/ai-brain/recipes/coder.yaml  # create a recipe
```

A recipe is a saved setup: instructions, model, which tools (files/shell/web), which project folders.
File tools can't leave the recipe's folders or read secrets. Writes and shell commands need your
approval, except commands you put on the recipe's allowlist. Shell commands run as your user, so
approve them with care. Conversations are stored in `~/.local/share/ai-brain/harness/`, separately
from the brain. The chat model comes from the `chat` agent entry in the settings (or `default`).

## Without Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[providers]"
brain init ~/my-brain
brain serve ~/my-brain                         # MCP on stdio for a local client
BRAIN_TOKEN=... brain serve-http ~/my-brain    # MCP over HTTP
brain config-template > ~/.config/ai-brain/config.yaml
brain ask "..." --vault ~/my-brain
brain check ~/my-brain                         # broken links, stale indexes, …
brain refresh ~/my-brain                       # regenerate indexes after editing in Obsidian
```

## Tools

| Read | Write |
| --- | --- |
| `conventions`, `read_index`, `read_note`, `read_source`, `follow_links`, `search`, `recent`, `list_inbox`, `check` | `create_folder`, `write_note`, `edit_note`, `link`, `move_note`, `mark_outdated`, `refresh_index`, `add_source` |

Every write is atomic (rolled back on error), logged to `.brain/log/` and committed to the vault's git
repository under the agent's name.

## Tests

```bash
.venv/bin/pip install -e ".[dev]" && .venv/bin/pytest
```
