"""Every call cotop makes into google-colab-cli. No other cotop module imports colab_cli.

The README's "Calls into the Colab CLI" names exactly what this module exports,
and tests/test_docs.py keeps the two equal. What these calls do inside the CLI
is the CLI's behavior; this file is where cotop decides what to hand it.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, get_args

from colab_cli.auth import get_credentials
from colab_cli.client import Client, Prod
from colab_cli.state import StateStore

__all__ = ["Command", "Google", "colab_argv", "named_sessions", "sign_in"]

Command = Literal["stop", "restart-kernel", "ssh"]

# The CLI's client logs request headers and bodies at DEBUG; keep tokens out of cotop's process.
logging.getLogger("colab_cli").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Google:
    """All cotop can do with the CLI's signed-in session; the session itself stays in sign_in."""

    get: Callable[..., str]  # (url, *, params, headers, timeout) -> body; raises on an HTTP error
    unassign: Callable[[str], None]  # stop a runtime by endpoint


def sign_in() -> Google:
    """The CLI's own sign-in; it prompts in the terminal if it has no token."""
    session = get_credentials()

    def get(url: str, *, params: dict, headers: dict, timeout: float) -> str:
        r = session.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text

    def unassign(endpoint: str) -> None:
        Client(Prod(), session).unassign(endpoint)

    return Google(get=get, unassign=unassign)


def named_sessions(path: Path) -> dict[str, dict]:
    """The CLI's named sessions, without their tokens, read under the CLI's own lock.

    None until the CLI first writes the file: before then its store fails,
    creating its lock before the directory the lock goes in.
    """
    if not path.exists():
        return {}
    return {
        name: {"name": s.name, "endpoint": s.endpoint, "running": s.running}
        for name, s in StateStore(str(path)).list().items()
    }


def colab_argv(command: Command, *args: str) -> list[str]:
    """A command line for the colab CLI installed with cotop, which matches the library above."""
    if command not in get_args(Command):
        raise ValueError(f"cotop does not run `colab {command}`")
    path = Path(sys.executable).with_name("colab")
    if not path.exists():
        raise FileNotFoundError(f"the colab CLI is missing beside {sys.executable}")
    return [str(path), command, *args]
