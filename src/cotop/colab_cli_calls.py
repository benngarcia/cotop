"""Every call cotop makes into google-colab-cli. No other cotop module imports colab_cli.

tests/test_handoff.py pins what it exports and the command lines it builds.
What these calls do inside the CLI is the CLI's behavior; this file is where
cotop decides what to hand it.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Literal, get_args

from colab_cli.auth import get_credentials
from colab_cli.client import Client, Prod
from colab_cli.state import StateStore

__all__ = ["Command", "Google", "colab_argv", "named_sessions", "sign_in"]

Command = Literal["stop", "restart-kernel", "ssh"]

# The CLI's client logs request headers and bodies at DEBUG; keep tokens out of cotop's process.
logging.getLogger("colab_cli").setLevel(logging.WARNING)


class Google:
    """The CLI's signed-in session, as cotop may use it: GET, and stop a runtime."""

    def __init__(self, session):
        self._session = session

    def get(self, url: str, *, params: dict, headers: dict, timeout: float) -> str:
        r = self._session.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text

    def unassign(self, endpoint: str) -> None:
        Client(Prod(), self._session).unassign(endpoint)


def sign_in() -> Google:
    """The CLI's own sign-in; it prompts in the terminal if it has no token."""
    return Google(get_credentials())


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


def colab_argv(command: Command, name: str, identity: str | None = None) -> list[str]:
    """The whole command line for the colab CLI installed with cotop, which matches the library above."""
    if command not in get_args(Command):
        raise ValueError(f"cotop does not run `colab {command}`")
    if identity and command != "ssh":
        raise ValueError(f"`colab {command}` takes no SSH key")
    path = Path(sys.executable).with_name("colab")
    if not path.exists():
        raise FileNotFoundError(f"the colab CLI is missing beside {sys.executable}")
    return [str(path), command, "-s", name, *(["-i", identity] if identity else [])]
