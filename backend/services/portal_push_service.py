"""
Push engagement registers to the SA Tools Portal.

THE CONTRACT (portal side is built, deployed, tested — this side only
delivers the file):

    POST {portal_url}/customers/{customerId}/engagement/ingest
    X-Edit-Token: {editToken}
    {"register": <the parsed contents of engagement_<slug>.register.json>}

The register is sent EXACTLY as written to disk. No key renaming, no
flattening of occurrences[], no owner resolution, no status filtering —
the portal owns normalisation, and two implementations of it would
disagree within a month. `push()` asserts this property by construction:
it loads the file with json.load and places the object under one key.

Ingest is idempotent on the portal side (same register twice → added: 0),
so pushing freely — every regeneration, reconnect, manual sync — carries
no dedupe burden here.

FAILURE SEMANTICS, verbatim from the portal team, encoded as types:

    400 / 422   PortalPermanent — the register itself is the problem.
                Retrying identical bytes cannot produce a different
                answer. Logged, surfaced, not retried.
    403         PortalBindingBroken — wrong or revoked edit token.
                The binding is MARKED broken and pushes stop until the
                user re-binds. Retrying a bad token forever is silent
                failure wearing a retry schedule.
    503 / 5xx / network
                PortalTransient — raised to the caller so the push
                worker's (5s, 30s, 120s) schedule applies. 503
                specifically means a partial write and is the one
                status that MEANS "try again".

THE TOKEN IS A CREDENTIAL. It lives in the OS keychain
(config/secrets.py — Windows Credential Manager / macOS Keychain), the
same place as the API keys. It appears in portal_bindings.json never,
in log lines never, and in exception text never: every message that
could carry request context is built from the status code and the
response body only, and `_redact` scrubs the token defensively should a
future edit thread it into an error path anyway.

BINDINGS live in recordings_dir/portal_bindings.json, keyed by the same
scope key the register filenames use (client__project), holding only
non-secret fields: customerId, opportunityName, parentName, boundAt,
enabled, plus broken/broken_reason when a 403 has fired. One project
maps to exactly one portal opportunity.

Only PER-PROJECT registers push. The client-level rollup (project "")
is the union of the project registers; pushing both would file the same
items twice under the same ids and flap between the two on every run.

NOTHING here runs on the record → finalize → process path. Pushes are
enqueued onto a dedicated worker thread (export_worker.PortalPushWorker)
with bounded HTTP timeouts, so a black-holed portal URL costs a worker
thread a timeout, never the app a hang.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config import secrets
from utils.logger import get_logger

logger = get_logger(__name__)

BINDINGS_FILENAME = "portal_bindings.json"

# Keychain entry name for one binding's edit token. Scoped per binding:
# a token authorises exactly one opportunity, so storage mirrors that.
_TOKEN_KEY_FMT = "portal_edit_token::{scope}"  # noqa: S105  # nosec B105 - keychain entry NAME, not a secret value

# Bounded so a black-holed endpoint costs a worker thread ~35s, never
# an unbounded hang. (Connect and read share urllib's single timeout.)
_HTTP_TIMEOUT_S = 35


class PortalTransient(Exception):
    """Retryable: 503 partial write, other 5xx, network failure."""


class PortalPermanent(Exception):
    """Not retryable: the register or request itself is the problem."""


class PortalBindingBroken(Exception):
    """403 — the edit token is wrong or the opportunity was
    re-tokenised. The binding has already been marked broken by the
    time this is raised; the caller must not retry."""


def scope_key(client_key: str, project_key: str) -> str:
    """The same slug the register filenames use, so a binding and its
    register can never disagree about identity."""
    raw = f"{client_key}__{project_key}" if project_key else client_key
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip().lower())


class PortalPushService:
    """Bindings + the push itself. Thread-safe; called from HTTP
    handlers (bind/unbind/status) and from the push worker (push)."""

    def __init__(self, recordings_dir: Path,
                 get_portal_url=None) -> None:
        self._dir = Path(recordings_dir)
        self._path = self._dir / BINDINGS_FILENAME
        self._lock = threading.Lock()
        # Injected so the URL is a live setting (the portal has dev and
        # prod hosts), read at push time rather than frozen at startup.
        self._get_portal_url = get_portal_url or (lambda: "")

    # ── bindings ─────────────────────────────────────────────────────

    def _read(self) -> Dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"portal bindings unreadable ({e}); "
                           f"treating as none — pushes will no-op")
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._dir, delete=False,
            suffix=".tmp")
        with fd as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            tmp = Path(f.name)
        tmp.replace(self._path)

    def bind(self, client_key: str, project_key: str, *,
             customer_id: str, opportunity_name: str,
             parent_name: str, edit_token: str) -> Dict[str, Any]:
        """Create/replace a binding. The token goes to the keychain;
        everything else to the JSON. Binding again clears `broken` —
        re-binding IS the recovery path for a revoked token."""
        if not (customer_id or "").strip():
            raise ValueError("customer_id is required")
        if not (edit_token or "").strip():
            raise ValueError("edit_token is required")
        if not (project_key or "").strip():
            # Per-project only, by the same rule pushes enforce: the
            # client rollup must never bind, or it will double-file
            # every item the project registers already pushed.
            raise ValueError("bindings are per project; a client-level "
                             "binding would push the rollup register")
        scope = scope_key(client_key, project_key)
        if not secrets.set_secret(_TOKEN_KEY_FMT.format(scope=scope),
                                  edit_token.strip()):
            # A binding whose token silently failed to store would push
            # tokenless forever and read as portal breakage. Refuse.
            raise RuntimeError(
                "the OS keychain rejected the edit token; the binding "
                "was not created")
        with self._lock:
            data = self._read()
            data[scope] = {
                "client": client_key,
                "project": project_key,
                "customerId": customer_id.strip(),
                "opportunityName": (opportunity_name or "").strip(),
                "parentName": (parent_name or "").strip(),
                "boundAt": datetime.now(timezone.utc).isoformat(),
                "enabled": True,
                "broken": False,
                "broken_reason": "",
                "last_push_at": None,
                "last_push_result": None,
            }
            self._write(data)
        return {k: v for k, v in data[scope].items()}

    def unbind(self, client_key: str, project_key: str) -> bool:
        scope = scope_key(client_key, project_key)
        secrets.set_secret(_TOKEN_KEY_FMT.format(scope=scope), "")
        with self._lock:
            data = self._read()
            existed = scope in data
            data.pop(scope, None)
            self._write(data)
        return existed

    def binding_for(self, client_key: str, project_key: str) -> Optional[dict]:
        with self._lock:
            return (self._read()).get(scope_key(client_key, project_key))

    def bindings(self) -> Dict[str, Any]:
        """All bindings, tokenless by construction (tokens never enter
        the JSON)."""
        with self._lock:
            return self._read()

    def should_push(self, client_key: str, project_key: str) -> bool:
        """The enqueue filter: per-project scope, binding present,
        enabled, and not marked broken. A project with no binding never
        pushes anything — acceptance criterion 5."""
        if not (project_key or "").strip():
            return False
        b = self.binding_for(client_key, project_key)
        return bool(b and b.get("enabled") and not b.get("broken"))

    def _record_result(self, scope: str, *, ok: bool, note: str,
                       broken: bool = False) -> None:
        with self._lock:
            data = self._read()
            b = data.get(scope)
            if not b:
                return
            b["last_push_at"] = datetime.now(timezone.utc).isoformat()
            b["last_push_result"] = note[:300]
            if broken:
                b["broken"] = True
                b["broken_reason"] = note[:300]
            elif ok:
                b["broken"] = False
                b["broken_reason"] = ""
            self._write(data)

    # ── the push ─────────────────────────────────────────────────────

    def register_path(self, client_key: str, project_key: str) -> Path:
        scope = scope_key(client_key, project_key)
        return self._dir / f"engagement_{scope}.register.json"

    def push(self, client_key: str, project_key: str) -> Dict[str, Any]:
        """Push one project's register. Raises PortalTransient for the
        worker's retry schedule; everything else resolves here."""
        scope = scope_key(client_key, project_key)
        binding = self.binding_for(client_key, project_key)
        if not binding or not binding.get("enabled"):
            raise PortalPermanent("no enabled portal binding for this project")
        if binding.get("broken"):
            raise PortalBindingBroken(
                binding.get("broken_reason") or "binding marked broken")

        portal_url = (self._get_portal_url() or "").strip().rstrip("/")
        if not portal_url:
            raise PortalPermanent("portal URL is not configured in Settings")
        # Scheme allow-list, checked BEFORE the request is built: the
        # URL comes from a user-editable setting, and urllib would
        # happily open file:// or a custom scheme with it. https only
        # (plus http for a localhost dev portal) — this is the actual
        # mitigation the B310/semgrep audit rules exist to prompt.
        if not (portal_url.startswith("https://")
                or portal_url.startswith("http://127.0.0.1")
                or portal_url.startswith("http://localhost")):
            raise PortalPermanent(
                "portal URL must be https:// (or http://localhost for a "
                "dev portal)")

        token = secrets.get_secret(_TOKEN_KEY_FMT.format(scope=scope)) or ""
        if not token:
            self._record_result(scope, ok=False, broken=True,
                                note="edit token missing from the OS "
                                     "keychain — re-bind this project")
            raise PortalBindingBroken("edit token missing — re-bind")

        reg_path = self.register_path(client_key, project_key)
        try:
            register = json.loads(reg_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PortalPermanent(
                f"register file not found: {reg_path.name}")
        except (json.JSONDecodeError, OSError) as e:
            raise PortalPermanent(f"register unreadable: {e}")

        # VERBATIM. The file's parsed object, under one key. Nothing
        # renamed, resolved, flattened or filtered.
        body = json.dumps({"register": register}).encode("utf-8")
        url = (f"{portal_url}/customers/{binding['customerId']}"
               f"/engagement/ingest")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Edit-Token": token,
            })

        try:
            # The URL's scheme is allow-listed (https / localhost http)
            # a few lines up — the mitigation the audit rules below
            # exist to prompt.
            with urllib.request.urlopen(  # noqa: S310  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                    req, timeout=_HTTP_TIMEOUT_S) as resp:
                text = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                text = ""
        except Exception as e:
            # DNS failure, refused connection, timeout, TLS error — the
            # transient bucket. The message is derived from the network
            # error, which cannot contain the token (it never left this
            # function except in a header) — but scrub anyway.
            raise PortalTransient(
                f"portal unreachable: {self._redact(str(e), token)}")

        if status == 200:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {}
            note = (f"ok: added={parsed.get('added')} "
                    f"updated={parsed.get('updated')} "
                    f"items={parsed.get('items')}")
            self._record_result(scope, ok=True, note=note)
            logger.info(f"portal push {scope}: {note}")
            return parsed

        detail = self._redact(text.strip()[:300], token)
        if status == 403:
            # Broken binding, by contract. Mark it so the enqueue filter
            # stops future pushes, and refuse to be retried.
            self._record_result(
                scope, ok=False, broken=True,
                note=f"403 — edit token rejected; re-bind this project "
                     f"in Settings. {detail}")
            raise PortalBindingBroken("edit token rejected (403)")
        if status in (400, 422):
            self._record_result(scope, ok=False,
                                note=f"{status} — {detail}")
            raise PortalPermanent(f"portal rejected the register "
                                  f"({status}): {detail}")
        # 503 (partial write — the one status that MEANS retry) and any
        # other 5xx.
        self._record_result(scope, ok=False,
                            note=f"{status} — transient; will retry")
        raise PortalTransient(f"portal returned {status}: {detail}")

    @staticmethod
    def _redact(text: str, token: str) -> str:
        """Belt-and-braces: the token must never appear in an error
        message even if a future edit threads response text through one.
        Applied to everything that leaves this module as a message."""
        if token and token in (text or ""):
            return text.replace(token, "[REDACTED]")
        return text or ""
