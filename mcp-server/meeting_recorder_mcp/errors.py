"""Failure taxonomy.

Every one of these carries a message written for the *model* to read
and relay, not a stack trace. The distinctions matter operationally:

  BackendUnreachable   the app isn't running (or is on another port)
  TokenUnavailable     we never found a token to send
  BackendUnauthorized  we sent a token and the backend rejected it
  BackendUnavailable   the backend is up but the feature isn't ready
                       (503/409 — e.g. no AI provider configured)
  BackendError         anything else the backend reported
  BackendTimeout       the backend accepted the connection and stalled

"empty result" is deliberately NOT in this list: an empty result is a
success and must render differently from any of the above. See
formatting.py — every renderer emits an explicit "0 results" line.
"""

from __future__ import annotations

from typing import List, Optional


class MeetingRecorderError(Exception):
    """Base class. `.message` is safe to hand straight to the model."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class BackendUnreachable(MeetingRecorderError):
    def __init__(self, base_url: str, detail: str = "") -> None:
        self.base_url = base_url
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"Meeting Recorder isn't running — nothing is listening on "
            f"{base_url}{suffix}. Start the Meeting Recorder app and try "
            f"again. If the app IS running, quit and reopen it: it writes "
            f"whichever port it got to a 'backend-port' file beside the "
            f"token, and this server reads that by itself — a stale file "
            f"from a previous run is the usual cause. Only if that still "
            f"fails, set MEETING_RECORDER_URL in this server's config to "
            f"the URL shown in the app under Settings -> Templates & "
            f"Integrations -> AI assistant access."
        )


class BackendTimeout(MeetingRecorderError):
    def __init__(self, base_url: str, seconds: float) -> None:
        super().__init__(
            f"Meeting Recorder accepted the connection at {base_url} but "
            f"didn't answer within {seconds:g}s. It may be busy indexing or "
            f"transcribing. Try again shortly."
        )


class TokenUnavailable(MeetingRecorderError):
    def __init__(self, searched: Optional[List[str]] = None) -> None:
        looked = ""
        if searched:
            looked = " Looked in: " + ", ".join(searched[:6]) + "."
        super().__init__(
            "No Meeting Recorder auth token found, so the request was never "
            "sent. The app writes its token to `extension-token` in its user "
            "data folder the first time it launches (v2.16+), so launching "
            "Meeting Recorder once usually fixes this. Otherwise set "
            "MEETING_RECORDER_TOKEN in this MCP server's config to the value "
            "from Settings -> Chrome Extension." + looked
        )


class BackendUnauthorized(MeetingRecorderError):
    def __init__(self, token_source: str, looks_unusual: bool = False) -> None:
        extra = (
            " The token also doesn't look like the app's format (64 hex "
            "characters), so it may be truncated or from a different tool."
            if looks_unusual else ""
        )
        super().__init__(
            f"Meeting Recorder rejected the auth token (HTTP 401). The token "
            f"came from {token_source}. It's stale or wrong — this happens "
            f"when the token file was rotated after this MCP server started, "
            f"or when MEETING_RECORDER_TOKEN is pinned to an old value. Copy "
            f"a fresh token from Meeting Recorder -> Settings -> Chrome "
            f"Extension, or delete the `extension-token` file and restart "
            f"the app to rotate it.{extra}"
        )


class BackendUnavailable(MeetingRecorderError):
    """Backend is healthy but this capability isn't ready (409 / 503)."""


class BackendError(MeetingRecorderError):
    def __init__(self, status: int, detail: str, path: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(
            f"Meeting Recorder returned HTTP {status} for {path}: "
            f"{detail or '(no detail)'}"
        )


class SessionNotFound(MeetingRecorderError):
    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"No session with id '{session_id}'. Use list_meetings or "
            f"search_meetings to get a valid session_id — document hits from "
            f"search_meetings have no session_id and can't be passed here."
        )
