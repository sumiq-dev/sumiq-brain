"""Engines run one agent task on one kind of model provider.

Two families:
- API engines (anthropic, openai): our own agent loop; brain tools run in-process.
- CLI engines (claude_code, codex): the official vendor CLI runs the loop with your
  subscription sign-in, and reaches the brain tools through our MCP server.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .settings import AgentSpec, SettingsError
from .tools import TOOLS, Context, call
from .vault import Vault, VaultError

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`) on the Claude API.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


@dataclass
class Task:
    role: str             # agent role; changes are recorded as agent:<role>
    system: str
    prompt: str
    tools: list[str]      # brain tools this task may use


@dataclass
class Result:
    text: str
    status: str = "done"  # done | max_turns | max_tokens | refusal | error
    turns: int = 0
    tool_calls: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class EngineError(RuntimeError):
    pass


def _add_usage(total: dict, **counts) -> None:
    for k, v in counts.items():
        if v:
            total[k] = total.get(k, 0) + v


class ToolRunner:
    """Executes brain tool calls for API engines, limited to the task's tools."""

    def __init__(self, vault: Vault, task: Task, import_roots: list[Path]):
        self.vault = vault
        self.allowed = set(task.tools)
        self.ctx = Context(actor=f"agent:{task.role}", import_roots=import_roots)
        self.calls: list[str] = []

    def schemas(self) -> list:
        return [TOOLS[name] for name in TOOLS if name in self.allowed]

    def run(self, name: str, args) -> tuple[str, bool]:
        self.calls.append(name)
        if name not in self.allowed:
            return f"tool '{name}' is not available to this agent", True
        if not isinstance(args, dict):
            return "tool arguments must be a JSON object", True
        try:
            return call(self.vault, name, args, self.ctx), False
        except VaultError as e:
            return str(e), True
        except Exception as e:  # a broken tool call must not end the agent's run
            return f"internal error: {type(e).__name__}: {e}", True


class Engine:
    def __init__(self, spec: AgentSpec, vault: Vault, import_roots: list[Path] | None = None):
        self.spec = spec
        self.vault = vault
        self.import_roots = import_roots or []

    def run(self, task: Task) -> Result:
        raise NotImplementedError


# ------------------------------------------------------------------- API engines

class AnthropicEngine(Engine):
    """Claude via API key, with our own tool-use loop."""

    def run(self, task: Task) -> Result:
        import anthropic

        key = self.spec.provider.api_key()
        if not key:
            raise EngineError(f"provider '{self.spec.provider.name}': set {self.spec.provider.options.get('api_key_env')}")
        base_url = self.spec.provider.options.get("base_url")
        client = anthropic.Anthropic(api_key=key, base_url=base_url)
        model = self.spec.model or DEFAULT_ANTHROPIC_MODEL
        runner = ToolRunner(self.vault, task, self.import_roots)

        tools = [{"name": t.name, "description": t.description, "input_schema": t.schema} for t in runner.schemas()]
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}  # tools + system form a stable, cached prefix
        request = {
            "model": model, "max_tokens": self.spec.max_tokens, "tools": tools,
            "system": [{"type": "text", "text": task.system, "cache_control": {"type": "ephemeral"}}],
        }
        if self.spec.effort:
            request["output_config"] = {"effort": self.spec.effort}
        use_fallback = model in FALLBACK_MODELS and not base_url and self.spec.provider.options.get("fallbacks", True)

        messages = [{"role": "user", "content": task.prompt}]
        usage: dict = {}
        for turn in range(1, self.spec.max_turns + 1):
            try:
                if use_fallback:
                    resp = client.beta.messages.create(**request, messages=messages,
                                                       betas=[FALLBACK_BETA], fallbacks="default")
                else:
                    resp = client.messages.create(**request, messages=messages)
            except anthropic.APIStatusError as e:
                raise EngineError(f"Anthropic API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise EngineError(f"cannot reach the Anthropic API: {e}") from e
            u = resp.usage
            _add_usage(usage, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                       cache_read_tokens=getattr(u, "cache_read_input_tokens", 0),
                       cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0))
            messages.append({"role": "assistant", "content": resp.content})
            text = "\n".join(b.text for b in resp.content if b.type == "text").strip()

            if resp.stop_reason == "refusal":
                return Result(text or "the model declined this request", "refusal", turn, runner.calls, usage)
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason == "tool_use" and uses:
                results = []
                for b in uses:  # all results go back in one message so parallel calls keep working
                    out, is_error = runner.run(b.name, b.input)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": is_error})
                messages.append({"role": "user", "content": results})
                continue
            status = "max_tokens" if resp.stop_reason == "max_tokens" else "done"
            return Result(text, status, turn, runner.calls, usage)
        return Result("stopped: reached the turn limit", "max_turns", self.spec.max_turns, runner.calls, usage)


