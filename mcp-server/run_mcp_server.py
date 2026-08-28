#!/usr/bin/env python
"""Launch the Meeting Recorder MCP server without installing anything.

An AI client is configured with two absolute paths — an interpreter and
this file — and launches the pair as a child process:

    "<app python>"  "<runtime>/mcp-server/run_mcp_server.py"

That is the whole setup. No virtualenv of its own, no `pip install -e`,
no PYTHONPATH: MCP clients start their servers with a minimal
environment and will not activate a virtualenv or carry your shell's
variables, so anything that depends on either is a support ticket
waiting to happen.

The one thing that has to be arranged is making `meeting_recorder_mcp`
importable, and this file sits right next to it — so it adds its own
directory to sys.path and dispatches to the package's normal entry
point. Appended rather than inserted: if a real installation of the
package is present (a developer's editable checkout, say), that one
should keep winning. This is the fallback for the machine that has no
installation at all, which is every machine that installed the app
rather than cloning the repo.

Run it by hand with --doctor to check the connection to a running app:

    "<app python>" "<runtime>/mcp-server/run_mcp_server.py" --doctor
"""

from __future__ import annotations

import os
import sys


def prepare_sys_path() -> str:
    """Put this file's directory on sys.path. Returns the directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.append(here)
    return here


def main() -> None:
    prepare_sys_path()
    from meeting_recorder_mcp.__main__ import main as run

    run()


if __name__ == "__main__":
    main()
