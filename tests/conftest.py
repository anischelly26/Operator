"""Ensure every test uses its own database, never the interactive sandbox."""

import pytest
import socket
import sandbox.environment


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.environment, "DEFAULT_DB_PATH", tmp_path / "sandbox.db")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network calls are forbidden in the offline test suite")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
