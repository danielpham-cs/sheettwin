"""Serve the UI under whatever URL prefix a reverse proxy puts in front of it, without rebuilding it.

Build the UI with NEXT_PUBLIC_BASE_PATH=/__SHEETTWIN_BASE__ (a placeholder) and every static file that needs the
prefix carries the placeholder instead. This middleware swaps it, per request, for the prefix the proxy reports in
X-Forwarded-Prefix (jupyter-server-proxy sends e.g. /6/proxy/8765), or for nothing when there is no proxy. One
image then serves both http://host:8765/ and https://<jupyter>/<n>/proxy/8765/, and the <n> may change freely.

API routes (/api/...) pass through untouched, so streamed answers stay streamed.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

PLACEHOLDER = b"/__SHEETTWIN_BASE__"
_SAFE = re.compile(r"^(/[A-Za-z0-9._~-]+)*$")  # a plain path; anything else is ignored
_TEXT = (b"text/", b"application/javascript", b"application/json", b"application/manifest+json", b"image/svg+xml")
_CONDITIONAL = (b"if-none-match", b"if-modified-since")  # a 304 would hand back a copy made for another prefix


def forwarded_prefix(headers: list[tuple[bytes, bytes]]) -> str:
    """The proxy's URL prefix without a trailing slash, or "" when absent or not a plain path."""
    raw = next((v for k, v in headers if k.lower() == b"x-forwarded-prefix"), b"").decode("latin-1").strip()
    raw = raw.rstrip("/")
    return raw if _SAFE.match(raw) else ""


def _behind(prefix: bytes, location: bytes) -> bytes:
    """A redirect target as seen through the proxy: its path (and query) under the prefix. The backend would
    otherwise send the browser to its own host (e.g. http://127.0.0.1:8765/app/), which the browser cannot reach."""
    u = urlsplit(location.decode("latin-1"))
    if u.netloc == "" and not u.path.startswith("/"):
        return location  # relative: already resolves under the prefix
    return prefix + (u.path + ("?" + u.query if u.query else "")).encode("latin-1")


class BasePathMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        prefix = forwarded_prefix(scope["headers"]).encode()
        scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k.lower() not in _CONDITIONAL]}
        start: dict | None = None
        chunks: list[bytes] = []

        async def rewrite(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = list(start["headers"])
            ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
            if 300 <= start["status"] < 400 and prefix:  # e.g. StaticFiles redirecting /app to /app/
                headers = [(k, _behind(prefix, v) if k.lower() == b"location" else v) for k, v in headers]
            if ctype.startswith(_TEXT) and PLACEHOLDER in body:
                body = body.replace(PLACEHOLDER, prefix)
                drop = (b"content-length", b"etag", b"last-modified", b"cache-control")
                headers = [(k, v) for k, v in headers if k.lower() not in drop]
                headers += [(b"content-length", str(len(body)).encode()), (b"cache-control", b"no-cache")]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, rewrite)
