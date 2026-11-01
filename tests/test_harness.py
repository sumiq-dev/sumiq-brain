import json
import stat
import threading
import time

import pytest

from aibrain.harness.chat import RunMcpServer
from aibrain.harness.recipes import Recipe, RecipeError, load_recipes
from aibrain.harness.runtime import Runtime, Transcript, build_system
from aibrain.harness.store import Store
from aibrain.harness.toolbox import ApprovalBroker, Toolbox, ToolError, fetch_url
from aibrain.settings import Settings

from test_engines import FakeApi


# --------------------------------------------------------------------------- helpers

def recipe(tmp_path, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    data = {"tools": {"files": True, "shell": True, "web": False}, "workspaces": [str(ws)], **kw}
    return Recipe.from_dict("coder", data)


class Approver:
    """Answers approval requests automatically, recording them."""

    def __init__(self, broker, answer=True):
        self.broker, self.answer, self.seen = broker, answer, []

    def __call__(self, event):
        if event["type"] == "approval_request":
            self.seen.append(event)
            threading.Thread(target=self.broker.resolve, args=(event["approval_id"], self.answer)).start()


def toolbox(vault, rec, answer=True, events=None):
    broker = ApprovalBroker()
    approver = Approver(broker, answer)
    log = events if events is not None else []

    def emit(e):
        log.append(e)
        approver(e)
    tb = Toolbox(vault, rec, "agent:coder", emit, broker, "run1")
    tb.approver = approver
    return tb


# --------------------------------------------------------------------------- recipes

def test_recipes(tmp_path):
    d = tmp_path / "recipes"
    d.mkdir()
    (d / "coder.yaml").write_text(f"name: Coder\ntools: {{files: true}}\nworkspaces: [{tmp_path}]\n")
    recipes = load_recipes(d)
    assert set(recipes) == {"standard", "coder"}
    assert recipes["standard"].web and not recipes["standard"].files
    assert recipes["coder"].files and recipes["coder"].workspaces == [tmp_path.resolve()]
    with pytest.raises(RecipeError, match="workspace"):
        Recipe.from_dict("x", {"tools": {"shell": True}})
    with pytest.raises(RecipeError, match="unknown tools"):
        Recipe.from_dict("x", {"tools": {"rocket": True}})


# --------------------------------------------------------------------------- store

def test_store(tmp_path):
    s = Store(tmp_path / "h.db")
    t = s.create_thread("standard")
    s.add_items(t["id"], [("user", {"text": "hi"}), ("assistant", {"text": "hello"})])
    s.add_items(t["id"], [("user", {"text": "again"})])
    s.update_thread(t["id"], title="Greeting", state={"session_id": "abc"})
    assert [i["text"] for i in s.items(t["id"])] == ["hi", "hello", "again"]
    assert s.thread(t["id"])["state"] == {"session_id": "abc"}
    assert s.threads()[0]["title"] == "Greeting"
    s.delete_thread(t["id"])
    assert s.items(t["id"]) == [] and s.thread(t["id"]) is None


# --------------------------------------------------------------------------- toolbox

def test_brain_tools_always_available(vault, tmp_path):
    standard = Recipe("standard", "Standard")
    tb = toolbox(vault, standard)
    assert "read_index" in tb.tools and "write_note" in tb.tools
    assert not {"fs_read", "run_command"} & set(tb.tools)
    text, err = tb.call("fs_read", {"path": "/etc/passwd"})
    assert err and "not available" in text


def test_file_tools_stay_in_workspace(vault, tmp_path):
    rec = recipe(tmp_path)
    tb = toolbox(vault, rec)
    (rec.workspaces[0] / "a.txt").write_text("one\ntwo\nthree\n")
    (rec.workspaces[0] / ".env").write_text("SECRET=1")
    text, err = tb.call("fs_read", {"path": "a.txt", "start_line": 2, "max_lines": 1})
    assert not err and "    2  two" in text and "continue with start_line=3" in text
    for bad in ("/etc/passwd", "../outside.txt", ".env"):
        text, err = tb.call("fs_read", {"path": bad})
        assert err, bad
    (tmp_path / "link").symlink_to("/etc")
    (rec.workspaces[0] / "escape").symlink_to("/etc")
    assert tb.call("fs_read", {"path": "escape/passwd"})[1]  # symlinks can't escape either
    assert "a.txt" in tb.call("fs_list", {})[0]
    assert "a.txt:2: two" in tb.call("fs_search", {"pattern": "tw"})[0]


def test_writes_need_approval(vault, tmp_path):
    rec = recipe(tmp_path)
    tb = toolbox(vault, rec, answer=False)
    text, err = tb.call("fs_write", {"path": "new.py", "content": "print(1)\n"})
    assert err and "denied" in text and not (rec.workspaces[0] / "new.py").exists()
    assert "Create" in tb.approver.seen[0]["reason"]

    tb = toolbox(vault, rec, answer=True)
    assert not tb.call("fs_write", {"path": "new.py", "content": "print(1)\n"})[1]
    assert not tb.call("fs_edit", {"path": "new.py", "find": "1", "replace": "2"})[1]
    assert (rec.workspaces[0] / "new.py").read_text() == "print(2)\n"
    assert "-print(1)" in tb.approver.seen[1]["reason"] and "+print(2)" in tb.approver.seen[1]["reason"]

    no_ask = toolbox(vault, recipe(tmp_path, approve_writes=False), answer=False)
    assert not no_ask.call("fs_write", {"path": "free.txt", "content": "x"})[1] and not no_ask.approver.seen


def test_shell_allowlist_and_approval(vault, tmp_path):
    rec = recipe(tmp_path, shell_allow=["echo"])
    tb = toolbox(vault, rec, answer=False)
    text, err = tb.call("run_command", {"command": "echo hello"})
    assert not err and "exit code 0" in text and "hello" in text and not tb.approver.seen
    for cmd in ("echo hi; rm -rf x", "echo $(whoami)", "echo hi > f", "ls"):
        assert tb.call("run_command", {"command": cmd})[1], cmd  # all need approval -> denied
    assert len(tb.approver.seen) == 4


def test_shell_hides_secrets(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    tb = toolbox(vault, recipe(tmp_path, shell_allow=["env"]))
    assert "sk-secret" not in tb.call("run_command", {"command": "env"})[0]


def test_web_fetch_blocks_private_addresses():
    for url in ("http://127.0.0.1:8765/health", "http://localhost/", "http://192.168.1.1/", "file:///etc/passwd"):
        with pytest.raises(ToolError):
            fetch_url(url)


def test_events_and_errors_are_reported(vault, tmp_path):
    events = []
    tb = toolbox(vault, recipe(tmp_path), events=events)
    tb.call("read_note", {"name": "missing"}, "c1")
    assert [e["type"] for e in events] == ["tool_call", "tool_result"]
    assert events[1]["is_error"] and events[1]["id"] == "c1"


def test_run_mcp_server_exposes_toolbox(vault, tmp_path):
    import urllib.request
    tb = toolbox(vault, recipe(tmp_path))
    server = RunMcpServer(tb)
    try:
        def post(msg, token=server.token):
            req = urllib.request.Request(server.url, data=json.dumps(msg).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        tools = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        assert {"read_index", "fs_read", "run_command"} <= {t["name"] for t in tools}
        res = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "read_index", "arguments": {"path": "."}}})["result"]
        assert not res["isError"] and "[index.md]" in res["content"][0]["text"]
        with pytest.raises(urllib.error.HTTPError):
            post({"jsonrpc": "2.0", "id": 3, "method": "ping"}, token="wrong")
    finally:
        server.close()


