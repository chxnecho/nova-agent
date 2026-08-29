"""Web tools: fetch a URL as readable text, with SSRF protection."""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

from nova.http_client import create_async_client
from nova.tools.base import tool

MAX_CONTENT = 20000
_MAX_REDIRECTS = 5
_TIMEOUT = 30.0


class WebTools:
    """SSRF-guarded fetch tool.

    Refuses private/loopback/link-local/internal hosts (fail closed) unless
    allow_private is enabled, and follows redirects only after re-validating
    each hop. An optional hostname allow-list can be supplied via config.
    """

    def __init__(self, allow_private: bool = False,
                 allowed_domains: list[str] | None = None):
        self.allow_private = allow_private
        self.allowed_domains = allowed_domains or []

    def register(self, registry) -> None:
        registry.register(tool(
            name="web_fetch",
            description="Fetch a web page by URL and return its readable text "
                        "(HTML tags stripped). Works for docs, articles, APIs returning JSON.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer",
                                  "description": "Truncate content to this length (default 20000)"},
                },
                "required": ["url"],
            },
        )(self.fetch))

    # ------------------------------------------------------------------ #

    def _validate_url(self, raw: str) -> str:
        """Return a normalized, safe fetch URL or raise ValueError (fail closed)."""
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {scheme or '(none)'!r}")
        host = parsed.hostname or ""
        if not host:
            raise ValueError("URL is missing a host")

        # optional hostname allow-list
        if self.allowed_domains and not any(
                host == d or host.endswith("." + d) for d in self.allowed_domains):
            raise ValueError(f"host '{host}' is not in tools.web.allowed_domains")

        # SSRF guard: refuse non-public destinations
        if not self.allow_private and _host_is_private(host):
            raise ValueError(
                f"URL resolves to a private/internal address ('{host}'); "
                "blocked by the SSRF guard (tools.web.allow_private=false)")

        # rebuild a clean URL so path/redirect trickery can't smuggle a scheme
        return (f"{scheme}://{host}"
                + (f":{parsed.port}" if parsed.port else "")
                + (parsed.path or "/")
                + (f"?{parsed.query}" if parsed.query else ""))

    async def fetch(self, url: str, max_chars: int | None = None) -> str:
        limit = min(max_chars or MAX_CONTENT, MAX_CONTENT)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaAgent/0.1)"}
        try:
            target = self._validate_url(url)
        except ValueError as exc:
            return f"ERROR: {exc}"

        # follow redirects ourselves so every hop is re-validated against SSRF
        for _ in range(_MAX_REDIRECTS + 1):
            async with create_async_client(headers=headers, timeout=_TIMEOUT,
                                           follow_redirects=False) as c:
                resp = await c.get(target)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    break
                try:
                    target = self._validate_url(urljoin(target, loc))
                except ValueError as exc:
                    return f"ERROR: redirect blocked: {exc}"
                continue
            break

        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        body = resp.text

        if "html" in ctype:
            body = _html_to_text(body)
        return f"[HTTP {resp.status_code} | {ctype.split(';')[0]}]\n{body[:limit]}"


def _host_is_private(host: str) -> bool:
    """True when host resolves (or fails to resolve) to a non-public address."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True                       # unresolvable -> fail closed
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return True                   # malformed -> fail closed
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False


def _html_to_text(html_text: str) -> str:
    # drop scripts/styles/comments, then tags, then unescape entities
    html_text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", "", html_text)
    html_text = re.sub(r"(?s)<!--.*?-->", "", html_text)
    html_text = re.sub(r"(?i)<br\s*/?>", "\n", html_text)
    html_text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n", html_text)
    html_text = re.sub(r"<[^>]+>", " ", html_text)
    text = html.unescape(html_text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()
