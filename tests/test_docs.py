"""Only cotop.colab_cli_calls touches google-colab-cli, and the README says exactly what it hands over."""

import ast
import importlib.util
import inspect
import re
from dataclasses import replace
from pathlib import Path

import cotop
from cotop import colab, colab_cli_calls
from cotop.colab_cli_calls import Google
from tests.test_model import G4, LOCAL
from cotop.model import runtimes_from

SRC = Path(cotop.__file__).parent
HANDOFF = colab_cli_calls.__name__
README = SRC.parents[1] / "README.md"


def imported_modules(path: Path) -> list[tuple[str, list[str]]]:
    """(absolute module, names) for each import statement; `from p import x` also yields ("p.x", []).

    Ordinary import statements only, not importlib or __import__.
    """
    rel = path.relative_to(SRC.parent).with_suffix("")
    package = ".".join(rel.parts if path.name == "__init__.py" else rel.parts[:-1])
    package = package.removesuffix(".__init__")
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


def handoff_section() -> str:
    return README.read_text().split("### Calls into the Colab CLI")[1].split("\n### ")[0]


def test_the_readme_lists_exactly_what_the_handoff_module_exports():
    functions = {n for n in colab_cli_calls.__all__ if n not in {"Command", "Google"}}
    methods = {f"Google.{n}" for n, _ in inspect.getmembers(Google, inspect.isfunction) if not n.startswith("_")}
    listed = {m.group(1) for m in re.finditer(r"^\s*- `([\w.]+)", handoff_section(), re.M)}
    assert listed == functions | methods


def test_cotop_runs_exactly_the_command_lines_the_readme_lists(monkeypatch):
    """Through the actions themselves: stop, restart and ssh, with and without a key."""
    ran = []
    monkeypatch.setattr(colab.subprocess, "run", lambda argv, **kw: ran.append(argv) or type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(colab.Colab, "confirm_still_assigned", lambda self, r: None)
    [r] = runtimes_from([G4], LOCAL, {})
    r = replace(r, name="NAME")
    colab.Colab(None).stop(r)
    colab.Colab(None).restart_kernel(r)
    ran.append(colab.Colab(None).ssh_command(r))
    ran.append(colab.Colab(None, ssh_identity="KEY").ssh_command(r))
    run = {" ".join(["colab", *argv[1:]]) for argv in ran}

    listed = set()
    for line in re.findall(r"`(colab [^`]+)`", handoff_section()):
        optional = re.search(r" \[([^\]]+)\]", line)
        listed |= {line.replace(optional.group(0), ""), line.replace(optional.group(0), " " + optional.group(1))} if optional else {line}
    assert run == listed
