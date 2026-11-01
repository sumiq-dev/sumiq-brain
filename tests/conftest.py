import subprocess

import pytest

from aibrain.scaffold import init_vault
from aibrain.tools import Context, call
from aibrain.vault import Vault


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "brain"
    init_vault(root, "test")
    return Vault(root)


@pytest.fixture
def run(vault):
    ctx = Context(actor="agent:test")

    def _run(tool, **args):
        return call(vault, tool, args, ctx)
    return _run


def git_log(vault):
    out = subprocess.run(["git", "log", "--format=%an|%s"], cwd=vault.root, capture_output=True, text=True)
    return out.stdout.strip().splitlines()
