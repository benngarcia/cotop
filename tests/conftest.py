"""Every test runs away from the real Colab CLI state, cotop's cache and the network.

This is enforced, not a convention: a test that forgets a stub still reads and
writes only a temp dir, and fails if anything in it looks up or connects to
another machine.
"""

import ipaddress
import socket

import pytest

import cotop.app
import cotop.colab


@pytest.fixture(autouse=True)
def hermetic(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")  # not tmp_path: tests assert on what exists there
    cli_home = home / ".config" / "colab-cli"
    monkeypatch.setattr(cotop.colab, "CLI_HOME", cli_home)
    monkeypatch.setattr(cotop.colab, "SESSIONS", cli_home / "sessions.json")
    monkeypatch.setattr(cotop.app, "CACHE", home / ".cache" / "cotop")
    for var, path in {"HOME": home, "XDG_CONFIG_HOME": home / ".config", "XDG_CACHE_HOME": home / ".cache"}.items():
        monkeypatch.setenv(var, str(path))  # google-colab-cli expands ~ when it is called

    attempts = []  # cotop turns proxy errors into a gap or a message, so a raise alone can pass

    def lookup(host, *args, _real=socket.getaddrinfo, **kwargs):
        if not on_this_machine(host):
            attempts.append(host)
            raise socket.gaierror(f"tests may not look up {host}")
        return _real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    for method in ("connect", "connect_ex"):

        def connect(self, address, _real=getattr(socket.socket, method)):
            if isinstance(address, tuple) and not on_this_machine(address[0]):  # not a tuple: a Unix socket
                attempts.append(address)
                raise ConnectionRefusedError(f"tests may not connect to {address}")
            return _real(self, address)

        monkeypatch.setattr(socket.socket, method, connect)
    yield
    assert not attempts, f"reached for the network: {attempts}"


def on_this_machine(host) -> bool:
    if host in (None, "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
