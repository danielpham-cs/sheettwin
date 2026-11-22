"""Per-user data: every signed-in user works in a data directory of their own.

Behind a gateway that signs users in and passes who they are in a request header, set SHEETIR_USER_HEADER to that
header's name. Intel Cloud's gateway sends the user's e-mail as Oidc_claim_email, and jupyter-server-proxy forwards it.
Each request then reads and writes only

    data/users/<user>/...          workbooks, projects, project documents (same layout as data/ below)

so users never see each other's workbooks, projects or documents. Unset (the default, e.g. on a laptop), or on a
request that carries no such header, everything stays in the one shared data directory, as before.

Only turn this on where the header can come from the gateway alone: the gateway must drop any copy the browser sends
(mod_auth_openidc does), and the app must not be reachable around it (bind it to 127.0.0.1).
"""

from __future__ import annotations

import contextvars
import os
import re
from pathlib import Path

USERS = "users"
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("sheetir_user", default=None)


def header_name() -> str:
    """The request header that names the user, lower-cased as ASGI delivers it; "" when users are not separated."""
    return os.environ.get("SHEETIR_USER_HEADER", "").strip().lower()


def user_key(value: str) -> str | None:
    """A directory name for the user: the header value lower-cased, anything but [a-z0-9._@+-] replaced.
    None for a value that would not name a directory of its own (empty, ".", "..")."""
    key = re.sub(r"[^a-z0-9._@+-]", "_", value.strip().lower())[:128]
    return key if key.strip("._") else None


def current_user() -> str | None:
    return _current.get()


def scoped(base: Path) -> Path:
    """The data directory of the current request: the user's own under `base`, or `base` when there is no user."""
    user = _current.get()
    return base / USERS / user if user else base


class UserScopeMiddleware:
    """Reads the user from SHEETIR_USER_HEADER for the duration of each HTTP request (see the module docstring)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        name = header_name().encode("latin-1")
        if scope["type"] != "http" or not name:
            await self.app(scope, receive, send)
            return
        raw = next((v for k, v in scope["headers"] if k.lower() == name), None)
        token = _current.set(user_key(raw.decode("latin-1")) if raw else None)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
