"""Terminal client for the harness: `brain chat "..."` streams a reply and asks for approvals."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..settings import Settings
from ..vault import Vault
from .recipes import load_recipes
from .runtime import Runtime
from .store import Store


def harness_dir() -> Path:
    env = os.environ.get("BRAIN_HARNESS_DIR")
    return Path(env) if env else Path.home() / ".local" / "share" / "ai-brain" / "harness"


def recipes_dir(settings: Settings) -> Path:
    env = os.environ.get("BRAIN_RECIPES")
    if env:
        return Path(env)
    return (settings.path.parent if settings.path else Settings.default_path().parent) / "recipes"


def runtime_for(vault: Vault, settings: Settings) -> Runtime:
    data = harness_dir()
    return Runtime(vault, settings, load_recipes(recipes_dir(settings)), Store(data / "harness.db"), data,
                   searxng_url=(settings.extras.get("web") or {}).get("searxng_url"))


def _short(args: dict, limit: int = 120) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def run_chat(runtime: Runtime, message: str, recipe: str, thread_id: str | None, yes: bool) -> int:
    if thread_id is None:
        thread_id = runtime.new_thread(recipe)["id"]
    run = runtime.start(thread_id, message)
    after, status, at_line_start = 0, "error", True
    interactive = sys.stdin.isatty()
    while True:
        for e in run.wait(after):
            after = e["seq"] + 1
            kind = e["type"]
            if kind == "text":
                sys.stdout.write(e["delta"])
                sys.stdout.flush()
                at_line_start = e["delta"].endswith("\n")
                continue
            if not at_line_start:
                sys.stdout.write("\n")
                at_line_start = True
            if kind == "tool_call":
                print(f"  → {e['name']} {_short(e.get('args') or {})}", file=sys.stderr)
            elif kind == "tool_result" and e.get("is_error"):
                print(f"  ✗ {e['output'][:200]}", file=sys.stderr)
            elif kind == "approval_request":
                print(f"\n  ? {e['reason']}", file=sys.stderr)
                if yes:
                    approved = True
                elif interactive:
                    approved = input("  Allow? [y/N] ").strip().lower() in ("y", "yes")
                else:
                    approved = False
                    print("  (denied: not an interactive terminal; use --yes to approve everything)", file=sys.stderr)
                runtime.broker.resolve(e["approval_id"], approved)
            elif kind == "error":
                print(f"error: {e['message']}", file=sys.stderr)
            elif kind == "done":
                status = e["status"]
                u = e.get("usage") or {}
                cost = f", ${u['cost_usd']:.3f} list price" if "cost_usd" in u else ""
                print(f"\n[{status} · {e.get('model') or '?'} · in {u.get('input_tokens', 0) + u.get('cache_read_tokens', 0) + u.get('cache_write_tokens', 0)}"
                      f" / out {u.get('output_tokens', 0)} tokens{cost}]\n[continue with: --thread {thread_id}]",
                      file=sys.stderr)
        if run.done and after >= len(run.events):
            return 0 if status == "done" else 1
