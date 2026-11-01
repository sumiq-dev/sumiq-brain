"""Per-conversation options the user can change in the chat window: provider and model, thinking
effort, and access (web/files/shell, plus read-only / ask / full-auto)."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import replace

from ..settings import Provider, Settings
from .recipes import Recipe

CHAT_KINDS = {"anthropic", "openai", "claude_code", "codex"}  # kinds with a chat engine
ACCESS_MODES = ("read", "ask", "auto")
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
OPENAI_EFFORTS = ["low", "medium", "high"]
CLAUDE_MODELS = [
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
    ("claude-fable-5-1", "Fable 5.1"),
]
CLAUDE_ALIASES = {"opus": "Opus (latest)", "sonnet": "Sonnet (latest)", "haiku": "Haiku (latest)"}
KIND_LABELS = {"anthropic": "Claude API", "openai": "OpenAI-compatible", "claude_code": "Claude subscription",
               "codex": "ChatGPT subscription"}

_model_cache: dict[str, tuple[float, list[str], str | None]] = {}
_codex_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def _codex_models(cli: str) -> list[dict]:
    """The models this Codex account can use, from the CLI's own catalog (cached for 5 minutes)."""
    with _cache_lock:
        hit = _codex_cache.get(cli)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
    models = []
    try:
        out = subprocess.run([cli, "debug", "models"], capture_output=True, text=True, timeout=30).stdout
        for m in json.loads(out).get("models", []):
            if m.get("visibility") != "list":
                continue
            efforts = [lvl["effort"] for lvl in m.get("supported_reasoning_levels", [])
                       if lvl.get("effort") in CLAUDE_EFFORTS]  # "ultra" delegates to sub-agents: left out
            models.append({"id": m["slug"], "label": m.get("display_name") or m["slug"], "efforts": efforts})
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        pass
    with _cache_lock:
        _codex_cache[cli] = (time.monotonic(), models)
    return models


class OptionsError(ValueError):
    pass


def _list_openai_models(provider: Provider) -> tuple[list[str], str | None]:
    """Ask an OpenAI-compatible server which models it has (cached for a minute)."""
    base = (provider.options.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    with _cache_lock:
        hit = _model_cache.get(provider.name)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1], hit[2]
    headers = {}
    key = provider.api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/models", headers=headers), timeout=3) as r:
            models, error = sorted(m["id"] for m in json.loads(r.read()).get("data", [])), None
    except Exception as e:
        models, error = [], f"can't reach {base}: {getattr(e, 'reason', e)}"
    with _cache_lock:
        _model_cache[provider.name] = (time.monotonic(), models, error)
    return models, error


def provider_info(p: Provider, configured_model: str | None = None, account: dict | None = None) -> dict:
    """What the model picker shows for one provider."""
    chat = p.kind in CHAT_KINDS
    available, reason = chat, None if chat else "not available in chat yet"
    models: list[dict] = []
    efforts: list[str] = []
    listed = p.options.get("models")
    if p.kind in ("anthropic", "claude_code"):
        efforts = CLAUDE_EFFORTS
        models = [{"id": i, "label": label} for i, label in CLAUDE_MODELS]
        if p.kind == "claude_code":
            models = [{"id": a, "label": label} for a, label in CLAUDE_ALIASES.items()] + models
            if not (p.options.get("command") or shutil.which("claude")):
                available, reason = False, "Claude Code CLI not installed"
            elif account is not None and not account.get("signed_in"):
                available, reason = False, "not signed in: open Accounts"
        elif not p.api_key():
            available, reason = False, f"needs {p.options.get('api_key_env') or 'an API key'}"
    elif p.kind == "openai":
        if p.options.get("api_key_env") and not p.api_key():
            available, reason = False, f"needs {p.options['api_key_env']}"
        else:
            ids, error = (list(listed), None) if listed else _list_openai_models(p)
            models = [{"id": i, "label": i} for i in ids]
            if error and not ids:
                available, reason = False, error
        if not p.options.get("base_url"):
            efforts = OPENAI_EFFORTS  # reasoning effort on OpenAI's own reasoning models
    elif p.kind == "codex":
        cli = p.options.get("command") or shutil.which("codex")
        if not cli:
            available, reason = False, "Codex CLI not installed"
        else:
            if account is not None and not account.get("signed_in"):
                available, reason = False, "not signed in: open Accounts"
            models = _codex_models(cli)
            efforts = sorted({e for m in models for e in m.get("efforts", [])}, key=CLAUDE_EFFORTS.index)
    if listed and p.kind != "openai":
        models = [{"id": i, "label": i} for i in listed]
    if configured_model and configured_model not in {m["id"] for m in models}:
        models.insert(0, {"id": configured_model, "label": configured_model})
    return {"name": p.name, "kind": p.kind, "label": f"{KIND_LABELS.get(p.kind, p.kind)} · {p.name}",
            "chat": chat, "available": available, "reason": reason, "models": models, "efforts": efforts}


