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
            serve_http(Vault(args.vault), args.host, args.port, os.environ.get("BRAIN_TOKEN") or None,
                       args.import_root + _env_roots())

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
