"""Access to Colab, signed in with the colab CLI's own credentials.

Reads never write the CLI's session file: the CLI's `sessions` and `status`
commands prune and rewrite local state, so this reads the store directly and
joins it with the server's assignments instead. Actions on a named runtime go
through the colab CLI itself, so its session file and history stay its own.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from colab_cli.auth import get_credentials
from colab_cli.client import Client, Prod
from colab_cli.state import StateStore

from cotop.model import History, Resources, Runtime, Status, parse_json, utc_now

COLAB = "https://colab.research.google.com"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
HEADERS = {"Accept": "application/json", "X-Colab-Client-Agent": "colab-cli"}
TIMEOUT = 15
RESOURCES_TIMEOUT = 3  # readings come every 2 s; one later than that is a gap
CLI_HOME = Path("~/.config/colab-cli").expanduser()
SESSIONS = CLI_HOME / "sessions.json"

# The CLI's client logs request headers and bodies at DEBUG; keep tokens out.
logging.getLogger("colab_cli").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


class Colab:
    def __init__(self, session, ssh_identity: str | None = None):
        self.session = session
        self.ssh_identity = ssh_identity

    @classmethod
    def sign_in(cls, ssh_identity: str | None = None) -> Colab:
        """Use the token the colab CLI stored; prompts for sign-in if there is none."""
        return cls(get_credentials(), ssh_identity)

    def _get(self, url: str, **params):
        r = self.session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return parse_json(r.text)

    def email(self) -> str:
        return self._get(USERINFO)["email"]

    def ccu_info(self) -> dict:
        return self._get(f"{COLAB}/tun/m/ccu-info", authuser="0")

    def assignments(self) -> list[dict]:
        return self._get(f"{COLAB}/tun/m/assignments", authuser="0").get("assignments", [])

    def stop(self, runtime: Runtime) -> str:
        if runtime.name is None:  # opened in a browser or on another host: no CLI name to stop
            Client(Prod(), self.session).unassign(runtime.endpoint)
            return f"Stopped {runtime.endpoint}."
        self.confirm_still_assigned(runtime)
        return _cli("stop", "-s", runtime.name)

    def restart_kernel(self, runtime: Runtime) -> str:
        self.confirm_still_assigned(runtime)
        return _cli("restart-kernel", "-s", runtime.name)

    def ssh_command(self, runtime: Runtime) -> list[str]:
        self.confirm_still_assigned(runtime)
        identity = ["-i", self.ssh_identity] if self.ssh_identity else []
        return [_colab(), "ssh", "-s", runtime.name, *identity]

    def confirm_still_assigned(self, runtime: Runtime) -> None:
        """Act on a name only while it still means this VM.

        The CLI resolves names, not endpoints: a name stopped and reused since
        the last poll would point the action at another VM, and `colab ssh`
        creates a new runtime for a name it does not know.
        """
        if not still_assigned(runtime, self.assignments(), self.sessions()):
            raise RuntimeGone(f"{runtime.name or runtime.endpoint} is no longer this runtime; refresh and retry")

    def sessions(self) -> dict[str, dict]:
        """The CLI's named sessions, without their tokens, read under the CLI's own lock.

        None until the CLI first writes the file: before then its store fails,
        creating its lock before the directory the lock goes in.
        """
        if not SESSIONS.exists():
            return {}
        return {
            name: {"name": s.name, "endpoint": s.endpoint, "running": s.running}
            for name, s in StateStore(str(SESSIONS)).list().items()
        }

    def history(self) -> History:
        return History.load(CLI_HOME / "history")

    def status(self, assignments: list[dict]) -> dict[str, Status | Exception]:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = pool.map(_runtime_status, assignments)
            return {a["endpoint"]: s for a, s in zip(assignments, results)}

    def resources(self, runtimes: list[Runtime]) -> dict[str, Resources | None]:
        """One reading per runtime; a failed read is None, a gap in the graph."""
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = pool.map(_runtime_resources, runtimes)
            return {r.endpoint: res for r, res in zip(runtimes, results)}


class RuntimeGone(Exception):
    pass


def still_assigned(runtime: Runtime, assignments: list[dict], local: dict[str, dict]) -> bool:
    if runtime.name is None:
        return False
    live = {a["endpoint"] for a in assignments}
    return runtime.endpoint in live and local.get(runtime.name, {}).get("endpoint") == runtime.endpoint


def _colab() -> str:
    """The colab CLI installed with cotop, so it matches the library cotop reads with."""
    path = Path(sys.executable).with_name("colab")
    if not path.exists():
        raise FileNotFoundError(f"the colab CLI is missing beside {sys.executable}")
    return str(path)


def _cli(*args: str) -> str:
    """Run a colab CLI command; its last output line is the result to show."""
    done = subprocess.run([_colab(), *args], capture_output=True, text=True, timeout=180)
    lines = (done.stdout + done.stderr).strip().splitlines()
    if done.returncode != 0:
        raise RuntimeError(lines[-1] if lines else f"colab {args[0]} exited {done.returncode}")
    return lines[-1] if lines else f"colab {args[0]} done"


def _proxy_get(url: str, token: str, path: str, timeout: float):
    headers = {**HEADERS, "X-Colab-Runtime-Proxy-Token": token}
    # Jupyter Server skips activity tracking for this parameter; whether Colab's
    # idle shutdown looks at it is not documented.
    r = requests.get(url.rstrip("/") + path, headers=headers, params={"no_track_activity": "1"}, timeout=timeout)
    r.raise_for_status()
    return parse_json(r.text)


def _runtime_status(assignment: dict) -> Status | Exception:
    proxy = assignment["runtimeProxyInfo"]
    try:
        return Status.parse(
            _proxy_get(proxy["url"], proxy["token"], "/api/status", TIMEOUT),
            _proxy_get(proxy["url"], proxy["token"], "/api/kernels", TIMEOUT),
        )
    except Exception as e:  # shown in the runtime's row, never swallowed
        return e


def _runtime_resources(runtime: Runtime) -> Resources | None:
    try:
        return Resources.parse(utc_now(), _proxy_get(runtime.url, runtime.token, "/api/colab/resources", RESOURCES_TIMEOUT))
    except Exception:  # a gap in the graph; the slow loop reports an unreachable runtime
        return None
