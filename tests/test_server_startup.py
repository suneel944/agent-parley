"""Checks that local server startup never depends on name resolution."""

import socket

from agent_parley.server import Server


def test_server_binds_without_reverse_dns(tmp_path, monkeypatch):
    def unexpected_lookup(*args):
        raise AssertionError("Loopback startup must not resolve a hostname")

    monkeypatch.setattr(socket, "getfqdn", unexpected_lookup)
    with Server(tmp_path, {"port": 0, "token": "test"}) as server:
        assert server.server_name == "127.0.0.1"
        assert server.server_port > 0
