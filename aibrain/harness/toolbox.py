"""Everything a chat agent can call: the brain (always, in full), plus files, shell and web as
its recipe allows. Risky calls wait for the user's approval. Every call is reported as events."""

from __future__ import annotations

import difflib
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import tools as brain_tools
from ..extractors import extract_text, html_to_text
from ..vault import Vault, VaultError
from .recipes import Recipe

MAX_RESULT_CHARS = 30_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             "dist", "build", ".next", ".cache"}
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".docker"}
SECRET_FILE_RE = re.compile(r"^(\.env(\..*)?|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|\.netrc|\.git-credentials)$")
SHELL_META_RE = re.compile(r"[;&|`$<>\n\\(){}]")
SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.I)

Emit = Callable[[dict], None]


class ToolError(Exception):
    """A refused or failed tool call; the message goes back to the model."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], str]
    approval: Callable[[dict], str | None] | None = None  # returns a reason when approval is needed


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n… [{len(text) - limit} characters omitted] …\n\n{text[-half:]}"


# ------------------------------------------------------------------------ approvals

class ApprovalBroker:
    """Holds tool calls until the user approves or denies them (from the UI or the terminal)."""

    def __init__(self):
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    def request(self, run_id: str, tool: str, args: dict, reason: str, emit: Emit,
                cancel: threading.Event | None = None, timeout: float = 900) -> bool:
        aid = uuid.uuid4().hex[:12]
        event = threading.Event()
        entry = {"id": aid, "run_id": run_id, "tool": tool, "args": args, "reason": reason,
                 "event": event, "approved": False}
        with self._lock:
            self._pending[aid] = entry
        emit({"type": "approval_request", "approval_id": aid, "tool": tool, "args": args, "reason": reason})
        waited = 0.0
        while not event.wait(0.5):
            waited += 0.5
            if waited >= timeout or (cancel is not None and cancel.is_set()):
                break
        with self._lock:
            self._pending.pop(aid, None)
        emit({"type": "approval_resolved", "approval_id": aid, "approved": entry["approved"]})
        return entry["approved"]

    def resolve(self, approval_id: str, approved: bool) -> bool:
        with self._lock:
            entry = self._pending.get(approval_id)
        if entry is None:
            return False
        entry["approved"] = bool(approved)
        entry["event"].set()
        return True

    def pending(self, run_id: str | None = None) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in e.items() if k != "event"} for e in self._pending.values()
                    if run_id is None or e["run_id"] == run_id]


# -------------------------------------------------------------------------- toolbox

class Toolbox:
    def __init__(self, vault: Vault, recipe: Recipe, actor: str, emit: Emit, broker: ApprovalBroker,
                 run_id: str = "", cancel: threading.Event | None = None, searxng_url: str | None = None,
                 import_roots: list[Path] | None = None, read_only: bool = False, auto_approve: bool = False):
        self.vault = vault
        self.recipe = recipe
        self.emit = emit
        self.broker = broker
        self.run_id = run_id
        self.cancel = cancel
        self.searxng_url = searxng_url
        self.read_only = read_only        # no tool that changes anything (brain, files, shell)
        self.auto_approve = auto_approve  # "full auto": nothing waits for the user
        self.ctx = brain_tools.Context(actor=actor, import_roots=list(import_roots or []) + recipe.workspaces)
        self.tools: dict[str, Tool] = {}
        for t in brain_tools.TOOLS.values():
            if not (read_only and t.writes):
                self.tools[t.name] = Tool(t.name, t.description, t.schema, self._brain_handler(t.name))
        if recipe.files:
            self._add_file_tools()
            if read_only:
                self.tools.pop("fs_write", None)
                self.tools.pop("fs_edit", None)
        if recipe.shell and not read_only:
            self._add_shell_tool()
        if recipe.web:
            self._add_web_tools()

    def specs(self, exclude: set[str] = frozenset()) -> list[Tool]:
        return [t for name, t in self.tools.items() if name not in exclude]

    def call(self, name: str, args, call_id: str | None = None) -> tuple[str, bool]:
        """Run a tool call; emits tool_call/tool_result events. Returns (text, is_error)."""
        call_id = call_id or uuid.uuid4().hex[:12]
        args = args if isinstance(args, dict) else {}
        self.emit({"type": "tool_call", "id": call_id, "name": name, "args": args})
        tool = self.tools.get(name)
        try:
            if tool is None:
                raise ToolError(f"tool '{name}' is not available to this agent")
            reason = tool.approval(args) if tool.approval and not self.auto_approve else None
            if reason and not self.broker.request(self.run_id, name, args, reason, self.emit, self.cancel):
                raise ToolError("the user denied this action")
            text, is_error = _clip(tool.handler(args)), False
        except (ToolError, VaultError) as e:
            text, is_error = str(e), True
        except Exception as e:  # never let one tool call end the conversation
            text, is_error = f"internal error: {type(e).__name__}: {e}", True
        self.emit({"type": "tool_result", "id": call_id, "name": name, "is_error": is_error,
                   "output": text if len(text) <= 4000 else text[:4000] + "…"})
        return text, is_error

    # ------------------------------------------------------------------- brain

    def _brain_handler(self, name: str) -> Callable[[dict], str]:
        return lambda args: brain_tools.call(self.vault, name, args, self.ctx)

    # ------------------------------------------------------------------- files

    def _path(self, raw: str) -> Path:
        roots = self.recipe.workspaces
        p = Path(str(raw or ".")).expanduser()
        if not p.is_absolute():
            p = roots[0] / p
        p = p.resolve()
        if not any(p == r or p.is_relative_to(r) for r in roots):
            raise ToolError(f"'{raw}' is outside the workspace folders: {', '.join(map(str, roots))}")
        if any(part in SECRET_DIRS for part in p.parts) or SECRET_FILE_RE.match(p.name):
            raise ToolError(f"'{raw}' looks like a secret (keys, tokens, .env); agents may not access it")
        return p

    def _add_file_tools(self) -> None:
        roots = ", ".join(map(str, self.recipe.workspaces))
        path_prop = {"type": "string", "description": f"Path, absolute or relative to {self.recipe.workspaces[0]}"}

        def fs_list(args):
            base, depth = self._path(args.get("path", ".")), min(int(args.get("depth") or 2), 4)
            if not base.is_dir():
                raise ToolError(f"{base} is not a folder")
            out = []

            def walk(d: Path, level: int):
                for child in sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
                    if len(out) >= 400 or child.name in SKIP_DIRS:
                        continue
                    rel = child.relative_to(base)
                    out.append(f"{rel}/" if child.is_dir() else f"{rel}  ({child.stat().st_size} B)")
                    if child.is_dir() and level < depth:
                        walk(child, level + 1)
            walk(base, 1)
            return f"[{base}]\n" + ("\n".join(out) or "(empty)") + ("\n… (truncated)" if len(out) >= 400 else "")

        def fs_read(args):
            p = self._path(args["path"])
            if not p.is_file():
                raise ToolError(f"{p} is not a file")
            data = p.read_bytes()[:4_000_000]
            if b"\x00" in data[:8000]:
                text = extract_text(p)  # documents (pdf, docx, ...) still read as text
                if text is None:
                    raise ToolError(f"{p} is a binary file")
                return f"[{p}]\n{text}"
            lines = data.decode("utf-8", errors="replace").splitlines()
            start = max(1, int(args.get("start_line") or 1))
            count = min(int(args.get("max_lines") or 500), 2000)
            chunk = lines[start - 1:start - 1 + count]
            body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(chunk, start))
            more = f"\n… {len(lines) - (start - 1 + len(chunk))} more lines; continue with start_line={start + len(chunk)}" \
                if start - 1 + len(chunk) < len(lines) else ""
            return f"[{p} lines {start}-{start + len(chunk) - 1} of {len(lines)}]\n{body}{more}"

        def fs_search(args):
            base = self._path(args.get("path", "."))
            try:
                rx = re.compile(args["pattern"])
            except re.error as e:
                raise ToolError(f"invalid regular expression: {e}")
            glob = args.get("glob") or "*"
            hits = []
            files = [base] if base.is_file() else (f for f in base.rglob(glob) if f.is_file())
            for f in files:
                if any(part in SKIP_DIRS for part in f.relative_to(base if base.is_dir() else base.parent).parts):
                    continue
                try:
                    if f.stat().st_size > 2_000_000 or SECRET_FILE_RE.match(f.name):
                        continue
                    for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{f}:{n}: {line.strip()[:200]}")
                            if len(hits) >= 200:
                                return "\n".join(hits) + "\n… (more matches; narrow the search)"
                except (UnicodeDecodeError, OSError):
                    continue
            return "\n".join(hits) or "no matches"

        def write_reason(args):
            if not self.recipe.approve_writes:
                return None
            p = self._path(args["path"])
            new = args.get("content", "")
            if p.exists():
                old = p.read_text(encoding="utf-8", errors="replace")
                diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(p), str(p), n=2))
                return f"Replace {p}:\n{_clip(diff, 3000)}"
            return f"Create {p} ({len(new)} characters):\n{_clip(new, 1500)}"

        def fs_write(args):
            p = self._path(args["path"])
            existed = p.exists()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.get("content", ""), encoding="utf-8")
            return f"{'replaced' if existed else 'created'} {p}"

        def edit_reason(args):
            if not self.recipe.approve_writes:
                return None
            p = self._path(args["path"])
            if not p.is_file():
                return None  # the call will fail anyway
            old = p.read_text(encoding="utf-8", errors="replace")
            new = old.replace(args["find"], args.get("replace", ""), 1)
            diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(p), str(p), n=2))
            return f"Edit {p}:\n{_clip(diff, 3000)}"

        def fs_edit(args):
            p = self._path(args["path"])
            if not p.is_file():
                raise ToolError(f"{p} is not a file")
            text = p.read_text(encoding="utf-8")
            count = text.count(args["find"])
            if count != 1:
                raise ToolError("text to find was not found" if count == 0 else
                                f"text to find occurs {count} times; include more context so it is unique")
            p.write_text(text.replace(args["find"], args.get("replace", ""), 1), encoding="utf-8")
            return f"edited {p}"

        self.tools.update({
            "fs_list": Tool("fs_list", f"List files and folders (workspaces: {roots}).",
                            _schema({"path": path_prop, "depth": {"type": "integer", "description": "1-4, default 2"}}, []),
                            fs_list),
            "fs_read": Tool("fs_read", "Read a file with line numbers (long files in pages).",
                            _schema({"path": path_prop, "start_line": {"type": "integer"},
                                     "max_lines": {"type": "integer", "description": "default 500"}}, ["path"]),
                            fs_read),
            "fs_search": Tool("fs_search", "Search file contents with a regular expression.",
                              _schema({"pattern": {"type": "string"}, "path": path_prop,
                                       "glob": {"type": "string", "description": "file name filter, e.g. *.py"}},
                                      ["pattern"]), fs_search),
            "fs_write": Tool("fs_write", "Create a file or replace its whole content.",
                             _schema({"path": path_prop, "content": {"type": "string"}}, ["path", "content"]),
                             fs_write, write_reason),
            "fs_edit": Tool("fs_edit", "Replace one exact passage in a file (must occur exactly once).",
                            _schema({"path": path_prop, "find": {"type": "string"}, "replace": {"type": "string"}},
                                    ["path", "find", "replace"]), fs_edit, edit_reason),
        })

    # ------------------------------------------------------------------- shell

    def _allowed_command(self, command: str) -> bool:
        if SHELL_META_RE.search(command):
            return False  # chained or redirected commands always need approval
        c = " ".join(command.split())
        return any(c == a or c.startswith(a + " ") for a in self.recipe.shell_allow)

    def _add_shell_tool(self) -> None:
        def reason(args):
            if self._allowed_command(args.get("command", "")):
                return None
            return f"Run in {self._path(args.get('cwd', '.'))}:\n$ {args.get('command', '')}"

        def run_command(args):
            cwd = self._path(args.get("cwd", "."))
            if not cwd.is_dir():
                raise ToolError(f"{cwd} is not a folder")
            timeout = min(int(args.get("timeout") or 120), 900)
            env = {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k)}
            try:
                proc = subprocess.run(["bash", "-c", args["command"]], cwd=cwd, env=env, capture_output=True,
                                      text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
                return f"timed out after {timeout}s\n{_clip(out, 10_000)}"
            out = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else "")
            return f"exit code {proc.returncode}\n{_clip(out.strip(), 20_000)}"

        self.tools["run_command"] = Tool(
            "run_command",
            "Run a shell command in a workspace folder. The user approves each command unless it is on the "
            "agent's allowlist.",
            _schema({"command": {"type": "string"},
                     "cwd": {"type": "string", "description": f"Folder, default {self.recipe.workspaces[0]}"},
                     "timeout": {"type": "integer", "description": "seconds, default 120"}}, ["command"]),
            run_command, reason)

    # --------------------------------------------------------------------- web

    def _add_web_tools(self) -> None:
        def web_fetch(args):
            text, ctype = fetch_url(args["url"])
            limit = min(int(args.get("max_chars") or 20_000), 60_000)
            return f"[{args['url']} · {ctype}]\n{_clip(text, limit)}"

        self.tools["web_fetch"] = Tool(
            "web_fetch", "Fetch a web page or document and return its text.",
            _schema({"url": {"type": "string"}, "max_chars": {"type": "integer", "description": "default 20000"}},
                    ["url"]), web_fetch)

        if self.searxng_url:
            def web_search(args):
                q = urllib.parse.urlencode({"q": args["query"], "format": "json"})
                with urllib.request.urlopen(f"{self.searxng_url.rstrip('/')}/search?{q}", timeout=20) as r:
                    results = json.loads(r.read())["results"][: int(args.get("limit") or 8)]
                return "\n".join(f"- {x.get('title')}\n  {x.get('url')}\n  {x.get('content', '')[:300]}"
                                 for x in results) or "no results"

            self.tools["web_search"] = Tool(
                "web_search", "Search the web. Returns titles, URLs and snippets; use web_fetch to read a page.",
                _schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]), web_search)


# ----------------------------------------------------------------------- web fetch

def _public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ToolError(f"cannot resolve {host}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ToolError(f"{host} is a local or private address; agents may only fetch public web pages")


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_host(urllib.parse.urlparse(newurl).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, max_bytes: int = 5_000_000) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("only http(s) URLs can be fetched")
    _public_host(parsed.hostname)
    opener = urllib.request.build_opener(_SafeRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ai-brain/0.1)"})
    try:
        with opener.open(req, timeout=30) as resp:
            data = resp.read(max_bytes)
            ctype = resp.headers.get_content_type()
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} from {url}")
    except urllib.error.URLError as e:
        raise ToolError(f"cannot fetch {url}: {e.reason}")
    if ctype in ("text/html", "application/xhtml+xml"):
        return html_to_text(data.decode(charset, errors="replace")), ctype
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        return data.decode(charset, errors="replace"), ctype
    suffix = {"application/pdf": ".pdf",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}.get(ctype)
    if suffix:
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(data)
            tmp.flush()
            text = extract_text(Path(tmp.name))
        if text:
            return text, ctype
    raise ToolError(f"{url} returned {ctype}, which can't be read as text")
