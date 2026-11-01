import io
import json
import subprocess
import sys

from aibrain.mcp_server import McpServer
from aibrain.tools import TOOLS, Context


def rpc(server, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        msg["id"] = msg_id
    return server.handle(msg)


def test_handshake_and_tools(vault):
    server = McpServer(vault, Context())
    init = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "Claude Code"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert "read_index" in init["result"]["instructions"]
    assert server.ctx.actor == "agent:claude-code"
    assert rpc(server, "notifications/initialized", msg_id=None) is None
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == set(TOOLS)
    assert next(t for t in tools if t["name"] == "read_index")["annotations"]["readOnlyHint"] is True


def test_unknown_protocol_version_gets_latest(vault):
    res = rpc(McpServer(vault, Context()), "initialize", {"protocolVersion": "1999-01-01"})
    assert res["result"]["protocolVersion"] == "2025-11-25"


def test_tool_call_success_and_error(vault):
    server = McpServer(vault, Context())
    ok = rpc(server, "tools/call", {"name": "read_index", "arguments": {"path": "."}})["result"]
    assert ok["isError"] is False and "[index.md]" in ok["content"][0]["text"]
    bad = rpc(server, "tools/call", {"name": "read_note", "arguments": {"name": "nope"}})["result"]
    assert bad["isError"] is True and "no note" in bad["content"][0]["text"]
    missing = rpc(server, "tools/call", {"name": "read_note", "arguments": {}})["result"]
    assert missing["isError"] and "missing required" in missing["content"][0]["text"]
    assert rpc(server, "tools/call", {"name": "explode"})["error"]["code"] == -32602
    assert rpc(server, "resources/list")["error"]["code"] == -32601


def test_serve_over_streams(vault):
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ]
    out = io.StringIO()
    McpServer(vault, Context()).serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700


def test_cli_serve_end_to_end(vault):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "write_note", "arguments": {"path": "topics/hello.md", "title": "Hello",
                                                "summary": "First note.", "body": "Hi."}}},
    ]
    proc = subprocess.run([sys.executable, "-m", "aibrain.cli", "serve", str(vault.root), "--actor", "agent:e2e"],
                          input="".join(json.dumps(m) + "\n" for m in msgs),
                          capture_output=True, text=True, timeout=30)
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert replies[1]["result"]["content"][0]["text"] == "saved topics/hello.md; folder index updated"
    assert "[[hello]] — First note." in (vault.root / "topics" / "index.md").read_text()
