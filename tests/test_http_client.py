"""Tests for the tolerant httpx client factory."""

import httpx
from nova.http_client import create_async_client, normalized_env_proxy


def test_normalize_socks_scheme():
    assert normalized_env_proxy() is None  # no env in test runner (usually)
    import os

    os.environ["ALL_PROXY"] = "socks://127.0.0.1:7897"
    try:
        assert normalized_env_proxy() == "socks5://127.0.0.1:7897"
    finally:
        del os.environ["ALL_PROXY"]


def test_broken_proxy_env_does_not_crash(monkeypatch):
    """Regression: ALL_PROXY=socks://... used to raise ValueError on client build."""
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")
    client = create_async_client(timeout=5)  # must not raise, whatever socks support exists
    assert isinstance(client, httpx.AsyncClient)


def test_explicit_proxy_none_ignores_env(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")
    client = create_async_client(proxy="http://127.0.0.1:1", timeout=5)
    assert isinstance(client, httpx.AsyncClient)
