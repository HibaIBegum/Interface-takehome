"""Structural rules: LLM isolation and the single policy chokepoint."""

import ast
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import cua

ROOT = Path(__file__).resolve().parents[1]
LLM_PACKAGE = "cua.agent"
CHOKEPOINT = ROOT / "cua" / "policy" / "gate.py"


def _modules_outside_agent() -> list[str]:
    names = [m.name for m in pkgutil.walk_packages(cua.__path__, "cua.")]
    return sorted(n for n in names if n != LLM_PACKAGE and not n.startswith(LLM_PACKAGE + "."))


@pytest.mark.parametrize("module", _modules_outside_agent())
def test_llm_sdk_is_not_imported_outside_agent(module):
    """Imports each module in a fresh interpreter and checks anthropic was not loaded, even transitively."""
    probe = (f"import importlib, sys; importlib.import_module({module!r}); "
             "print(any(m == 'anthropic' or m.startswith('anthropic.') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", f"{module} pulls in the anthropic SDK"


def test_replay_package_is_covered_once_it_exists():
    # Guard against the isolation test silently skipping replay because of a naming change.
    if (ROOT / "cua" / "replay").exists():
        assert any(m.startswith("cua.replay") for m in _modules_outside_agent())


def test_only_the_policy_gate_calls_surface_act():
    offenders = []
    for path in (ROOT / "cua").rglob("*.py"):
        if path == CHOKEPOINT or "surface" in path.relative_to(ROOT / "cua").parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "act":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], f"Surface.act() called outside the policy gate: {offenders}"


def test_replay_source_never_imports_the_agent_or_llm_sdk():
    for path in (ROOT / "cua" / "replay").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("cua.agent", "anthropic")), f"{path.name} imports {name}"
