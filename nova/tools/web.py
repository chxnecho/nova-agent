"""Web tools: fetch a URL as readable text."""

from __future__ import annotations

import html
import re

from nova.http_client import create_async_client
from nova.tools.base import tool

MAX_CONTENT = 20000
_TIMEOUT = 30.0


class WebTools:
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

    async def fetch(self, url: str, max_chars: int | None = None) -> str:
        limit = min(max_chars or MAX_CONTENT, MAX_CONTENT)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaAgent/0.1)"}
        async with create_async_client(headers=headers, timeout=_TIMEOUT,
                                       follow_redirects=True) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            body = resp.text

        if "html" in ctype:
            body = _html_to_text(body)
        return f"[HTTP {resp.status_code} | {ctype.split(';')[0]}]\n{body[:limit]}"


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
