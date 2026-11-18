"""The README's list of calls into the Colab CLI is the code's list."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def calls_in_code() -> tuple[set[str], set[str]]:
    """colab_cli names cotop imports, and the colab subcommands it runs."""
    imported, commands = set(), set()
    for f in (ROOT / "src" / "cotop").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "colab_cli":
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names if a.name.split(".")[0] == "colab_cli"}
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_cli":
                commands |= {a.value for a in node.args[:1] if isinstance(a, ast.Constant)}
            elif isinstance(node, ast.List) and node.elts and getattr(getattr(node.elts[0], "func", None), "id", None) == "_colab":
                commands |= {a.value for a in node.elts[1:2] if isinstance(a, ast.Constant)}
    return imported, commands


def calls_in_readme() -> tuple[set[str], set[str], set[str]]:
    section = (ROOT / "README.md").read_text().split("### Calls into the Colab CLI")[1].split("\n### ")[0]
    spans = re.findall(r"`([^`]+)`", section)
    callees = {m.group(1) for s in spans if (m := re.match(r"(\w+)\(", s))}
    named = {n for s in spans for n in re.findall(r"\b(\w+)\(", s)}
    commands = {m.group(1) for s in spans if (m := re.match(r"colab ([a-z-]+)", s))}
    return callees, named, commands


def test_the_readme_names_every_call_into_the_colab_cli():
    imported, run = calls_in_code()
    callees, named, listed = calls_in_readme()
    assert imported <= named  # every library name cotop imports appears in the list
    assert callees <= imported  # and the list names no call cotop doesn't make
    assert run == listed
