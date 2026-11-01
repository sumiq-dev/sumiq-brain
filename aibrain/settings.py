"""Platform settings: which model providers exist and which agent uses which model.

Lives outside the vault (machine-specific, may reference secrets via env vars), so the
vault stays portable. Default location: $BRAIN_CONFIG, else ~/.config/ai-brain/config.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROVIDER_KINDS = {
    "anthropic": "Claude via API key (Anthropic API)",
    "openai": "OpenAI API, or any OpenAI-compatible endpoint (Ollama, vLLM, llama.cpp, LM Studio, OpenRouter)",
    "claude_code": "Claude via your Claude subscription, through the official Claude Code CLI",
    "codex": "OpenAI models via your ChatGPT subscription, through the official Codex CLI",
}

TEMPLATE = """\
# AI Brain platform settings. Secrets are never written here: reference env vars instead.

providers:
  # Claude subscription (Pro/Max): runs the official Claude Code CLI.
  # Sign in once inside the container: `claude` then /login, or set CLAUDE_CODE_OAUTH_TOKEN
  # (from `claude setup-token`).
  claude-subscription:
    kind: claude_code

  # ChatGPT subscription (Plus/Pro): runs the official Codex CLI.
  # Sign in once inside the container: `codex login --device-auth`.
  chatgpt-subscription:
    kind: codex

  # Claude API, billed per token.
  anthropic:
    kind: anthropic
    api_key_env: ANTHROPIC_API_KEY

  # OpenAI API, billed per token.
  openai:
    kind: openai
    api_key_env: OPENAI_API_KEY

  # Open-weight models through any OpenAI-compatible server.
  local:
    kind: openai
    base_url: http://ollama:11434/v1

# Which provider and model each agent uses. "default" applies to every agent without its own entry.
agents:
  default:
    provider: claude-subscription
    model: opus
  # navigator:
  #   provider: local
  #   model: qwen3:32b
  # librarian:
  #   provider: anthropic
  #   model: claude-opus-5-5
  #   effort: medium
"""


class SettingsError(ValueError):
    pass


@dataclass
class Provider:
    name: str
    kind: str
    options: dict = field(default_factory=dict)

    def api_key(self) -> str | None:
        env = self.options.get("api_key_env")
        return os.environ.get(env) if env else None


@dataclass
class AgentSpec:
    role: str
    provider: Provider
    model: str | None = None
    effort: str | None = None
    max_turns: int = 40
    max_tokens: int = 16000
    timeout: int = 900  # seconds, for CLI providers


@dataclass
class Settings:
    providers: dict[str, Provider]
    agents: dict[str, dict]
    path: Path | None = None
    extras: dict = field(default_factory=dict)  # other top-level sections, e.g. web

    @staticmethod
    def default_path() -> Path:
        env = os.environ.get("BRAIN_CONFIG")
        return Path(env) if env else Path.home() / ".config" / "ai-brain" / "config.yaml"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = Path(path) if path else cls.default_path()
        if not path.exists():
            raise SettingsError(f"no settings file at {path}; create one with `brain config-template > {path}`")
        return cls.parse(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)

    @classmethod
    def parse(cls, data: dict, path: Path | None = None) -> "Settings":
        providers = {}
        for name, opts in (data.get("providers") or {}).items():
            opts = dict(opts or {})
            kind = opts.pop("kind", None)
            if kind not in PROVIDER_KINDS:
                raise SettingsError(f"provider '{name}': kind must be one of {', '.join(PROVIDER_KINDS)}")
            providers[name] = Provider(name, kind, opts)
        agents = {role: dict(spec or {}) for role, spec in (data.get("agents") or {}).items()}
        for role, spec in agents.items():
            if spec.get("provider") and spec["provider"] not in providers:
                raise SettingsError(f"agent '{role}' uses unknown provider '{spec['provider']}'")
        extras = {k: v for k, v in data.items() if k not in ("providers", "agents")}
        return cls(providers, agents, path, extras)

    def agent(self, role: str, provider: str | None = None, model: str | None = None) -> AgentSpec:
        """Resolve the spec for an agent role; explicit provider/model override the settings."""
        spec = {**self.agents.get("default", {}), **self.agents.get(role, {})}
        if provider:
            spec["provider"] = provider
            if not model and provider != (self.agents.get(role) or self.agents.get("default", {})).get("provider"):
                spec.pop("model", None)  # a model name rarely carries over between providers
        if model:
            spec["model"] = model
        name = spec.get("provider")
        if not name:
            raise SettingsError(f"no provider configured for agent '{role}' (and no default)")
        if name not in self.providers:
            raise SettingsError(f"unknown provider '{name}'; configured: {', '.join(self.providers)}")
        return AgentSpec(
            role=role, provider=self.providers[name], model=spec.get("model"), effort=spec.get("effort"),
            max_turns=int(spec.get("max_turns", 40)), max_tokens=int(spec.get("max_tokens", 16000)),
            timeout=int(spec.get("timeout", 900)),
        )