# --------------------------------------------------------------------------- runtime

def test_transcript_groups_text_around_tools():
    t = Transcript()
    for e in [{"type": "text", "delta": "Let me "}, {"type": "text", "delta": "check."},
              {"type": "tool_call", "id": "1", "name": "read_index", "args": {}},
              {"type": "tool_result", "id": "1", "name": "read_index", "output": "...", "is_error": False},
              {"type": "text", "delta": "Done."}]:
        t.feed(e)
    t.flush()
    assert [k for k, _ in t.items] == ["assistant", "tool_call", "tool_result", "assistant"]
    assert t.items[0][1]["text"] == "Let me check."


def test_system_prompt(vault, tmp_path):
    s = build_system(recipe(tmp_path, instructions="Be terse.", shell_allow=["git status"]), vault)
    assert "Map of the brain" in s and "- projects/ —" in s
    assert "approves each command (no approval needed for: git status)" in s
    assert s.endswith("Your instructions:\nBe terse.")
    assert "cannot access the user's files" in build_system(Recipe("standard", "Standard"), vault)


def anthropic_sse(blocks_per_turn):
    """Server-sent events for scripted Anthropic streaming responses."""
    responses = []
    for blocks, stop in blocks_per_turn:
        events = [("message_start", {"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}})]
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                events += [("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "text", "text": ""}}),
                           ("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "text_delta", "text": b["text"]}})]
            else:
                events += [("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {**b, "input": {}}}),
                           ("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "input_json_delta",
                                                              "partial_json": json.dumps(b["input"])}})]
            events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                      "usage": {"output_tokens": 5}}),
                   ("message_stop", {"type": "message_stop"})]
        responses.append("".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events))
    return responses


class SseApi(FakeApi):
    def __init__(self, bodies):
        super().__init__([])
        self.bodies = list(bodies)
        fake = self
        from http.server import BaseHTTPRequestHandler

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = fake.bodies.pop(0).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.httpd.RequestHandlerClass = H


def make_runtime(vault, tmp_path, provider: dict, recipes=None):
    settings = Settings.parse({"providers": {"p": provider}, "agents": {"default": {"provider": "p"}}})
    return Runtime(vault, settings, recipes or {"standard": Recipe("standard", "Standard", web=False)},
                   Store(tmp_path / "h.db"), tmp_path / "harness")


def finish(run, timeout=20):
    deadline = time.time() + timeout
    while not run.done and time.time() < deadline:
        run.wait(len(run.events), 0.5)
    assert run.done
    return run.events


def test_anthropic_chat_two_turns(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("K", "sk")
    api = SseApi(anthropic_sse([
        ([{"type": "text", "text": "Checking. "},
          {"type": "tool_use", "id": "tu1", "name": "read_index", "input": {"path": "."}}], "tool_use"),
        ([{"type": "text", "text": "There are 8 areas."}], "end_turn"),
        ([{"type": "text", "text": "You asked about areas."}], "end_turn"),
    ]))
    try:
        rt = make_runtime(vault, tmp_path, {"kind": "anthropic", "api_key_env": "K", "base_url": api.url})
        tid = rt.new_thread("standard")["id"]
        events = finish(rt.start(tid, "How many areas?"))
        kinds = [e["type"] for e in events]
        assert kinds[0] == "run_started" and kinds[-1] == "done"
        assert "".join(e["delta"] for e in events if e["type"] == "text") == "Checking. There are 8 areas."
        assert [e["name"] for e in events if e["type"] == "tool_call"] == ["read_index"]
        finish(rt.start(tid, "What did I ask?"))
    finally:
        api.close()
    third = api.requests[2]
    assert [m["role"] for m in third["messages"]] == ["user", "assistant", "user", "assistant", "user"]
    assert third["system"][0]["text"] == api.requests[0]["system"][0]["text"]  # frozen per thread
    assert third["cache_control"] == {"type": "ephemeral"}
    items = rt.store.items(tid)
    assert [i["kind"] for i in items if i["kind"] != "usage"] == [
        "user", "assistant", "tool_call", "tool_result", "assistant", "user", "assistant"]
    assert rt.store.thread(tid)["title"] == "How many areas?"


def test_one_run_per_thread_and_cancel(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("K", "sk")
    rt = make_runtime(vault, tmp_path, {"kind": "anthropic", "api_key_env": "K", "base_url": "http://127.0.0.1:9"})
    tid = rt.new_thread("standard")["id"]
    run = rt.start(tid, "hi")
    finish(run)
    assert any(e["type"] == "error" and "cannot reach" in e["message"] for e in run.events)
    assert rt.store.items(tid)[0]["text"] == "hi"


def test_cli_engine_end_to_end_with_fake_claude(vault, tmp_path):
    """A fake `claude` that talks to the run's MCP endpoint like the real CLI would."""
    script = tmp_path / "claude"
    script.write_text(f"""#!{__import__('sys').executable}
import json, sys, urllib.request
argv = sys.argv[1:]
cfg = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["harness"]
prompt = sys.stdin.read()
json.dump(argv, open({str(tmp_path / 'argv.json')!r}, "w"))
req = urllib.request.Request(cfg["url"], method="POST", headers={{**cfg["headers"], "Content-Type": "application/json"}},
    data=json.dumps({{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {{"name": "read_index", "arguments": {{"path": "projects"}}}}}}).encode())
out = json.loads(urllib.request.urlopen(req).read())["result"]["content"][0]["text"]
def emit(e): print(json.dumps(e), flush=True)
emit({{"type": "stream_event", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": "Projects index has " + str(len(out)) + " chars. You said: " + prompt}}}}}})
emit({{"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": "s",
      "usage": {{"input_tokens": 3, "output_tokens": 4}}, "total_cost_usd": 0.01,
      "modelUsage": {{"claude-sonnet-5-5": {{"inputTokens": 3, "outputTokens": 4, "costUSD": 0.01}}}}}})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    rt = make_runtime(vault, tmp_path, {"kind": "claude_code", "command": str(script)})
    tid = rt.new_thread("standard")["id"]
    events = finish(rt.start(tid, "hello"))
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    assert text.startswith("Projects index has") and text.endswith("You said: hello")
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["read_index"]  # via MCP -> toolbox
    done = events[-1]
    assert done["status"] == "done" and done["model"] == "claude-sonnet-5-5" and done["usage"]["cost_usd"] == 0.01
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "--session-id" in argv and argv[argv.index("--tools") + 1] == ""  # web off in this recipe
    finish(rt.start(tid, "again"))
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == rt.store.thread(tid)["state"]["session_id"]


# --------------------------------------------------------------------------- HTTP API

def test_http_api_flow(vault, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from aibrain.harness.server import create_app

    monkeypatch.setenv("K", "sk")
    api = SseApi(anthropic_sse([([{"type": "text", "text": "Hello!"}], "end_turn")]))
    try:
        rt = make_runtime(vault, tmp_path, {"kind": "anthropic", "api_key_env": "K", "base_url": api.url})
        client = TestClient(create_app(rt, token="t"))
        assert client.get("/api/threads").status_code == 401
        h = {"Authorization": "Bearer t"}
        assert client.get("/api/recipes", headers=h).json()[0]["tools"]["brain"] is True
        tid = client.post("/api/threads", json={"recipe": "standard"}, headers=h).json()["id"]
        rid = client.post(f"/api/threads/{tid}/messages", json={"text": "hi"}, headers=h).json()["run_id"]
        with client.stream("GET", f"/api/runs/{rid}/events", headers=h) as r:
            body = "".join(r.iter_text())
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        assert [e["type"] for e in events] == ["run_started", "text", "done"]
        thread = client.get(f"/api/threads/{tid}", headers=h).json()
        assert [i["kind"] for i in thread["items"]][:2] == ["user", "assistant"]
        assert client.post("/api/approvals/nope", json={"approved": True}, headers=h).status_code == 404
        assert client.delete(f"/api/threads/{tid}", headers=h).json() == {"ok": True}
    finally:
        api.close()


def test_package_includes_subpackages():
    """The installed package (e.g. in Docker) must contain aibrain.harness, not just aibrain."""
    import tomllib
    from pathlib import Path
    from setuptools import find_packages
    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"]["find"]
    assert "aibrain.harness" in find_packages(str(root), include=cfg["include"])


# --------------------------------------------------------------------------- chat options

def two_provider_runtime(vault, tmp_path, monkeypatch, api_a, api_b, recipes=None):
    monkeypatch.setenv("K", "sk")
    settings = Settings.parse({
        "providers": {"a": {"kind": "anthropic", "api_key_env": "K", "base_url": api_a},
                      "b": {"kind": "openai", "base_url": api_b, "models": ["qwen3"]}},
        "agents": {"default": {"provider": "a", "model": "claude-sonnet-5-5"}}})
    return Runtime(vault, settings, recipes or {"standard": Recipe("standard", "Standard", web=False)},
                   Store(tmp_path / "h.db"), tmp_path / "harness")


def openai_sse(text):
    chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "qwen3",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def test_switch_provider_mid_conversation(vault, tmp_path, monkeypatch):
    a = SseApi(anthropic_sse([([{"type": "text", "text": "Pity is 90."}], "end_turn")]))
    b = SseApi([openai_sse("As I said, 90.")])
    try:
        rt = two_provider_runtime(vault, tmp_path, monkeypatch, a.url, b.url + "/v1")
        tid = rt.new_thread("standard")["id"]
        finish(rt.start(tid, "What is the hard pity?"))
        assert rt.set_options(tid, {"provider": "b", "model": "qwen3"})["model"] == "qwen3"
        events = finish(rt.start(tid, "Repeat that."))
    finally:
        a.close()
        b.close()
    assert any(e["type"] == "notice" and "Switched to b" in e["text"] for e in events)
    sent = b.requests[0]["messages"][-1]["content"]
    assert "User: What is the hard pity?" in sent and "Assistant: Pity is 90." in sent and sent.endswith("Repeat that.")
    kinds = [i["kind"] for i in rt.store.items(tid)]
    assert kinds.count("notice") == 1 and kinds.index("notice") < len(kinds) - 2  # stored before the new message


def test_effort_and_access_change(vault, tmp_path, monkeypatch):
    a = SseApi(anthropic_sse([([{"type": "text", "text": "ok"}], "end_turn")] * 2))
    try:
        rt = two_provider_runtime(vault, tmp_path, monkeypatch, a.url, "http://127.0.0.1:9/v1")
        tid = rt.new_thread("standard", {"effort": "high"})["id"]
        finish(rt.start(tid, "one"))
        rt.set_options(tid, {"access": "read", "web": True})
        events = finish(rt.start(tid, "two"))
    finally:
        a.close()
    first, second = a.requests
    assert first["output_config"] == {"effort": "high"}
    assert "write_note" in {t.get("name") for t in first["tools"]}
    assert "write_note" not in {t.get("name") for t in second["tools"]}       # read-only now
    assert any(t.get("type", "").startswith("web_search") for t in second["tools"])  # web switched on
    assert second["messages"][-1]["content"].startswith("[Note from the app: access for this conversation changed.")
    assert any(e["type"] == "notice" for e in events)


def test_option_validation(vault, tmp_path, monkeypatch):
    rt = two_provider_runtime(vault, tmp_path, monkeypatch, "http://x", "http://y/v1")
    tid = rt.new_thread("standard")["id"]
    from aibrain.harness.options import OptionsError
    for bad in ({"provider": "nope"}, {"access": "root"}, {"files": True}, {"effort": "ultra"}, {"color": "red"}):
        with pytest.raises(OptionsError):
            rt.set_options(tid, bad)
    opts = rt.thread_options(rt.store.thread(tid))
    assert opts == {"provider": "a", "model": "claude-sonnet-5-5", "effort": None, "web": False,
                    "files": False, "shell": False, "access": "ask"}
    assert rt.set_options(tid, {"provider": "b"})["model"] is None  # the old provider's model doesn't carry over


def test_full_auto_skips_approvals(vault, tmp_path):
    rec = recipe(tmp_path)
    broker = ApprovalBroker()
    tb = Toolbox(vault, rec, "agent:t", lambda e: None, broker, auto_approve=True)
    assert not tb.call("fs_write", {"path": "x.txt", "content": "hi"})[1]
    assert not tb.call("run_command", {"command": "echo hi"})[1]
    ro = Toolbox(vault, rec, "agent:t", lambda e: None, broker, read_only=True)
    assert not {"fs_write", "fs_edit", "run_command", "write_note", "edit_note"} & set(ro.tools)
    assert {"fs_read", "read_note"} <= set(ro.tools)


def test_providers_endpoint(vault, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from aibrain.harness.server import create_app
    rt = two_provider_runtime(vault, tmp_path, monkeypatch, "http://x", "http://y/v1")
    monkeypatch.delenv("K")
    client = TestClient(create_app(rt))
    providers = {p["name"]: p for p in client.get("/api/providers").json()}
    assert providers["a"]["available"] is False and "needs K" in providers["a"]["reason"]
    assert providers["a"]["efforts"][-1] == "max" and providers["b"]["efforts"] == []
    assert [m["id"] for m in providers["b"]["models"]] == ["qwen3"]
    assert client.get("/api/recipes").json()[0]["defaults"]["access"] == "ask"


def test_cli_usage_is_per_turn():
    from aibrain.harness.chat import ClaudeCodeChat
    state = {}
    turn1 = {"modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 20, "costUSD": 0.001}}}
    turn2 = {"modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 20, "costUSD": 0.001},
                            "claude-sonnet-5-5": {"inputTokens": 30, "outputTokens": 5, "costUSD": 0.02}}}
    assert ClaudeCodeChat._turn_usage(state, turn1, True) == (
        {"input_tokens": 10, "output_tokens": 20, "cost_usd": 0.001}, "claude-haiku-5-5")
    usage, model = ClaudeCodeChat._turn_usage(state, turn2, False)
    assert model == "claude-sonnet-5-5" and usage == {"input_tokens": 30, "output_tokens": 5, "cost_usd": 0.02}
