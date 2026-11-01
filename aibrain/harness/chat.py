"""Streaming chat engines. Each runs one conversational turn: stream text and tool activity as
events, execute tool calls through the toolbox, and keep the provider's conversation state so the
next turn continues exactly where this one ended (append-only, which prompt caching relies on)."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..engines import DEFAULT_ANTHROPIC_MODEL, FALLBACK_BETA, FALLBACK_MODELS, EngineError
from ..mcp_server import McpServer
from ..settings import AgentSpec
from ..tools import Context
from .toolbox import Emit, Toolbox

SERVER_WEB_TOOLS = [{"type": "web_search_20260209", "name": "web_search"},
                    {"type": "web_fetch_20260209", "name": "web_fetch"}]


@dataclass
class TurnResult:
    status: str = "done"          # done | max_turns | refusal | cancelled | error
    usage: dict = field(default_factory=dict)
    model: str = ""


def _add(usage: dict, **counts) -> None:
    for k, v in counts.items():
        if v:
            usage[k] = usage.get(k, 0) + v


class ChatEngine:
    def __init__(self, spec: AgentSpec, toolbox: Toolbox, emit: Emit, cancel: threading.Event,
                 workdir: Path):
        self.spec = spec
        self.toolbox = toolbox
        self.emit = emit
        self.cancel = cancel
        self.workdir = workdir  # per-thread folder for CLI session files

    def turn(self, state: dict, system: str, user_text: str) -> TurnResult:
        raise NotImplementedError


# ------------------------------------------------------------------------ Claude API

class AnthropicChat(ChatEngine):
    def turn(self, state, system, user_text):
        import anthropic

        key = self.spec.provider.api_key()
        if not key:
            raise EngineError(f"provider '{self.spec.provider.name}': set {self.spec.provider.options.get('api_key_env')}")
        base_url = self.spec.provider.options.get("base_url")
        client = anthropic.Anthropic(api_key=key, base_url=base_url)
        model = self.spec.model or DEFAULT_ANTHROPIC_MODEL
        web = self.toolbox.recipe.web
        own = self.toolbox.specs(exclude={"web_fetch", "web_search"} if web else set())
        tools = [{"name": t.name, "description": t.description, "input_schema": t.schema} for t in own]
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        if web:
            tools += SERVER_WEB_TOOLS  # Anthropic runs search/fetch server-side
        request = {"model": model, "max_tokens": self.spec.max_tokens, "tools": tools,
                   "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                   "cache_control": {"type": "ephemeral"}}  # also cache the growing conversation
        if self.spec.effort:
            request["output_config"] = {"effort": self.spec.effort}
        use_fallback = model in FALLBACK_MODELS and not base_url and self.spec.provider.options.get("fallbacks", True)

        history = state.setdefault("messages", [])
        history.append({"role": "user", "content": user_text})
        usage: dict = {}
        for _ in range(self.spec.max_turns):
            if self.cancel.is_set():
                return TurnResult("cancelled", usage, model)
            try:
                stream_cm = (client.beta.messages.stream(**request, messages=history, betas=[FALLBACK_BETA],
                                                         fallbacks="default")
                             if use_fallback else client.messages.stream(**request, messages=history))
                with stream_cm as stream:
                    for event in stream:
                        if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                            self.emit({"type": "text", "delta": event.delta.text})
                        if self.cancel.is_set():
                            break
                    if self.cancel.is_set():
                        return TurnResult("cancelled", usage, model)
                    msg = stream.get_final_message()
            except anthropic.APIStatusError as e:
                raise EngineError(f"Anthropic API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise EngineError(f"cannot reach the Anthropic API: {e}") from e
            u = msg.usage
            _add(usage, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                 cache_read_tokens=getattr(u, "cache_read_input_tokens", 0),
                 cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0))
            history.append({"role": "assistant",
                            "content": [b.model_dump(mode="json", by_alias=True, exclude_none=True) for b in msg.content]})
            for b in msg.content:  # server-side web tool activity, for the UI
                if b.type == "server_tool_use":
                    self.emit({"type": "tool_call", "id": b.id, "name": b.name, "args": dict(b.input or {}),
                               "server": True})
            if msg.stop_reason == "refusal":
                self.emit({"type": "text", "delta": "\n\n(The model declined this request.)"})
                return TurnResult("refusal", usage, model)
            if msg.stop_reason == "pause_turn":
                continue  # a long server-side tool run; resume it
            uses = [b for b in msg.content if b.type == "tool_use"]
            if msg.stop_reason == "tool_use" and uses:
                results = []
                for b in uses:
                    out, err = self.toolbox.call(b.name, b.input, b.id)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err})
                history.append({"role": "user", "content": results})
                continue
            return TurnResult("done", usage, model)
        return TurnResult("max_turns", usage, model)


# ------------------------------------------------------------ OpenAI / open-weight

class OpenAIChat(ChatEngine):
    def turn(self, state, system, user_text):
        import openai

        opts = self.spec.provider.options
        key = self.spec.provider.api_key()
        if opts.get("api_key_env") and not key:
            raise EngineError(f"provider '{self.spec.provider.name}': set {opts['api_key_env']}")
        if not self.spec.model:
            raise EngineError(f"set a model for provider '{self.spec.provider.name}'")
        client = openai.OpenAI(api_key=key or "not-needed", base_url=opts.get("base_url"))
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.schema}} for t in self.toolbox.specs()]
        history = state.setdefault("messages", [])
        history.append({"role": "user", "content": user_text})
        extra = dict(opts.get("extra_body") or {})
        usage: dict = {}
        for _ in range(self.spec.max_turns):
            if self.cancel.is_set():
                return TurnResult("cancelled", usage, self.spec.model)
            kwargs = {"model": self.spec.model, "messages": [{"role": "system", "content": system}, *history],
                      "tools": tools, "stream": True}
            if not opts.get("base_url"):
                kwargs["stream_options"] = {"include_usage": True}  # not every compatible server knows it
            if extra:
                kwargs["extra_body"] = extra
            if self.spec.effort and not opts.get("base_url"):  # OpenAI reasoning models: low/medium/high
                kwargs["reasoning_effort"] = {"xhigh": "high", "max": "high"}.get(self.spec.effort, self.spec.effort)
            text, calls, finish = [], {}, None
            try:
                for chunk in client.chat.completions.create(**kwargs):
                    if chunk.usage:
                        _add(usage, input_tokens=chunk.usage.prompt_tokens, output_tokens=chunk.usage.completion_tokens)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta.content:
                        text.append(delta.content)
                        self.emit({"type": "text", "delta": delta.content})
                    for tc in delta.tool_calls or []:
                        c = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        c["id"] = tc.id or c["id"]
                        if tc.function:
                            c["name"] += tc.function.name or ""
                            c["arguments"] += tc.function.arguments or ""
                    finish = choice.finish_reason or finish
                    if self.cancel.is_set():
                        return TurnResult("cancelled", usage, self.spec.model)
            except openai.APIStatusError as e:
                raise EngineError(f"API error {e.status_code} from {opts.get('base_url', 'OpenAI')}: {e.message}") from e
            except openai.APIConnectionError as e:
                raise EngineError(f"cannot reach {opts.get('base_url', 'the OpenAI API')}: {e}") from e
            message = {"role": "assistant", "content": "".join(text) or None}
            if calls:
                message["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                                          "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                         for i, c in sorted(calls.items())]
            history.append(message)
            if not calls:
                return TurnResult("done", usage, self.spec.model)
            for tc in message["tool_calls"]:
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    out = "tool arguments were not valid JSON"
                else:
                    out, _ = self.toolbox.call(tc["function"]["name"], args, tc["id"])
                history.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        return TurnResult("max_turns", usage, self.spec.model)


# ------------------------------------------------- Claude subscription (Claude Code CLI)

class ToolboxMcp(McpServer):
    """MCP view of a toolbox, so a CLI agent uses the same tools (and approvals) as API agents."""

    def __init__(self, toolbox: Toolbox):
        super().__init__(toolbox.vault, Context(actor=toolbox.ctx.actor), actor_fixed=True)
        self.toolbox = toolbox

    def list_tools(self):
        return [{"name": t.name, "description": t.description, "inputSchema": t.schema} for t in self.toolbox.specs()]

    def has_tool(self, name):
        return name in self.toolbox.tools

    def run_tool(self, name, args):
        return self.toolbox.call(name, args)


class RunMcpServer:
    """A loopback HTTP MCP endpoint that lives for one turn, protected by a random token."""

    def __init__(self, toolbox: Toolbox):
        mcp, token = ToolboxMcp(toolbox), secrets.token_urlsafe(24)
        self.token = token

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, status, body=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {token}":
                    return self._reply(401, {"error": "unauthorized"})
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
                reply = mcp.handle(msg)
                self._reply(202 if reply is None else 200, reply)

            def do_GET(self):
                self._reply(405, {"error": "no server-initiated streams"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ClaudeCodeChat(ChatEngine):
    """Claude via the unmodified Claude Code CLI with your own subscription. One CLI session per
    thread (resumed each turn); its built-in tools are off except web search/fetch, and everything
    else goes through the toolbox over MCP."""

    def turn(self, state, system, user_text):
        cli = self.spec.provider.options.get("command") or shutil.which("claude")
        if not cli:
            raise EngineError("'claude' CLI not found")
        self.workdir.mkdir(parents=True, exist_ok=True)
        first = "session_id" not in state
        session = state.setdefault("session_id", str(uuid.uuid4()))
        web = self.toolbox.recipe.web
        mcp = RunMcpServer(self.toolbox)
        self._builtin_ids: set[str] = set()
        try:
            config = {"mcpServers": {"harness": {"type": "http", "url": mcp.url,
                                                 "headers": {"Authorization": f"Bearer {mcp.token}"}}}}
            cmd = [cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                   "--system-prompt", system, "--mcp-config", json.dumps(config), "--strict-mcp-config",
                   "--tools", "WebSearch,WebFetch" if web else "",
                   "--allowedTools", "mcp__harness" + (",WebSearch,WebFetch" if web else "")]
            cmd += ["--session-id", session] if first else ["--resume", session]
            if self.spec.model:
                cmd += ["--model", self.spec.model]
            if self.spec.effort:
                cmd += ["--effort", self.spec.effort]
            env = {**os.environ, "MCP_TOOL_TIMEOUT": "1000000"}  # approvals can take a while
            proc = subprocess.Popen(cmd, cwd=self.workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env)
            watchdog = threading.Thread(target=self._kill_on_cancel, args=(proc,), daemon=True)
            watchdog.start()
            proc.stdin.write(user_text)
            proc.stdin.close()
            result = None
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                result = self._handle(event) or result
            proc.wait()
            stderr = proc.stderr.read()
        finally:
            mcp.close()
        if self.cancel.is_set():
            return TurnResult("cancelled")
        if result is None:
            if first:
                state.pop("session_id", None)
            raise EngineError(f"claude CLI failed (exit {proc.returncode}): {stderr.strip()[-500:]}")
        if result.get("is_error") and "not logged in" in str(result.get("result", "")).lower():
            if first:
                state.pop("session_id", None)
            raise EngineError("Claude Code is not signed in: sign in under Accounts (bottom left), "
                              "or run `claude` and use /login")
        usage, model = self._turn_usage(state, result, first)
        status = "error" if result.get("is_error") else "done"
        if result.get("subtype") == "error_max_turns":
            status = "max_turns"
        if status == "error":
            self.emit({"type": "error", "message": str(result.get("result") or "the CLI reported an error")})
        return TurnResult(status, usage, model or self.spec.model or "")

    @staticmethod
    def _turn_usage(state: dict, result: dict, first: bool) -> tuple[dict, str]:
        """Usage of this turn only. The CLI reports per-model totals for the whole session, so take the
        difference to the previous turn's totals (kept in the thread state)."""
        fields = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                  "cacheReadInputTokens": "cache_read_tokens", "cacheCreationInputTokens": "cache_write_tokens",
                  "costUSD": "cost_usd"}
        current = result.get("modelUsage") or {}
        previous = {} if first else state.get("cli_model_usage", {})
        usage: dict = {}
        cost_by_model: dict[str, float] = {}
        for name, totals in current.items():
            before = previous.get(name, {})
            for src, dst in fields.items():
                delta = max(0, (totals.get(src) or 0) - (before.get(src) or 0))
                if delta:
                    usage[dst] = usage.get(dst, 0) + delta
            cost_by_model[name] = max(0.0, (totals.get("costUSD") or 0) - (before.get("costUSD") or 0))
        state["cli_model_usage"] = current
        if "cost_usd" in usage:
            usage["cost_usd"] = round(usage["cost_usd"], 4)
        # Claude Code also uses a small model for background chores; report the one that did the work.
        model = max(cost_by_model, key=cost_by_model.get) if cost_by_model else ""
        return usage, model

    def _kill_on_cancel(self, proc: subprocess.Popen) -> None:
        while proc.poll() is None:
            if self.cancel.wait(0.5):
                proc.kill()
                return

    def _handle(self, event: dict) -> dict | None:
        kind = event.get("type")
        if kind == "result":
            return event
        if event.get("parent_tool_use_id"):
            return None  # sub-agent chatter
        if kind == "stream_event":
            e = event.get("event") or {}
            if e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                self.emit({"type": "text", "delta": e["delta"]["text"]})
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                # Built-in web tools are reported here; toolbox tools report themselves.
                if block.get("type") == "tool_use" and not str(block.get("name", "")).startswith("mcp__"):
                    self._builtin_ids.add(block.get("id"))
                    self.emit({"type": "tool_call", "id": block.get("id"), "name": block.get("name"),
                               "args": block.get("input") or {}, "server": True})
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and \
                        block.get("tool_use_id") in self._builtin_ids:
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)[:2000]
                    self.emit({"type": "tool_result", "id": block.get("tool_use_id"), "name": "",
                               "is_error": bool(block.get("is_error")), "output": text[:2000], "server": True})
        return None


CHAT_ENGINES = {"anthropic": AnthropicChat, "openai": OpenAIChat, "claude_code": ClaudeCodeChat}
