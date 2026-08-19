from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meeting_recorder_mcp.client import MeetingRecorderClient  # noqa: E402
from meeting_recorder_mcp.discovery import BackendLocation  # noqa: E402
from tests import stub_backend  # noqa: E402

STUB_LOCATION = BackendLocation(
    base_url="http://127.0.0.1:17645",
    token=stub_backend.VALID_TOKEN,
    token_source="/fake/MeetingRecorder/extension-token",
    token_looks_unusual=False,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Discovery reads process env; never let the host's env leak in."""
    for var in ("MEETING_RECORDER_TOKEN", "MEETING_RECORDER_URL",
                "MEETING_RECORDER_PORT", "MEETING_RECORDER_HOST",
                "MEETING_RECORDER_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)


def make_client(transport=None, **kwargs) -> MeetingRecorderClient:
    return MeetingRecorderClient(
        STUB_LOCATION,
        transport=transport if transport is not None
        else stub_backend.make_transport(**kwargs),
    )


@pytest.fixture
def client():
    return make_client()
