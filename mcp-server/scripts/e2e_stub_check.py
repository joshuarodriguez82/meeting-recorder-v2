"""End-to-end check: real stdio MCP + real HTTP, against a stub backend.

Stands up a stdlib HTTP server on a free localhost port that serves the
same fixtures the unit tests use (tests/stub_backend.py), points the MCP
server at it with MEETING_RECORDER_URL / MEETING_RECORDER_TOKEN, then
drives every tool through a real stdio MCP client and prints exactly
what a model would receive.

This exercises the whole stack — JSON-RPC framing, HTTP, bearer auth,
SSE parsing, rendering — with nothing mocked except the backend's data.
It is NOT a check against the real Meeting Recorder app; for that, start
the app and run `python -m meeting_recorder_mcp --doctor`.

    python scripts/e2e_stub_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
from mcp import ClientSession, StdioServerParameters, stdio_client  # noqa: E402

from tests import stub_backend  # noqa: E402

TOKEN = stub_backend.VALID_TOKEN
#: One source of truth for the fixture responses: the same MockTransport
#: handler the unit tests use, re-fronted with a real socket.
HANDLER = stub_backend.make_transport().handler


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bridge(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean for the report
        pass

    def _serve(self, body: bytes = b""):
        request = httpx.Request(
            self.command,
            f"http://127.0.0.1{self.path}",
            headers=dict(self.headers),
            content=body,
        )
        response = HANDLER(request)
        payload = response.content
        self.send_response(response.status_code)
        self.send_header(
            "content-type",
            response.headers.get("content-type", "application/json"))
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._serve()

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self._serve(self.rfile.read(length))


async def drive(port: int) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["MEETING_RECORDER_URL"] = f"http://127.0.0.1:{port}"
    env["MEETING_RECORDER_TOKEN"] = TOKEN

    calls = [
        ("list_clients", {}),
        ("list_sessions", {"limit": 3}),
        ("search_meetings", {"query": "when is the cutover?", "top_k": 5}),
        ("ask_knowledge_base", {"question": "When is the ACME cutover and "
                                            "who owns decommission?"}),
        ("get_session", {"session_id": "session_20260714_101500",
                         "part": "summary"}),
        ("get_session", {"session_id": "session_20260714_101500",
                         "part": "transcript"}),
        ("get_session", {"session_id": "session_20260714_101500",
                         "part": "requirements"}),
        ("get_session", {"session_id": "session_does_not_exist",
                         "part": "summary"}),
    ]

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "meeting_recorder_mcp"],
        env=env, cwd=str(REPO))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected to {init.server_info.name} "
                  f"v{init.server_info.version} "
                  f"(protocol {init.protocol_version})\n")
            for name, args in calls:
                print("=" * 72)
                print(f"call_tool {name}({json.dumps(args)})")
                print("=" * 72)
                result = await session.call_tool(name, args)
                for block in result.content:
                    print(getattr(block, "text", block))
                print()


def main() -> int:
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Bridge)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(drive(port))
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
