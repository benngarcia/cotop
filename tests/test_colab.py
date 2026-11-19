import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from cotop import colab
from cotop.colab import still_assigned
from cotop.model import runtimes_from
from tests.test_model import FIXTURES, G4, LOCAL, T4, assignment

STATUS = {"/api/status": json.dumps({"started": "2026-11-17T20:59:46Z"}), "/api/kernels": "[]"}


@contextmanager
def runtime_proxy(code=200):
    """A runtime proxy on loopback: yields a runtime that uses it and the (method, path) of each request."""
    seen = []

    class Proxy(BaseHTTPRequestHandler):
        def answer(self):
            seen.append((self.command, self.path))
            self.send_response(code)
            self.end_headers()
            self.wfile.write(STATUS.get(self.path.split("?")[0], (FIXTURES / "resources_g4.txt").read_text()).encode())

        do_GET = do_POST = answer

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    a = assignment("gpu-g4-s-local", "G4")
    a["runtimeProxyInfo"]["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield a, seen
    finally:
        server.shutdown()


def test_a_name_counts_only_while_it_still_means_the_same_vm():
    [r] = runtimes_from([G4], LOCAL, {})
    assert still_assigned(r, [G4], LOCAL)
    assert not still_assigned(r, [], LOCAL)  # VM gone: `colab ssh -s` would create a new one
    reused = {"train": {"name": "train", "endpoint": "gpu-g4-new", "running": None}}
    assert not still_assigned(r, [G4], reused)  # name now means another VM
    [untracked] = runtimes_from([G4], {}, {})
    assert not still_assigned(untracked, [G4], {})


def test_no_cli_sessions_before_the_cli_has_run(tmp_path, monkeypatch):
    """A first-time user signed in through cotop has no ~/.config/colab-cli yet."""
    import cotop.colab

    monkeypatch.setattr(cotop.colab, "SESSIONS", tmp_path / "colab-cli" / "sessions.json")
    assert cotop.colab.Colab(None).sessions() == {}
    assert not (tmp_path / "colab-cli").exists()  # cotop creates nothing in the CLI's directory


def test_every_proxy_read_is_a_get_that_asks_not_to_count_as_activity():
    """README "Safety": reading a runtime must not keep it from going idle, and must not change it."""
    with runtime_proxy() as (a, seen):
        assert colab.Colab(None).status([a])[a["endpoint"]].started is not None
        [r] = runtimes_from([a], {}, {})
        assert colab.Colab(None).resources([r])[r.endpoint] is not None
    assert len(seen) == 3
    assert all(method == "GET" and path.endswith("?no_track_activity=1") for method, path in seen), seen


def test_a_failed_resources_read_is_a_gap_not_a_zero():
    with runtime_proxy(code=502) as (a, _):
        [r] = runtimes_from([a], {}, {})
        assert colab.Colab(None).resources([r]) == {r.endpoint: None}


def test_an_untracked_stop_unassigns_that_runtimes_endpoint():
    """A runtime with no CLI name is stopped through the client, by its endpoint."""
    unassigned = []
    google = type("Google", (), {"unassign": lambda self, endpoint: unassigned.append(endpoint)})()
    [untracked] = runtimes_from([T4], {}, {})
    colab.Colab(google).stop(untracked)
    assert unassigned == [T4["endpoint"]]


def test_the_proxy_token_never_follows_a_redirect():
    """A runtime's proxy token goes only to the URL Colab returned for that runtime."""
    import pytest
    import requests

    from cotop.colab import _proxy_get

    seen = []

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("X-Colab-Runtime-Proxy-Token"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    other = HTTPServer(("127.0.0.1", 0), Other)

    class Proxy(Other):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{other.server_port}/elsewhere")
            self.end_headers()

    proxy = HTTPServer(("127.0.0.1", 0), Proxy)
    for server in (proxy, other):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(requests.HTTPError, match="redirected"):
            _proxy_get(f"http://127.0.0.1:{proxy.server_port}", "TOKEN", "/api/status", 3)
        assert seen == []
    finally:
        proxy.shutdown()
        other.shutdown()


def test_the_cli_library_never_logs_below_warning_or_to_its_log_file(monkeypatch):
    """Its client logs request headers and bodies at DEBUG, even when the root logger takes DEBUG."""
    import logging

    import cotop.colab  # noqa: F401

    monkeypatch.setattr(logging.getLogger(), "level", logging.DEBUG)

    for name in ("colab_cli", "colab_cli.client"):
        logger = logging.getLogger(name)
        assert logger.getEffectiveLevel() == logging.WARNING
        handlers = logger.handlers + logging.getLogger().handlers
        assert not [h for h in handlers if str(getattr(h, "baseFilename", "")).endswith("colab.log")]


FAKE_COLAB = """
import sys
mode = sys.argv[1]
if mode == "warn":
    print("[colab] Stopping session 'train'...")
    print("[colab] Session terminated.")
    print("Warning: Could not read the OAuth token from the OS keyring (timed out).", file=sys.stderr)
elif mode == "notfound":
    print("[colab] Session 'train' not found.")
elif mode == "prompt":  # the CLI's sign-in when its token can't be refreshed
    print("read: " + input("Enter the authorization code: "))
"""


def test_a_colab_command_reports_what_it_said_on_stdout_and_reads_no_keys(tmp_path, monkeypatch):
    """Its stderr warning is not the result, "not found" is not a success, and a prompt can't read cotop's terminal."""
    import os
    import sys

    import pytest

    (tmp_path / "colab").write_text(FAKE_COLAB)
    monkeypatch.setattr(colab, "colab_argv", lambda mode, name: [sys.executable, str(tmp_path / "colab"), mode])
    assert colab._cli("warn", "train") == "[colab] Session terminated."
    with pytest.raises(RuntimeError, match="Session 'train' not found"):
        colab._cli("notfound", "train")
    typed, keyboard = os.pipe()  # cotop's stdin, holding keys meant for the dashboard
    os.write(keyboard, b"s\n")
    os.close(keyboard)
    terminal = os.dup(0)
    os.dup2(typed, 0)
    try:
        with pytest.raises(RuntimeError, match="Enter the authorization code"):
            colab._cli("prompt", "train")
    finally:
        os.dup2(terminal, 0)
        os.close(terminal)
        os.close(typed)


def test_the_demo_runs_without_the_cli_library(tmp_path):
    """--demo needs no google-colab-cli internals, which may change under cotop."""
    import subprocess
    import sys

    script = "import sys; sys.modules['colab_cli'] = None; from cotop.app import demo_dashboard; demo_dashboard(__import__('pathlib').Path(sys.argv[1]))"
    done = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
