"""Only cotop.colab_cli_calls touches google-colab-cli, and it hands over exactly these calls."""

import ast
import importlib.util
import inspect
from dataclasses import replace
from pathlib import Path

import pytest

import cotop
from cotop import colab, colab_cli_calls
from cotop.colab_cli_calls import Google
from cotop.model import runtimes_from
from tests.test_model import G4, LOCAL

SRC = Path(cotop.__file__).parent
HANDOFF = colab_cli_calls.__name__


def imported_modules(path: Path) -> list[tuple[str, list[str]]]:
    """(absolute module, names) for each import statement; `from p import x` also yields ("p.x", []).

    Ordinary import statements only, not importlib or __import__.
    """
    rel = path.relative_to(SRC.parent).with_suffix("")
    package = ".".join(rel.parts if path.name == "__init__.py" else rel.parts[:-1]).removesuffix(".__init__")
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out += [(a.name, []) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            names = [a.name for a in node.names]
            out.append((module, names))
            out += [(f"{module}.{n}", []) for n in names]
    return out


def test_only_the_handoff_module_imports_colab_cli_or_touches_the_session():
    for f in SRC.rglob("*.py"):
        if f == Path(colab_cli_calls.__file__):
            continue
        tree = ast.parse(f.read_text())
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "_session"], f"{f.name} reaches the session"
        for module, names in imported_modules(f):
            assert module.split(".")[0] != "colab_cli", f"{f.name} imports {module}"
            if module == HANDOFF:
                assert names and set(names) <= set(colab_cli_calls.__all__), f"{f.name} takes {names or 'the module'} from it"


def test_the_handoff_module_exports_exactly_these():
    assert set(colab_cli_calls.__all__) == {"Command", "Google", "colab_argv", "named_sessions", "sign_in"}
    assert {n for n, _ in inspect.getmembers(Google, inspect.isfunction) if not n.startswith("_")} == {"get", "unassign"}


@pytest.fixture
def ran(monkeypatch):
    """Command lines cotop runs, with the colab path shown as `colab`; the name check passes."""
    lines = []
    done = type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(colab.subprocess, "run", lambda argv, **kw: lines.append(argv) or done)
    monkeypatch.setattr(colab.Colab, "assignments", lambda self: [G4])
    monkeypatch.setattr(colab.Colab, "sessions", lambda self: {"NAME": {"name": "NAME", "endpoint": G4["endpoint"]}})
    return lines


def runtime(name="NAME"):
    [r] = runtimes_from([G4], LOCAL, {})
    return replace(r, name=name)


def test_cotop_runs_exactly_these_command_lines(ran):
    colab.Colab(None).stop(runtime())
    colab.Colab(None).restart_kernel(runtime())
    ran.append(colab.Colab(None).ssh_command(runtime()))
    ran.append(colab.Colab(None, ssh_identity="KEY").ssh_command(runtime()))
    assert [" ".join(["colab", *argv[1:]]) for argv in ran] == [
        "colab stop -s NAME",
        "colab restart-kernel -s NAME",
        "colab ssh -s NAME",
        "colab ssh -s NAME -i KEY",
    ]


@pytest.mark.parametrize("act", ["stop", "restart_kernel", "ssh_command"])
def test_every_named_action_checks_the_name_still_means_this_runtime(ran, monkeypatch, act):
    """A name stopped and reused since the last poll must not send the action to another VM."""
    monkeypatch.setattr(colab.Colab, "sessions", lambda self: {"NAME": {"name": "NAME", "endpoint": "gpu-reused"}})
    with pytest.raises(colab.RuntimeGone):
        getattr(colab.Colab(None), act)(runtime())
    assert ran == []


def test_colab_argv_refuses_other_commands_and_keys_outside_ssh():
    with pytest.raises(ValueError, match="does not run"):
        colab_cli_calls.colab_argv("new", "NAME")
    with pytest.raises(ValueError, match="takes no SSH key"):
        colab_cli_calls.colab_argv("stop", "NAME", "KEY")
