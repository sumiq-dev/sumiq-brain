"""Command line for AI Brain. Defaults come from env vars so the same commands work in Docker."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .scaffold import init_vault
from .settings import TEMPLATE, Settings, SettingsError
from .tools import TOOLS, Context, call
from .vault import Vault, VaultError


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _env_roots() -> list[Path]:
    return [Path(p) for p in os.environ.get("BRAIN_IMPORT_ROOTS", "").split(os.pathsep) if p]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain", description="AI Brain: a portable, agent-maintained second brain")
    sub = parser.add_subparsers(dest="command", required=True)
    default_vault = _env_path("BRAIN_VAULT")

    def vault_arg(p):
        p.add_argument("vault", type=Path, nargs="?" if default_vault else None, default=default_vault,
                       help="vault folder (default: $BRAIN_VAULT)")

    p = sub.add_parser("init", help="create a new vault")
    p.add_argument("path", type=Path)
    p.add_argument("--name", help="brain name (default: folder name)")
    p.add_argument("--no-git", action="store_true", help="don't create a git repository")

    p = sub.add_parser("serve", help="run the MCP server on stdio (for a local client)")
    vault_arg(p)
    p.add_argument("--actor", help="name recorded for changes (default: derived from the MCP client)")
    p.add_argument("--import-root", action="append", type=Path, default=[],
                   help="extra folder add_source may read files from (repeatable)")
    p.add_argument("--tools", help="comma-separated subset of tools to expose (default: all)")

    p = sub.add_parser("serve-http", help="run the MCP server over HTTP (for remote clients and Docker)")
    vault_arg(p)
    p.add_argument("--host", default=os.environ.get("BRAIN_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("BRAIN_PORT", "8765")))
    p.add_argument("--import-root", action="append", type=Path, default=[])
    p.add_argument("--watch", action="store_true", default=os.environ.get("BRAIN_WATCH", "").lower() in ("1", "true", "yes"),
                   help="also run the librarian on the inbox (default: $BRAIN_WATCH)")
    p.add_argument("--interval", type=float, default=float(os.environ.get("BRAIN_WATCH_INTERVAL", "10")))
    p.add_argument("--config", type=Path, help="settings file (default: $BRAIN_CONFIG)")
    p.add_argument("--harness", action="store_true", default=os.environ.get("BRAIN_HARNESS", "").lower() in ("1", "true", "yes"),
                   help="also serve the harness API and web UI (default: $BRAIN_HARNESS)")
    p.add_argument("--harness-host", default=os.environ.get("BRAIN_HARNESS_HOST", "127.0.0.1"))
    p.add_argument("--harness-port", type=int, default=int(os.environ.get("BRAIN_HARNESS_PORT", "8780")))

    for name, role, desc in (("ask", "navigator", "answer a question from the brain (read-only agent)"),
                             ("run", "worker", "give an agent a task that may change the brain")):
        p = sub.add_parser(name, help=desc)
        p.add_argument("prompt")
        p.add_argument("--vault", type=Path, default=default_vault, required=default_vault is None)
        p.add_argument("--config", type=Path, help="settings file (default: $BRAIN_CONFIG)")
        p.add_argument("--provider", help="override the configured provider")
        p.add_argument("--model", help="override the configured model")
        p.add_argument("--import-root", action="append", type=Path, default=[])
        p.set_defaults(role=role)

    for name, desc in (("ingest", "file everything waiting in the inbox, then stop"),
                       ("watch", "keep filing new inbox files as they arrive")):
        p = sub.add_parser(name, help=desc)
        p.add_argument("--vault", type=Path, default=default_vault, required=default_vault is None)
        p.add_argument("--config", type=Path, help="settings file (default: $BRAIN_CONFIG)")
        p.add_argument("--provider", help="override the configured provider")
        p.add_argument("--model", help="override the configured model")
        p.add_argument("--import-root", action="append", type=Path, default=[])
        if name == "watch":
            p.add_argument("--interval", type=float, default=float(os.environ.get("BRAIN_WATCH_INTERVAL", "10")))

    p = sub.add_parser("chat", help="chat with an agent in the terminal (keeps the conversation)")
    p.add_argument("message")
    p.add_argument("--vault", type=Path, default=default_vault, required=default_vault is None)
    p.add_argument("--config", type=Path, help="settings file (default: $BRAIN_CONFIG)")
    p.add_argument("--recipe", default="standard", help="agent recipe for a new conversation")
    p.add_argument("--thread", help="continue this conversation (id)")
    p.add_argument("--yes", action="store_true", help="approve every action without asking (careful)")

    p = sub.add_parser("harness", help="run the harness API for the web UI")
    p.add_argument("--vault", type=Path, default=default_vault, required=default_vault is None)
    p.add_argument("--config", type=Path, help="settings file (default: $BRAIN_CONFIG)")
    p.add_argument("--host", default=os.environ.get("BRAIN_HARNESS_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("BRAIN_HARNESS_PORT", "8780")))

    sub.add_parser("recipe-example", help="print an example agent recipe")

    sub.add_parser("config-template", help="print a starter settings file")

    p = sub.add_parser("check", help="report convention problems")
    vault_arg(p)

    p = sub.add_parser("refresh", help="regenerate all indexes")
    vault_arg(p)

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            for notice in init_vault(args.path, args.name, git=not args.no_git):
                print(f"note: {notice}", file=sys.stderr)
            print(f"created brain at {args.path.resolve()}")

        elif args.command == "serve":
            from .mcp_server import McpServer
            tools = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else None
            unknown = set(tools or []) - set(TOOLS)
            if unknown:
                raise VaultError(f"unknown tool(s): {', '.join(sorted(unknown))}")
            ctx = Context(actor=args.actor or "agent:mcp", import_roots=args.import_root + _env_roots())
            McpServer(Vault(args.vault), ctx, actor_fixed=bool(args.actor), tools=tools).serve()

        elif args.command == "serve-http":
            from .http_server import serve_http
            vault, roots = Vault(args.vault), args.import_root + _env_roots()
            if args.watch:
                from .librarian import start_watcher
                try:
                    start_watcher(vault, Settings.load(args.config), args.interval, roots)
                except SettingsError as e:
                    print(f"warning: inbox watcher not started: {e}", file=sys.stderr)
            if not args.harness:
                serve_http(vault, args.host, args.port, os.environ.get("BRAIN_TOKEN") or None, roots)
                return 0
            # One process, one Vault object: the MCP server runs in a thread, the harness in front.
            import threading
            from .harness.cli import runtime_for
            from .harness.server import serve
            runtime = runtime_for(vault, Settings.load(args.config))
            threading.Thread(target=serve_http, daemon=True, name="mcp",
                             args=(vault, args.host, args.port, os.environ.get("BRAIN_TOKEN") or None, roots)).start()
            serve(runtime, args.harness_host, args.harness_port)

        elif args.command in ("ask", "run"):
            from .agents import run_agent
            from .engines import EngineError
            settings = Settings.load(args.config)
            try:
                result = run_agent(args.role, args.prompt, Vault(args.vault), settings, args.provider,
                                   args.model, args.import_root + _env_roots())
            except EngineError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            print(result.text)
            usage = ", ".join(f"{k}={v}" for k, v in result.usage.items())
            print(f"\n[{result.status}; turns={result.turns}; tools={len(result.tool_calls)}"
                  + (f"; {usage}" if usage else "") + "]", file=sys.stderr)
            return 0 if result.status == "done" else 1

        elif args.command in ("ingest", "watch"):
            from .librarian import Librarian
            librarian = Librarian(Vault(args.vault), Settings.load(args.config), args.provider, args.model,
                                  args.import_root + _env_roots())
            if args.command == "watch":
                librarian.watch(args.interval)
                return 0
            outcomes = librarian.run_once()
            for o in outcomes:
                print(f"{o.status:<10} {o.file}" + (f" -> [[{o.source}]]" if o.source else ""))
            if not outcomes:
                print("nothing to file")
            return 1 if any(o.status in ("failed", "incomplete") for o in outcomes) else 0

        elif args.command in ("chat", "harness"):
            from .harness.cli import run_chat, runtime_for
            runtime = runtime_for(Vault(args.vault), Settings.load(args.config))
            if args.command == "harness":
                from .harness.server import serve
                serve(runtime, args.host, args.port)
                return 0
            return run_chat(runtime, args.message, args.recipe, args.thread, args.yes)

        elif args.command == "recipe-example":
            from .harness.recipes import EXAMPLE
            print(EXAMPLE, end="")

        elif args.command == "config-template":
            print(TEMPLATE, end="")

        elif args.command == "check":
            issues = Vault(args.vault).check()
            for i in issues:
                print(f"[{i['kind']}] {i['path']}: {i['message']}")
            print(f"{len(issues)} problem(s)" if issues else "no problems found")
            return 1 if issues else 0

        elif args.command == "refresh":
            print(call(Vault(args.vault), "refresh_index", {"all": True}, Context(actor="human:cli")))

    except (VaultError, SettingsError, FileNotFoundError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
