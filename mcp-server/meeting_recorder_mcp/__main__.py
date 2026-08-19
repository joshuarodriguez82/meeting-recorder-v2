"""Entry point: `python -m meeting_recorder_mcp`.

Two modes:
    (no args)   run the stdio MCP server (what Claude Desktop / Claude
                Code invoke).
    --doctor    resolve the backend, try an authenticated call, and
                print a human-readable diagnosis to stdout. This is the
                "is it working?" check the README points at; it never
                speaks MCP, so it's safe to run by hand.
"""

from __future__ import annotations

import asyncio
import sys


def _doctor() -> int:
    from .client import MeetingRecorderClient
    from .discovery import resolve_base_url, token_search_paths
    from .errors import MeetingRecorderError

    print("Meeting Recorder MCP — connection check")
    print(f"  base URL: {resolve_base_url()}")

    try:
        api = MeetingRecorderClient()
    except MeetingRecorderError as exc:
        print("  token:    NOT FOUND")
        print(f"\nFAILED: {exc.message}")
        print("\n  Paths probed for the token file:")
        for path in token_search_paths():
            print(f"    - {path}")
        return 2

    loc = api.location
    masked = loc.token[:6] + "..." + loc.token[-4:] if len(loc.token) > 12 else "***"
    print(f"  token:    {masked}  (from {loc.token_source})")
    if loc.token_looks_unusual:
        print("            NOTE: not the app's usual 64-hex-character format.")

    async def _run() -> int:
        try:
            health = await api.health()
        except MeetingRecorderError as exc:
            print(f"\nFAILED: {exc.message}")
            return 2
        print(f"  health:   ok (backend reports version "
              f"{health.get('version', '?')})")
        try:
            status = await api.verify()
        except MeetingRecorderError as exc:
            print(f"\nFAILED: {exc.message}")
            return 3
        print(f"  auth:     ok")
        print(f"  index:    {status.get('indexed_sessions', 0)} of "
              f"{status.get('total_sessions', 0)} sessions embedded"
              f"{'' if status.get('available', True) else ' (INDEX UNAVAILABLE)'}")
        print("\nOK — the MCP server can reach Meeting Recorder.")
        return 0

    return asyncio.run(_run())


def main() -> None:
    if "--doctor" in sys.argv[1:]:
        raise SystemExit(_doctor())
    from .server import main as run_server
    run_server()


if __name__ == "__main__":
    main()
