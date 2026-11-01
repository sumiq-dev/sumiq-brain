import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aibrain.agents import READ_TOOLS, run_agent
from aibrain.engines import Task, engine_for
from aibrain.settings import Settings, SettingsError, TEMPLATE

import yaml


class FakeApi:
    """A local HTTP server that replays scripted JSON responses and records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                data = json.dumps(fake.responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def settings_for(provider: dict, agent: dict | None = None) -> Settings:
    return Settings.parse({"providers": {"p": provider}, "agents": {"default": {"provider": "p", **(agent or {})}}})


def test_template_parses():
    s = Settings.parse(yaml.safe_load(TEMPLATE))
    assert {p.kind for p in s.providers.values()} == {"claude_code", "codex", "anthropic", "openai"}
    spec = s.agent("navigator")
    assert spec.provider.name == "claude-subscription" and spec.model == "opus"


def test_settings_resolution_and_errors():
    s = Settings.parse({"providers": {"a": {"kind": "anthropic"}, "b": {"kind": "openai"}},
                        "agents": {"default": {"provider": "a", "model": "claude-opus-5-5"},
                                   "navigator": {"model": "claude-haiku-5-5"}}})
    assert s.agent("navigator").model == "claude-haiku-5-5"
    assert s.agent("worker").model == "claude-opus-5-5"
    assert s.agent("worker", provider="b").model is None  # model names don't carry across providers
    assert s.agent("worker", provider="b", model="qwen3").model == "qwen3"
    with pytest.raises(SettingsError, match="unknown provider"):
        s.agent("worker", provider="zzz")
    with pytest.raises(SettingsError, match="kind must be"):
        Settings.parse({"providers": {"x": {"kind": "magic"}}})


def test_anthropic_engine_tool_loop(vault, monkeypatch):
    api = FakeApi([
        {"id": "m1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
         "content": [{"type": "tool_use", "id": "tu1", "name": "read_index", "input": {"path": "."}},
                     {"type": "tool_use", "id": "tu2", "name": "write_note", "input": {}}],
         "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"id": "m2", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
         "content": [{"type": "text", "text": "The brain has 8 areas."}],
         "stop_reason": "end_turn", "stop_sequence": None,
         "usage": {"input_tokens": 20, "output_tokens": 7, "cache_read_input_tokens": 9}},
    ])
    monkeypatch.setenv("TEST_KEY", "sk-test")
    try:
        s = settings_for({"kind": "anthropic", "api_key_env": "TEST_KEY", "base_url": api.url},
                         {"model": "claude-opus-5-5", "effort": "low"})
        result = run_agent("navigator", "How many areas?", vault, s)
    finally:
        api.close()
    assert result.text == "The brain has 8 areas." and result.status == "done" and result.turns == 2
    assert result.tool_calls == ["read_index", "write_note"]
    assert result.usage == {"input_tokens": 30, "output_tokens": 12, "cache_read_tokens": 9}
    first, second = (r["body"] for r in api.requests)
    assert first["output_config"] == {"effort": "low"} and "fallbacks" not in first  # no fallback via base_url
    assert {t["name"] for t in first["tools"]} == set(READ_TOOLS)
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    results = second["messages"][-1]["content"]  # both results in one user message
    assert [r["tool_use_id"] for r in results] == ["tu1", "tu2"]
    assert "[index.md]" in results[0]["content"] and results[0]["is_error"] is False
    assert results[1]["is_error"] is True and "not available" in results[1]["content"]  # read-only role


def test_anthropic_engine_refusal(vault, monkeypatch):
    api = FakeApi([{"id": "m1", "type": "message", "role": "assistant", "model": "x", "content": [],
                    "stop_reason": "refusal", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}])
    monkeypatch.setenv("TEST_KEY", "sk-test")
    try:
        result = run_agent("navigator", "q", vault, settings_for(
            {"kind": "anthropic", "api_key_env": "TEST_KEY", "base_url": api.url}))
    finally:
        api.close()
    assert result.status == "refusal"


def test_openai_engine_tool_loop(vault):
    def completion(message, finish):
        return {"id": "c", "object": "chat.completion", "created": 0, "model": "qwen3",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    api = FakeApi([
        completion({"role": "assistant", "content": None, "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": "read_index", "arguments": "{\"path\": \"projects\"}"}},
            {"id": "call2", "type": "function", "function": {"name": "read_note", "arguments": "not json"}},
        ]}, "tool_calls"),
        completion({"role": "assistant", "content": "No projects yet."}, "stop"),
    ])
    try:
        result = run_agent("navigator", "Projects?", vault, settings_for(
            {"kind": "openai", "base_url": api.url + "/v1"}, {"model": "qwen3"}))
    finally:
        api.close()
    assert result.text == "No projects yet." and result.turns == 2
    msgs = api.requests[1]["body"]["messages"]
    assert msgs[0]["role"] == "system"
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "call1" and "[projects/index.md]" in tool_msgs[0]["content"]
    assert "not valid JSON" in tool_msgs[1]["content"]


def test_openai_engine_needs_model(vault):
    from aibrain.engines import EngineError
    with pytest.raises(EngineError, match="set a model"):
        run_agent("navigator", "q", vault, settings_for({"kind": "openai", "base_url": "http://x/v1"}))


def fake_cli(tmp_path, name, output: str):
    """A stand-in for the vendor CLI that records its arguments and stdin."""
    script = tmp_path / name
    script.write_text(f"""#!/usr/bin/env python3
