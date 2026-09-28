"""Test-suite-wide fixtures.

The product and its tests must never touch the network (see the project's cost/isolation
rules): every test runs under an autouse fixture that patches the socket primitives any
network call would go through and makes them raise instead of connecting.
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Iterator
from typing import Any

import pytest

# `fixtures/` and `eval/` are not part of the installed package; make them importable as
# `fixtures.*` / `eval.*` for tests that render or load scenario YAML.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _blocked_network_call(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("network access is blocked in tests")


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(socket.socket, "connect", _blocked_network_call)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_network_call)
    monkeypatch.setattr(socket, "create_connection", _blocked_network_call)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_network_call)
    yield
