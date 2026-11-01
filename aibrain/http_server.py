"""MCP over HTTP ("Streamable HTTP" transport) so a hosted brain is reachable from anywhere.

POST /mcp carries one JSON-RPC message (or a batch) and gets a JSON reply. Each client gets a
session (Mcp-Session-Id header) with its own identity for change attribution. All requests
except GET /health need `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .mcp_server import McpServer
from .tools import Context
from .vault import Vault

MAX_BODY = 10 * 1024 * 1024
MAX_SESSIONS = 1000


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class BrainHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, vault: Vault, token: str | None, import_roots: list[Path]):
        super().__init__(address, Handler)
        self.vault = vault
        self.token = token
        self.import_roots = import_roots
        self.sessions: OrderedDict[str, McpServer] = OrderedDict()
        self.sessions_lock = threading.Lock()

    def new_session(self) -> tuple[str, McpServer]:
        sid = secrets.token_urlsafe(24)
        server = McpServer(self.vault, Context(actor="agent:remote", import_roots=self.import_roots))
        with self.sessions_lock:
            self.sessions[sid] = server
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)
        return sid, server

    def session(self, sid: str | None) -> McpServer | None:
        if not sid:
            return None
        with self.sessions_lock:
            server = self.sessions.get(sid)
            if server is not None:
                self.sessions.move_to_end(sid)
            return server


class Handler(BaseHTTPRequestHandler):
    server: BrainHttpServer
    protocol_version = "HTTP/1.1"
    server_version = f"ai-brain/{__version__}"

    def log_message(self, fmt, *args):
        if self.path == "/health":  # polled by Docker every 30s; would drown out everything else
            return
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def _send(self, status: int, body=None, headers: dict | None = None) -> None:
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if self.server.token is None:
            return True
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if hmac.compare_digest(supplied.encode(), self.server.token.encode()):
            return True
        self._send(401, {"error": "missing or invalid bearer token"}, {"WWW-Authenticate": "Bearer"})
        return False

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "version": __version__})
        if self.path == "/mcp":
            if self._authorized():
                self._send(405, {"error": "this server does not open server-initiated streams"}, {"Allow": "POST, DELETE"})
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path != "/mcp":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return
        sid = self.headers.get("Mcp-Session-Id")
        with self.server.sessions_lock:
            self.server.sessions.pop(sid, None)
        self._send(204)

    def do_POST(self):
        if self.path != "/mcp":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, {"error": "request too large"})
        try:
            msg = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})

        messages = msg if isinstance(msg, list) else [msg]
        is_init = any(isinstance(m, dict) and m.get("method") == "initialize" for m in messages)
        headers = {}
        if is_init:
            sid, server = self.server.new_session()
            headers["Mcp-Session-Id"] = sid
        else:
            sid = self.headers.get("Mcp-Session-Id")
            server = self.server.session(sid)
            if server is None:
                if sid:  # an expired or unknown session: the client must initialize again
                    return self._send(404, {"error": "unknown session; initialize again"})
                sid, server = self.server.new_session()  # clients that skip sessions entirely

        reply = server.handle(msg)
        if reply is None:  # only notifications or responses
            return self._send(202, None, headers)
        self._send(200, reply, headers)


def serve_http(vault: Vault, host: str, port: int, token: str | None, import_roots: list[Path]) -> None:
    if token is None and not is_loopback(host):
        raise SystemExit("refusing to serve on a non-loopback address without a token; set BRAIN_TOKEN")
    httpd = BrainHttpServer((host, port), vault, token, import_roots)
    print(f"AI Brain MCP server on http://{host}:{port}/mcp (vault: {vault.root})", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
