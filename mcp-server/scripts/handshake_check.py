"""Prove the stdio transport works: spawn the server, initialize, list tools.

Runs a real MCP client over a real stdio subprocess — no in-process
shortcuts. It also calls one tool with no backend running so the
failure text a model would see is visible on stdout.

    python scripts/handshake_check.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mcp import ClientSession, StdioServerParameters, stdio_client  # noqa: E402


async def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    # A token so the server gets past discovery and actually attempts an
    # HTTP call; the port is deliberately one nothing is listening on,
    # so the "app isn't running" path is what we exercise.
    env["MEETING_RECORDER_TOKEN"] = "0" * 64
    env["MEETING_RECORDER_URL"] = "http://127.0.0.1:1"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "meeting_recorder_mcp"],
        env=env,
        cwd=str(REPO),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("=== initialize ===")
            print(f"server:   {init.server_info.name} v{init.server_info.version}")
            print(f"protocol: {init.protocol_version}")
            print(f"tools capability: {init.capabilities.tools is not None}")
            print()
            print("=== instructions ===")
            print(init.instructions or "(none)")
            print()

            print("=== list_tools ===")
            listed = await session.list_tools()
            for tool in listed.tools:
                schema = tool.input_schema
                args = ", ".join(
                    f"{n}{'' if n in schema.get('required', []) else '?'}"
                    for n in schema.get("properties", {})
                )
                ro = getattr(tool.annotations, "read_only_hint", None)
                print(f"- {tool.name}({args})   read_only={ro}")
            print(f"\n{len(listed.tools)} tools.\n")

            print("=== call_tool with the backend down ===")
            result = await session.call_tool(
                "search_meetings", {"query": "cutover window"})
            for block in result.content:
                print(getattr(block, "text", block))
            print()
            print(f"is_error flag: {result.is_error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
