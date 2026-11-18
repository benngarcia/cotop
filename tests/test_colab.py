from cotop.colab import still_assigned
from cotop.model import runtimes_from
from tests.test_model import G4, LOCAL


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


def test_the_proxy_token_never_follows_a_redirect():
    """A runtime's proxy token goes only to the URL Colab returned for that runtime."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

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
