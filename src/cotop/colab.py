"""Access to Colab, signed in with the colab CLI's own credentials.

Reads never write the CLI's session file: the CLI's `sessions` and `status`
commands prune and rewrite local state, so this reads the store directly and
joins it with the server's assignments instead. Actions on a named runtime go
through the colab CLI itself, so its session file and history stay its own.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

import requests

from cotop.colab_cli_calls import Command, Google, colab_argv, named_sessions
from cotop.colab_cli_calls import sign_in as cli_sign_in
from cotop.model import History, Resources, Runtime, Status, parse_json, utc_now

COLAB = "https://colab.research.google.com"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
HEADERS = {"Accept": "application/json", "X-Colab-Client-Agent": "colab-cli"}
TIMEOUT = 15
RESOURCES_TIMEOUT = 3  # readings come every 2 s; one that takes longer than this is a gap
CLI_HOME = Path("~/.config/colab-cli").expanduser()
SESSIONS = CLI_HOME / "sessions.json"

logging.getLogger("urllib3").setLevel(logging.WARNING)


class Colab:
    def __init__(self, google: Google | None, ssh_identity: str | None = None):
        self.google = google
        self.ssh_identity = ssh_identity

    @classmethod
    def sign_in(cls, ssh_identity: str | None = None) -> Colab:
        """Use the token the colab CLI stored; prompts for sign-in if there is none."""
        return cls(cli_sign_in(), ssh_identity)

    def _get(self, url: str, **params):
        return parse_json(self.google.get(url, params=params, headers=HEADERS, timeout=TIMEOUT))

    def email(self) -> str:
        return self._get(USERINFO)["email"]

    def ccu_info(self) -> dict:
        return self._get(f"{COLAB}/tun/m/ccu-info", authuser="0")

    def assignments(self) -> list[dict]:
        return self._get(f"{COLAB}/tun/m/assignments", authuser="0").get("assignments", [])

    def stop(self, runtime: Runtime) -> str:
        if runtime.name is None:  # opened in a browser or on another host: no CLI name to stop
            self.google.unassign(runtime.endpoint)
            return f"Stopped {runtime.endpoint}."
        self.confirm_still_assigned(runtime)
        return _cli("stop", runtime.name)

    def restart_kernel(self, runtime: Runtime) -> str:
        self.confirm_still_assigned(runtime)
        return _cli("restart-kernel", runtime.name)

    def ssh_command(self, runtime: Runtime) -> list[str]:
        self.confirm_still_assigned(runtime)
        return colab_argv("ssh", runtime.name, self.ssh_identity)

    def confirm_still_assigned(self, runtime: Runtime) -> None:
        """Act on a name only while it still means this VM.

        The CLI resolves names, not endpoints: a name stopped and reused since
        the last poll would point the action at another VM, and `colab ssh`
        creates a new runtime for a name it does not know.
        """
        if not still_assigned(runtime, self.assignments(), self.sessions()):
            raise RuntimeGone(f"{runtime.name or runtime.endpoint} is no longer this runtime; refresh and retry")

    def sessions(self) -> dict[str, dict]:
        return named_sessions(SESSIONS)

    def history(self) -> History:
        return History.load(CLI_HOME / "history")

    def status(self, assignments: list[dict]) -> dict[str, Status | Exception]:
        """Every runtime at once, each on a daemon thread: a pool's threads would hold up quitting until their reads return."""
        statuses = {}
        readers = [threading.Thread(target=lambda a=a: statuses.update({a["endpoint"]: _runtime_status(a)}), daemon=True) for a in assignments]
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join()
        return statuses

    def resources(self, runtime: Runtime) -> Resources | Exception:
        """One reading of one runtime; a failed or late read is its error, a gap in the graph."""
        try:
            return Resources.parse(_proxy_get(runtime.url, runtime.token, "/api/colab/resources", RESOURCES_TIMEOUT))
        except Exception as e:  # shown once reads have failed for a while, never swallowed
            return e


class RuntimeGone(Exception):
    pass


def still_assigned(runtime: Runtime, assignments: list[dict], local: dict[str, dict]) -> bool:
    if runtime.name is None:
        return False
    live = {a["endpoint"] for a in assignments}
    return runtime.endpoint in live and local.get(runtime.name, {}).get("endpoint") == runtime.endpoint


def _cli(command: Command, name: str) -> str:
    """Run a colab CLI command with no stdin, so a prompt fails instead of waiting unseen behind the dashboard.

    The CLI says what happened on stdout; stderr holds warnings, or the traceback when it crashes.
    """
    done = subprocess.run(colab_argv(command, name), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    out, err = done.stdout.strip().splitlines(), done.stderr.strip().splitlines()
    if done.returncode != 0 or (out and out[-1].endswith("not found.")):  # `colab stop` exits 0 for a name it doesn't know
        raise RuntimeError((out or err or [f"colab {command} exited {done.returncode}"])[-1])
    return out[-1] if out else f"colab {command} done"


def _proxy_get(url: str, token: str, path: str, timeout: float):
    """GET a path on a runtime's proxy; a reply not complete within timeout seconds raises.

    requests applies a timeout to each socket wait, so a proxy that trickles
    its reply never trips it. The body is read a chunk at a time and cut off
    at the deadline. Headers can't be: requests returns only once they are
    all in, so a reply whose headers trickle raises when they finish.
    """
    deadline = time.monotonic() + timeout
    headers = {**HEADERS, "X-Colab-Runtime-Proxy-Token": token}
    # Jupyter Server skips activity tracking for this parameter; whether Colab's
    # idle shutdown looks at it is not documented.
    # The token is for this runtime's proxy only, so a redirect is an error, never followed.
    with requests.get(
        url.rstrip("/") + path, headers=headers, params={"no_track_activity": "1"}, timeout=timeout, allow_redirects=False, stream=True
    ) as r:
        if r.is_redirect:
            raise requests.HTTPError(f"runtime proxy redirected to {r.headers.get('Location', '?')}", response=r)
        r.raise_for_status()
        body = bytearray()
        while time.monotonic() < deadline and (chunk := r.raw.read1(decode_content=True)):
            body += chunk
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no full reply from the runtime in {timeout:g} s")
    return parse_json(body.decode())


def _runtime_status(assignment: dict) -> Status | Exception:
    proxy = assignment["runtimeProxyInfo"]
    try:
        return Status.parse(
            _proxy_get(proxy["url"], proxy["token"], "/api/status", TIMEOUT),
            _proxy_get(proxy["url"], proxy["token"], "/api/kernels", TIMEOUT),
        )
    except Exception as e:  # shown in the runtime's row, never swallowed
        return e
