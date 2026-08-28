"""HTTP client for the Meeting Recorder backend.

Read-only. Every method here maps to a GET, or to a POST that the
backend treats as a query (/search/semantic, /qa/stream). Nothing in
this module deletes, mutates settings, or triggers indexing — see the
module-level allowlist assertion in tests.

Auth: `Authorization: Bearer <token>` on every request, per
backend/server.py::require_backend_token. /health is the one exempt
path, which is exactly why it is NOT used as the auth probe: it answers
200 with a bad token too.

Errors are normalised into meeting_recorder_mcp.errors so callers never
see an httpx exception or an RFC 7807 body.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .discovery import BackendLocation, TokenNotFound, resolve_location
from .errors import (
    BackendError,
    BackendTimeout,
    BackendUnauthorized,
    BackendUnavailable,
    BackendUnreachable,
    SessionNotFound,
    TokenUnavailable,
)
from .models import QAAnswer, parse_hits

DEFAULT_TIMEOUT = 30.0
#: Q&A calls an LLM behind the stream, so they get a longer leash.
QA_TIMEOUT = 180.0


def _problem_detail(response: httpx.Response) -> str:
    """Pull a human string out of an RFC 7807 body, or fall back to text.

    backend/server.py returns application/problem+json everywhere:
    {"type","title","status","detail","instance"}.
    """
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return (response.text or "").strip()[:500]
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("title") or ""
        if isinstance(detail, (dict, list)):
            return json.dumps(detail)[:500]
        return str(detail)[:500]
    return str(payload)[:500]


class MeetingRecorderClient:
    """Thin, synchronous-shaped async client over the backend API."""

    def __init__(
        self,
        location: Optional[BackendLocation] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        # Resolution is deferred-but-eager: if the token can't be found
        # we raise TokenUnavailable here rather than sending an
        # unauthenticated request and reporting a confusing 401.
        if location is None:
            try:
                location = resolve_location()
            except TokenNotFound as exc:
                raise TokenUnavailable([str(p) for p in exc.searched]) from exc
        self.location = location
        self._timeout = timeout
        self._transport = transport

    # ── plumbing ────────────────────────────────────────────────────

    def _client(self, timeout: Optional[float] = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.location.base_url,
            headers={
                "Authorization": f"Bearer {self.location.token}",
                "Accept": "application/json",
            },
            timeout=timeout or self._timeout,
            transport=self._transport,
        )

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 401:
            raise BackendUnauthorized(
                self.location.token_source, self.location.token_looks_unusual)
        detail = _problem_detail(response)
        if response.status_code == 404:
            raise BackendError(404, detail, path)
        if response.status_code in (409, 503):
            # The backend uses these for "up but this capability isn't
            # configured" — Q&A with no AI provider, embeddings missing,
            # a cloud-synced config file not downloaded yet. Its own
            # `detail` text is already user-facing and actionable, so
            # relay it rather than replacing it.
            raise BackendUnavailable(
                f"Meeting Recorder can't serve this right now: "
                f"{detail or f'HTTP {response.status_code}'}")
        raise BackendError(response.status_code, detail, path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        try:
            async with self._client(timeout) as client:
                response = await client.request(
                    method, path, json=json_body, params=params)
        except httpx.TimeoutException as exc:
            raise BackendTimeout(
                self.location.base_url, timeout or self._timeout) from exc
        except httpx.HTTPError as exc:
            # ConnectError, ConnectTimeout, remote-disconnect, DNS —
            # all of them mean "the app isn't there".
            raise BackendUnreachable(
                self.location.base_url, type(exc).__name__) from exc
        self._raise_for_status(response, path)
        if not response.content:
            return None
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise BackendError(
                response.status_code,
                f"response was not JSON: {response.text[:200]!r}",
                path,
            ) from exc

    # ── read-only API surface ───────────────────────────────────────

    async def health(self) -> Dict[str, Any]:
        """GET /health — auth-exempt liveness probe.

        Useful to separate "app down" from "token bad": if this
        succeeds but an authed call 401s, the token is the problem.
        """
        return await self._request("GET", "/health") or {}

    async def verify(self) -> Dict[str, Any]:
        """Authenticated round-trip. Raises the specific failure.

        Uses /search/index/status because it's cheap, authed, and always
        answers 200 even when the search service is unavailable (it
        returns available=False rather than erroring).
        """
        return await self._request("GET", "/search/index/status") or {}

    async def semantic_search(
        self,
        query: str,
        *,
        top_k: int = 10,
        client: Optional[str] = None,
        project: Optional[str] = None,
    ) -> List[Any]:
        body: Dict[str, Any] = {
            "query": query,
            # Backend clamps to 1..50; clamp here too so the request is
            # honest about what it will get back.
            "top_k": max(1, min(50, int(top_k))),
        }
        if client:
            body["client"] = client
        if project:
            body["project"] = project
        payload = await self._request("POST", "/search/semantic", json_body=body)
        results = (payload or {}).get("results")
        return parse_hits(results)

    async def index_status(self) -> Dict[str, Any]:
        return await self._request("GET", "/search/index/status") or {}

    async def client_configs(self) -> Dict[str, Any]:
        return await self._request("GET", "/clients/config") or {}

    async def client_knowledge(self, client_name: str) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/clients/{quote(client_name, safe='')}/knowledge") or {}

    async def list_sessions(self) -> List[Dict[str, Any]]:
        payload = await self._request("GET", "/sessions")
        return payload if isinstance(payload, list) else []

    async def portal_bindings(self) -> Dict[str, Any]:
        """Every (client, project) -> portal binding, keyed by scope slug.

        Tokenless by construction: edit tokens live in the OS keychain
        and never enter the bindings JSON, so nothing here needs
        redacting before it reaches a model. `token_present` on an entry
        is computed per machine and means "the token is on THIS device",
        not "the binding is healthy" — the bindings file roams between
        the user's laptops, the keychain does not.
        """
        payload = await self._request("GET", "/portal/bindings")
        return payload if isinstance(payload, dict) else {}

    async def list_commitments(
        self,
        *,
        client: Optional[str] = None,
        project: Optional[str] = None,
        status: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Commitments across every session, filtered server-side.

        `status` is passed through verbatim because /commitments already
        understands the synthetic values "active" (awaiting + overdue)
        and "overdue" (awaiting, past due). Re-deriving those here would
        mean fetching everything and duplicating a rule that already has
        one home.
        """
        params: Dict[str, Any] = {}
        if client:
            params["client"] = client
        if project:
            params["project"] = project
        if status:
            params["status"] = status
        if owner:
            params["owner"] = owner
        payload = await self._request("GET", "/commitments", params=params or None)
        if isinstance(payload, dict):
            rows = payload.get("commitments")
            return rows if isinstance(rows, list) else []
        return payload if isinstance(payload, list) else []

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        try:
            payload = await self._request(
                "GET", f"/sessions/{quote(session_id, safe='')}")
        except BackendError as exc:
            if exc.status == 404:
                raise SessionNotFound(session_id) from exc
            raise
        if not isinstance(payload, dict):
            raise SessionNotFound(session_id)
        return payload

    # ── Q&A (SSE) ───────────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        *,
        top_k: int = 8,
        client: Optional[str] = None,
        project: Optional[str] = None,
        timeout: float = QA_TIMEOUT,
    ) -> QAAnswer:
        """POST /qa/stream and collect the SSE stream into one answer.

        Frame vocabulary (backend/server.py::qa_stream):
            event: sources   data: [ ...hits... ]
            (default event)  data: {"text": "..."}
            event: done      data:
            event: error     data: {"error": "..."}

        A trailing `event: error` after partial text is reported WITH the
        partial text, not swallowed — a half answer plus "the stream
        failed here" beats silently returning a truncated answer as if
        it were complete.
        """
        body: Dict[str, Any] = {
            "query": question,
            "top_k": max(1, min(20, int(top_k))),
        }
        if client:
            body["client"] = client
        if project:
            body["project"] = project

        chunks: List[str] = []
        sources: List[Any] = []
        error = ""
        try:
            async with self._client(timeout) as http:
                async with http.stream(
                    "POST", "/qa/stream", json=body,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        self._raise_for_status(response, "/qa/stream")
                    buffer = ""
                    async for raw in response.aiter_text():
                        buffer += raw
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            name, data = _parse_sse_frame(frame)
                            if name == "sources":
                                try:
                                    sources = parse_hits(json.loads(data))
                                except (ValueError, TypeError):
                                    sources = []
                            elif name == "done":
                                buffer = ""
                                break
                            elif name == "error":
                                error = _sse_error_text(data)
                                buffer = ""
                                break
                            elif name == "message":
                                try:
                                    payload = json.loads(data)
                                except (ValueError, TypeError):
                                    continue
                                if isinstance(payload, dict) and payload.get("text"):
                                    chunks.append(str(payload["text"]))
                        if error:
                            break
        except httpx.TimeoutException as exc:
            raise BackendTimeout(self.location.base_url, timeout) from exc
        except httpx.HTTPError as exc:
            raise BackendUnreachable(
                self.location.base_url, type(exc).__name__) from exc

        return QAAnswer(answer="".join(chunks), sources=sources, error=error)


def _parse_sse_frame(frame: str) -> tuple[str, str]:
    """(event_name, data) for one SSE frame. Comment-only frames -> ("", "").

    Multiple `data:` lines concatenate with newlines, per the SSE spec —
    the backend JSON-encodes fragments precisely so this never happens,
    but honouring it costs nothing and avoids a silent truncation if it
    ever does.
    """
    name = ""
    data_lines: List[str] = []
    for line in frame.split("\n"):
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if not name and data_lines:
        name = "message"
    return name, "\n".join(data_lines)


def _sse_error_text(data: str) -> str:
    try:
        payload = json.loads(data)
    except (ValueError, TypeError):
        return data.strip() or "Unknown error"
    if isinstance(payload, dict):
        return str(payload.get("error") or "Unknown error")
    return str(payload)
