"""Command line: `brain init`, `brain serve`, `brain check`, `brain refresh`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .mcp_server import McpServer
from .scaffold import init_vault
from .tools import Context, call
from .vault import Vault, VaultError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain", description="AI Brain: a portable, agent-maintained second brain")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a new vault")
    p.add_argument("path", type=Path)
    p.add_argument("--name", help="brain name (default: folder name)")
    p.add_argument("--no-git", action="store_true", help="don't create a git repository")

    p = sub.add_parser("serve", help="run the MCP server on stdio")
    p.add_argument("vault", type=Path)
    p.add_argument("--actor", help="name recorded for changes (default: derived from the MCP client)")
    p.add_argument("--import-root", action="append", type=Path, default=[],
                   help="extra folder add_source may read files from (repeatable)")

    p = sub.add_parser("check", help="report convention problems")
    p.add_argument("vault", type=Path)

    p = sub.add_parser("refresh", help="regenerate all indexes")
    p.add_argument("vault", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            for notice in init_vault(args.path, args.name, git=not args.no_git):
                print(f"note: {notice}", file=sys.stderr)
            print(f"created brain at {args.path.resolve()}")
        elif args.command == "serve":
            vault = Vault(args.vault)
            ctx = Context(actor=args.actor or "agent:mcp", import_roots=args.import_root)
            McpServer(vault, ctx, actor_fixed=bool(args.actor)).serve()
        elif args.command == "check":
            issues = Vault(args.vault).check()
            for i in issues:
                print(f"[{i['kind']}] {i['path']}: {i['message']}")
            print(f"{len(issues)} problem(s)" if issues else "no problems found")
            return 1 if issues else 0
        elif args.command == "refresh":
            print(call(Vault(args.vault), "refresh_index", {"all": True}, Context(actor="human:cli")))
    except (VaultError, FileNotFoundError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
