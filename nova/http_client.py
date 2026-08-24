"""Shared async httpx client factory with tolerant proxy handling.

Users behind Clash/V2Ray often export ALL_PROXY=socks://127.0.0.1:7897.
httpx rejects the bare `socks://` scheme and needs the `socksio` extra for
SOCKS at all. We normalize what we can, degrade gracefully to a direct
connection otherwise, and never let proxy config crash the app.
"""

from __future__ import annotations

import os

import httpx

from nova.log import get_logger

log = get_logger("net")


def normalized_env_proxy() -> str | None:
    """Best-effort read+normalize of proxy env vars for httpx."""
    raw = (os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
           or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
           or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
           or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if low.startswith("socks://"):            # httpx only knows socks5/socks4
        raw = "socks5://" + raw[len("socks://"):]
    elif low.startswith("socks4a://"):
        raw = "socks4://" + raw[len("socks4a://"):]
    return raw


def create_async_client(**kwargs) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient, tolerating broken/unusable proxy settings."""
    proxy = kwargs.pop("proxy", None)
    if proxy is None:
        proxy = normalized_env_proxy()
    if proxy is None:
        return httpx.AsyncClient(**kwargs)

    # honor an explicit NO_* opt-out, mirroring curl conventions
    host = ""
    try:
        from urllib.parse import urlparse
        target = kwargs.get("base_url") or ""
        host = urlparse(target if "//" in target else "https://" + target).hostname or ""
    except Exception:
        pass
    no_proxy = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
    if host and any(h.strip() and host.endswith(h.strip()) for h in no_proxy.split(",")):
        return httpx.AsyncClient(trust_env=False, **kwargs)

    try:
        return httpx.AsyncClient(proxy=proxy, **kwargs)
    except (ValueError, ImportError, TypeError) as exc:
        log.warning("proxy %r unusable (%s: %s); falling back to direct connection",
                    proxy, type(exc).__name__, exc)
        return httpx.AsyncClient(trust_env=False, **kwargs)
