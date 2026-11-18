"""Only cotop.colab_cli_calls touches google-colab-cli, and the README lists what it hands over."""

import ast
import re
from dataclasses import fields
from pathlib import Path
from typing import get_args

from cotop import colab_cli_calls
from cotop.colab_cli_calls import Command, Google

SRC = Path(colab_cli_calls.__file__).parent
HANDOFF = Path(colab_cli_calls.__file__).stem
README = SRC.parents[1] / "README.md"


def imports(path: Path) -> list[tuple[str, list[str]]]:
    """(module, names) for every import in a file; `import a.b` gives ("a.b", [])."""
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out += [(a.name, []) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.module or "", [a.name for a in node.names]))
    return out


def test_only_the_handoff_module_imports_colab_cli():
    for f in SRC.glob("*.py"):
        if f.stem == HANDOFF:
            continue
        for module, names in imports(f):
            assert module.split(".")[0] != "colab_cli", f"{f.name} imports {module}"
            if module == f"cotop.{HANDOFF}":
                assert names and set(names) <= set(colab_cli_calls.__all__), f"{f.name} takes {names} from it"
            if module == "cotop":
                assert HANDOFF not in names, f"{f.name} imports the module, not its exports"


def handoff_section() -> str:
    return README.read_text().split("### Calls into the Colab CLI")[1].split("\n### ")[0]


def test_the_readme_lists_exactly_what_the_handoff_module_exports():
    functions = {n for n in colab_cli_calls.__all__ if n not in {"Command", "Google"}}
    exported = functions | {f"Google.{f.name}" for f in fields(Google)}
    listed = {m.group(1) for m in re.finditer(r"^\s*- `([\w.]+)", handoff_section(), re.M)}
    assert listed == exported


def test_the_readme_lists_exactly_the_colab_commands_cotop_can_run():
    listed = set(re.findall(r"`colab ([a-z-]+)", handoff_section()))
    assert listed == set(get_args(Command))