def defaults(recipe: Recipe, settings: Settings) -> dict:
    """Options for a new conversation with this recipe."""
    spec = settings.agent("chat", recipe.provider, recipe.model)
    return {"provider": spec.provider.name, "model": spec.model, "effort": recipe.effort or spec.effort,
            "web": recipe.web, "files": recipe.files, "shell": recipe.shell,
            "access": "ask" if recipe.approve_writes else "auto"}


def effective(recipe: Recipe, settings: Settings, stored: dict | None) -> dict:
    base, stored = defaults(recipe, settings), stored or {}
    opts = {**base, **{k: v for k, v in stored.items() if v is not None or k in ("model", "effort")}}
    if opts["provider"] != base["provider"] and not stored.get("model"):
        opts["model"] = None  # the default model belongs to the default provider; use the new one's
    if not recipe.workspaces:  # no project folders: nothing to read or run
        opts["files"] = opts["shell"] = False
    return opts


def validate(update: dict, recipe: Recipe, settings: Settings) -> dict:
    """Check a partial options update from the UI; returns the cleaned update."""
    allowed = {"provider", "model", "effort", "web", "files", "shell", "access"}
    unknown = set(update) - allowed
    if unknown:
        raise OptionsError(f"unknown option(s): {', '.join(sorted(unknown))}")
    clean = dict(update)
    if "provider" in clean:
        p = settings.providers.get(clean["provider"])
        if p is None:
            raise OptionsError(f"unknown provider '{clean['provider']}'")
        if p.kind not in CHAT_KINDS:
            raise OptionsError(f"provider '{p.name}' can't be used for chat yet")
    if "access" in clean and clean["access"] not in ACCESS_MODES:
        raise OptionsError(f"access must be one of {', '.join(ACCESS_MODES)}")
    if clean.get("effort") not in (None, *CLAUDE_EFFORTS):
        raise OptionsError(f"thinking must be one of {', '.join(CLAUDE_EFFORTS)} (or empty for the default)")
    for flag in ("web", "files", "shell"):
        if flag in clean:
            clean[flag] = bool(clean[flag])
            if flag in ("files", "shell") and clean[flag] and not recipe.workspaces:
                raise OptionsError(f"{flag} needs project folders: add workspaces to the recipe '{recipe.id}'")
    return clean


def apply(recipe: Recipe, opts: dict) -> Recipe:
    """The recipe as this conversation's options shape it."""
    return replace(recipe, web=opts["web"], files=opts["files"], shell=opts["shell"],
                   approve_writes=opts["access"] != "auto")


def capabilities(opts: dict) -> str:
    """Plain-language description of what the agent may do, for telling it when access changes."""
    parts = []
    if opts["access"] == "read":
        parts.append("You are in read-only mode: you may read the brain and files but must not change "
                     "anything (no writing notes, files, or running commands).")
    else:
        parts.append("You may read and write the brain.")
        if opts["files"]:
            parts.append("You may read and edit files in the workspace folders"
                         + (" (each write needs the user's approval)." if opts["access"] == "ask" else "."))
        if opts["shell"]:
            parts.append("You may run shell commands" + (" (each needs approval)." if opts["access"] == "ask" else "."))
    if not opts["files"]:
        parts.append("You cannot access the user's files.")
    if not opts["shell"]:
        parts.append("You cannot run commands.")
    parts.append("You can use the web." if opts["web"] else "You cannot use the web.")
    return " ".join(parts)
