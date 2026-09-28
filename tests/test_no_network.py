"""Proves the autouse `block_network` fixture in conftest.py actually blocks network access."""

from __future__ import annotations

import socket

import pytest


def test_socket_connect_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(RuntimeError):
        sock.connect(("example.com", 80))


def test_socket_connect_ex_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(RuntimeError):
        sock.connect_ex(("example.com", 80))


def test_create_connection_is_blocked() -> None:
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.com", 80))


def test_getaddrinfo_is_blocked() -> None:
    with pytest.raises(RuntimeError):
        socket.getaddrinfo("example.com", 80)