class OpenAIEngine(Engine):
    """OpenAI API or any OpenAI-compatible server (open-weight models), with our own tool loop."""

    def run(self, task: Task) -> Result:
        import openai

        opts = self.spec.provider.options
        key = self.spec.provider.api_key()
        if opts.get("api_key_env") and not key:
            raise EngineError(f"provider '{self.spec.provider.name}': set {opts['api_key_env']}")
        if not self.spec.model:
            raise EngineError(f"agent '{task.role}': set a model for provider '{self.spec.provider.name}'")
        client = openai.OpenAI(api_key=key or "not-needed", base_url=opts.get("base_url"))
        runner = ToolRunner(self.vault, task, self.import_roots)
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.schema}} for t in runner.schemas()]
        extra = dict(opts.get("extra_body") or {})
        messages: list = [{"role": "system", "content": task.system}, {"role": "user", "content": task.prompt}]
        usage: dict = {}
        for turn in range(1, self.spec.max_turns + 1):
            try:
                resp = client.chat.completions.create(model=self.spec.model, messages=messages,
                                                      tools=tools or openai.NOT_GIVEN, extra_body=extra or None)
            except openai.APIStatusError as e:
                raise EngineError(f"API error {e.status_code} from {opts.get('base_url', 'OpenAI')}: {e.message}") from e
            except openai.APIConnectionError as e:
                raise EngineError(f"cannot reach {opts.get('base_url', 'the OpenAI API')}: {e}") from e
            if resp.usage:
                _add_usage(usage, input_tokens=resp.usage.prompt_tokens, output_tokens=resp.usage.completion_tokens)
            choice = resp.choices[0]
            msg = choice.message
            messages.append(msg.model_dump(exclude_none=True))
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        out, _ = "tool arguments were not valid JSON", True
                    else:
                        out, _ = runner.run(tc.function.name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                continue
            status = "max_tokens" if choice.finish_reason == "length" else "done"
            return Result((msg.content or "").strip(), status, turn, runner.calls, usage)
        return Result("stopped: reached the turn limit", "max_turns", self.spec.max_turns, runner.calls, usage)


# ------------------------------------------------------------------- CLI engines

class CliEngine(Engine):
    """Base for engines that drive an official vendor CLI with the brain as an MCP server."""

    binary = ""

    def _cli(self) -> str:
        path = self.spec.provider.options.get("command") or shutil.which(self.binary)
        if not path:
            raise EngineError(f"'{self.binary}' CLI not found; install it or set 'command' for provider "
                              f"'{self.spec.provider.name}'")
        return path

    def _serve_args(self, task: Task) -> list[str]:
        args = ["-m", "aibrain.cli", "serve", str(self.vault.root), "--actor", f"agent:{task.role}",
                "--tools", ",".join(task.tools)]
        for root in self.import_roots:
            args += ["--import-root", str(root)]
        return args

    def _exec(self, cmd: list[str], prompt: str, cwd: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd,
                                  timeout=self.spec.timeout)
        except subprocess.TimeoutExpired as e:
            raise EngineError(f"{self.binary} did not finish within {self.spec.timeout}s") from e


class ClaudeCodeEngine(CliEngine):
    """Claude through the unmodified Claude Code CLI, signed in with your own Claude subscription
    (or an API key). The CLI runs the agent loop; only the brain tools are enabled."""

    binary = "claude"

    def run(self, task: Task) -> Result:
        mcp = {"mcpServers": {"brain": {"command": sys.executable, "args": self._serve_args(task)}}}
        cmd = [self._cli(), "-p", "--output-format", "json", "--no-session-persistence",
               "--system-prompt", task.system,
               "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
               "--tools", "",  # no built-in tools (shell, files, web): the brain tools are all it gets
               "--allowedTools", ",".join(f"mcp__brain__{t}" for t in task.tools)]
        if self.spec.model:
            cmd += ["--model", self.spec.model]
        with tempfile.TemporaryDirectory(prefix="brain-claude-") as cwd:
            proc = self._exec(cmd, task.prompt, cwd)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            detail = (proc.stderr or proc.stdout).strip()[-500:]
            raise EngineError(f"claude CLI failed (exit {proc.returncode}): {detail}") from e
        u = out.get("usage") or {}
        usage = {k: v for k, v in {
            "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
            "cache_read_tokens": u.get("cache_read_input_tokens"),
            "cache_write_tokens": u.get("cache_creation_input_tokens"),
        }.items() if v}
        if out.get("is_error") and "not logged in" in str(out.get("result", "")).lower():
            raise EngineError("Claude Code is not signed in: run `claude` and use /login (in Docker: "
                              "`docker compose exec -u brain brain claude`), or set CLAUDE_CODE_OAUTH_TOKEN")
        status = "error" if out.get("is_error") else "done"
        if out.get("subtype") == "error_max_turns":
            status = "max_turns"
        return Result(str(out.get("result") or "").strip(), status, int(out.get("num_turns") or 0), [], usage)


def _toml(value) -> str:
    return json.dumps(value)  # JSON strings and string arrays are valid TOML


class CodexEngine(CliEngine):
    """OpenAI models through the unmodified Codex CLI, signed in with your ChatGPT subscription
    (or an API key). The CLI runs the agent loop in a read-only sandbox in an empty folder."""

    binary = "codex"

    def run(self, task: Task) -> Result:
        cli = self._cli()
        status = subprocess.run([cli, "login", "status"], capture_output=True, text=True, timeout=60)
        if status.returncode != 0:
            raise EngineError("Codex is not signed in: run `codex login --device-auth` (in Docker: "
                              "`docker compose exec -u brain brain codex login --device-auth`)")
        serve = self._serve_args(task)
        with tempfile.TemporaryDirectory(prefix="brain-codex-") as cwd:
            last = Path(cwd) / "last-message.txt"
            cmd = [cli, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                   "--sandbox", "read-only", "--color", "never", "--json", "-C", cwd, "-o", str(last),
                   "-c", f"mcp_servers.brain.command={_toml(sys.executable)}",
                   "-c", f"mcp_servers.brain.args={_toml(serve)}",
                   "-c", f"mcp_servers.brain.default_tools_approval_mode={_toml('approve')}"]
            if self.spec.model:
                cmd += ["-m", self.spec.model]
            cmd.append("-")  # prompt from stdin
            prompt = f"{task.system}\n\n---\n\n{task.prompt}"
            proc = self._exec(cmd, prompt, cwd)
            text = last.read_text(encoding="utf-8").strip() if last.exists() else ""

        calls, usage, errors = [], {}, []
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
                calls.append(item.get("tool", ""))
            if event.get("type") == "turn.completed":
                u = event.get("usage") or {}
                _add_usage(usage, input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                           cache_read_tokens=u.get("cached_input_tokens"))
            if event.get("type") in ("error", "turn.failed"):
                errors.append(str(event.get("message") or event.get("error") or event))
        if proc.returncode != 0 and not text:
            detail = (errors[-1] if errors else (proc.stderr or proc.stdout).strip())[-500:]
            raise EngineError(f"codex CLI failed (exit {proc.returncode}): {detail}")
        return Result(text, "error" if errors and not text else "done", 0, calls, usage)


ENGINES = {"anthropic": AnthropicEngine, "openai": OpenAIEngine,
           "claude_code": ClaudeCodeEngine, "codex": CodexEngine}


def engine_for(spec: AgentSpec, vault: Vault, import_roots: list[Path] | None = None) -> Engine:
    cls = ENGINES.get(spec.provider.kind)
    if cls is None:
        raise SettingsError(f"no engine for provider kind '{spec.provider.kind}'")
    return cls(spec, vault, import_roots)