import json, sys
if sys.argv[1:3] == ["login", "status"]:
    sys.exit(0)
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}, open({str(tmp_path / 'call.json')!r}, "w"))
if "-o" in sys.argv:
    open(sys.argv[sys.argv.index("-o") + 1], "w").write("codex answer")
print({output!r})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_claude_code_engine(vault, tmp_path):
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Answer.",
                      "num_turns": 4, "usage": {"input_tokens": 5, "output_tokens": 6}})
    cli = fake_cli(tmp_path, "claude", out)
    s = settings_for({"kind": "claude_code", "command": str(cli)}, {"model": "sonnet"})
    result = run_agent("navigator", "Question?", vault, s)
    assert result.text == "Answer." and result.turns == 4 and result.usage["output_tokens"] == 6
    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    assert call["stdin"] == "Question?"
    assert argv[argv.index("--tools") + 1] == ""  # built-in tools disabled
    assert argv[argv.index("--model") + 1] == "sonnet"
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert allowed == [f"mcp__brain__{t}" for t in READ_TOOLS]
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["brain"]
    assert "--actor" in mcp["args"] and "agent:navigator" in mcp["args"]
    assert mcp["args"][mcp["args"].index("--tools") + 1] == ",".join(READ_TOOLS)


def test_codex_engine(vault, tmp_path):
    events = "\n".join(json.dumps(e) for e in [
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "read_index"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 50}},
    ])
    cli = fake_cli(tmp_path, "codex", events)
    s = settings_for({"kind": "codex", "command": str(cli)})
    result = run_agent("navigator", "Question?", vault, s)
    assert result.text == "codex answer" and result.tool_calls == ["read_index"]
    assert result.usage == {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50}
    call = json.loads((tmp_path / "call.json").read_text())
    assert "--sandbox" in call["argv"] and "read-only" in call["argv"]
    assert call["stdin"].endswith("---\n\nQuestion?")


def test_missing_cli_is_reported(vault, monkeypatch):
    from aibrain import engines
    monkeypatch.setattr(engines.shutil, "which", lambda name: None)
    spec = settings_for({"kind": "codex"}).agent("navigator")
    with pytest.raises(engines.EngineError, match="'codex' CLI not found"):
        engine_for(spec, vault).run(Task("navigator", "s", "p", READ_TOOLS))


def test_claude_code_not_signed_in(vault, tmp_path):
    from aibrain.engines import EngineError
    out = json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"})
    cli = fake_cli(tmp_path, "claude", out)
    with pytest.raises(EngineError, match="not signed in"):
        run_agent("navigator", "q", vault, settings_for({"kind": "claude_code", "command": str(cli)}))


def test_codex_not_signed_in(vault, tmp_path):
    from aibrain.engines import EngineError
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\nexit 1\n")
    cli.chmod(0o755)
    with pytest.raises(EngineError, match="codex login --device-auth"):
        run_agent("navigator", "q", vault, settings_for({"kind": "codex", "command": str(cli)}))
