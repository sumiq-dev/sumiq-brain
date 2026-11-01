"""Agent recipes: saved setups (instructions, model, allowed tools, project folders).

Every recipe has full access to the brain. Recipes only decide what an agent may do outside it:
which folders it may touch, whether it may run commands, whether it may use the web.
Recipes live as YAML files in <config dir>/recipes/; "standard" is built in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

STANDARD = "standard"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RecipeError(ValueError):
    pass


@dataclass
class Recipe:
    id: str
    name: str
    description: str = ""
    instructions: str = ""
    provider: str | None = None     # None: the "chat" agent setting (or the default)
    model: str | None = None
    effort: str | None = None
    files: bool = False             # read/write files in `workspaces`
    shell: bool = False             # run commands in `workspaces` (with approval)
    web: bool = True                # search and fetch web pages
    workspaces: list[Path] = field(default_factory=list)
    shell_allow: list[str] = field(default_factory=list)  # commands that need no approval
    approve_writes: bool = True     # file writes need approval

    @classmethod
    def from_dict(cls, recipe_id: str, data: dict) -> "Recipe":
        if not ID_RE.match(recipe_id):
            raise RecipeError(f"recipe id '{recipe_id}' must be lowercase kebab-case")
        tools = data.get("tools") or {}
        unknown = set(tools) - {"files", "shell", "web"}
        if unknown:
            raise RecipeError(f"recipe '{recipe_id}': unknown tools {sorted(unknown)}; use files, shell, web")
        workspaces = [Path(p).expanduser().resolve() for p in data.get("workspaces") or []]
        recipe = cls(
            id=recipe_id, name=str(data.get("name") or recipe_id),
            description=str(data.get("description") or ""),
            instructions=str(data.get("instructions") or ""),
            provider=data.get("provider"), model=data.get("model"), effort=data.get("effort"),
            files=bool(tools.get("files", False)), shell=bool(tools.get("shell", False)),
            web=bool(tools.get("web", True)), workspaces=workspaces,
            shell_allow=[str(c) for c in data.get("shell_allow") or []],
            approve_writes=bool(data.get("approve_writes", True)),
        )
        if (recipe.files or recipe.shell) and not workspaces:
            raise RecipeError(f"recipe '{recipe_id}': files/shell tools need at least one workspace folder")
        return recipe

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "provider": self.provider, "model": self.model,
                "tools": {"brain": True, "files": self.files, "shell": self.shell, "web": self.web},
                "workspaces": [str(p) for p in self.workspaces]}


def standard_recipe() -> Recipe:
    return Recipe(id=STANDARD, name="Standard",
                  description="Chat with the default model. Full brain access, web on, no files or commands.")


def load_recipes(directory: Path | None) -> dict[str, Recipe]:
    """Built-in standard recipe plus every <id>.yaml in `directory` (a file can override 'standard')."""
    recipes = {STANDARD: standard_recipe()}
    if directory and directory.is_dir():
        for path in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise RecipeError(f"{path}: must be a YAML mapping")
            recipes[path.stem] = Recipe.from_dict(path.stem, data)
    return recipes


EXAMPLE = """\
# Example agent recipe. Copy to <config dir>/recipes/<id>.yaml and adjust.
name: Game designer
description: Works on the Kings Raid style gacha RPG.
instructions: |
  You help design and build a mobile gacha RPG inspired by King's Raid.
  Check the brain for existing design decisions before proposing changes.
provider: claude-subscription   # optional; defaults to the "chat" agent setting
model: sonnet                   # optional
tools:
  files: true                   # read/write files in the workspaces below
  shell: true                   # run commands there (each needs your approval unless allowed below)
  web: true
workspaces:
  - ~/repos/my-game
shell_allow:                    # exact command prefixes that run without asking
  - git status
  - git diff
approve_writes: true            # ask before writing or editing files
"""
