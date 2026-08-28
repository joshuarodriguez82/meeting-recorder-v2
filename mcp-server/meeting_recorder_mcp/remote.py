"""
Remote (HTTP) transport, so a cloud-hosted assistant can reach the archive.

WHY
---
The stdio transport only serves tools running on the same machine as the
app: the client launches the server as its own child process. That
covers Claude Code, Claude Desktop, Cursor and VS Code, and it is the
right default because the archive stays unreachable by construction.

It does not cover claude.ai in a browser, Claude on a phone, or any
hosted assistant, because those run on someone else's computers and have
nothing to launch. Reaching them needs an address on the internet, which
is what this module provides — deliberately, behind a switch, off until
someone turns it on.

THE SHAPE OF THE DEPLOYMENT
---------------------------
This binds **loopback only** by default and expects a tunnel in front of
it (Cloudflare Tunnel, or equivalent) to give it a public HTTPS name.
That is not an incidental detail:

  - No inbound firewall rule and no open port. The machine dials out.
  - TLS terminates at the tunnel, so the token never crosses the network
    in the clear.
  - Turning it off is closing the tunnel, not editing firewall rules.

Binding 0.0.0.0 is possible and is not the default, because that turns
"I published this" into "it was on the office wifi the whole time".

TWO THINGS THIS CANNOT FIX, which belong in any UI that turns it on:
the machine has to be awake with the app running, because the tunnel
only forwards to something that is listening; and running a tunnel on a
managed work machine may sit badly with its security policy.

AUTH
----
Every request carries `Authorization: Bearer <token>`, checked before
anything reaches the MCP layer.

The token is its OWN secret, not the backend token. That one is held by
the Chrome extension and pasted into local tools; this one is typed into
a hosted connector's configuration and travels off the machine. Separate
secrets mean revoking remote access does not break the extension, and a
leak of one is not a leak of the other.

WHAT THIS DOES NOT CHANGE
-------------------------
The tools. Same seven, same read-only surface, same code. Transport is a
choice about who can reach the server, not about what it can do.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger(__name__)

#: Env var the app sets when it launches the server in HTTP mode.
TOKEN_ENV = "MEETING_RECORDER_REMOTE_TOKEN"  # nosec B105 - a variable name, not a secret

#: Filename beside the app's other secrets. Distinct from
#: ``extension-token`` on purpose — see the module docstring.
TOKEN_FILENAME = "remote-access-token"  # nosec B105 - a filename, not a secret

#: Loopback unless explicitly overridden. See the module docstring.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17646

#: Paths served without a token. Only liveness, which reveals nothing
#: beyond "a server is here" and which a tunnel needs.
_OPEN_PATHS = frozenset({"/health"})


def generate_token() -> str:
    """A fresh 64-character hex token. Same shape as the app's other
    tokens, so it looks familiar in a config field."""
    return secrets.token_hex(32)


def bind_host(host: Optional[str]) -> str:
    return (host or "").strip() or DEFAULT_HOST


def is_open_path(path: str) -> bool:
    """Exact match only. A prefix test would let ``/health/../mcp``
    through on any client that doesn't normalise first."""
    return path in _OPEN_PATHS


def authorized(header: Optional[str], expected: str) -> bool:
    """Whether an Authorization header carries the expected token.

    Returns False for an empty ``expected`` rather than accepting an
    empty header — if the app failed to generate or load a token, the
    correct behaviour is to refuse everything. A naive ``==`` would
    accept every request in exactly that state.

    Uses compare_digest so the comparison does not short-circuit on the
    first differing byte.
    """
    if not expected:
        return False
    if not header:
        return False
    parts = header.strip().split(None, 1)
    if len(parts) != 2:
        return False
    scheme, value = parts
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip(), expected)


def resolve_token() -> str:
    """The remote token: env first (how the app passes it), then the
    file beside the other secrets. Empty string when there is none,
    which `authorized` treats as refuse-everything."""
    env = (os.environ.get(TOKEN_ENV) or "").strip()
    if env:
        return env
    from .discovery import candidate_data_dirs

    for directory in candidate_data_dirs():
        try:
            raw = (directory / TOKEN_FILENAME).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        token = raw.strip()
        if token:
            return token
    return ""


def build_app(server, token: str):
    """The MCP streamable-HTTP app with auth wrapped around it.

    Auth is applied as ASGI middleware rather than inside a route so
    there is no path through the app that skips it — a new endpoint
    added by the SDK is closed by default, which is the right way round.
    """
    inner = server.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] != "http":
            # Lifespan and anything else non-HTTP goes straight through;
            # there is no request to authorize.
            await inner(scope, receive, send)
            return

        path = scope.get("path", "")
        if is_open_path(path):
            await _plain(send, 200, b'{"status":"ok"}')
            return

        header = None
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                header = value.decode("latin-1")
                break

        if not authorized(header, token):
            # No detail about which part failed, and no logging of the
            # supplied value — it is a credential.
            logger.warning("Rejected an unauthorized remote request to %s", path)
            await _plain(send, 401, b'{"error":"unauthorized"}',
                         extra=[(b"www-authenticate", b'Bearer realm="meeting-recorder"')])
            return

        await inner(scope, receive, send)

    return app


async def _plain(send, status: int, body: bytes, extra=None) -> None:
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode())]
    headers.extend(extra or [])
    await send({"type": "http.response.start", "status": status,
                "headers": headers})
    await send({"type": "http.response.body", "body": body})
