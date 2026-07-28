"""An unreadable briefing must not look like an absent one.

Field repro: the Today tab intermittently dropped the whole day's
briefing and rendered its first-run "import a briefing" screen after
switching tabs. The data was on disk the entire time — a transient read
failure returned None, the endpoint turned that into `{}`, and `{}` is
exactly what "you have never imported a briefing" looks like. The user
can't tell those apart, and neither could the frontend.
"""

from pathlib import Path

import pytest

from services.daily_briefing_service import (
    BriefingUnreadableError,
    DailyBriefingService,
)


def _svc(tmp_path: Path) -> DailyBriefingService:
    return DailyBriefingService(tmp_path)


def test_absent_briefing_returns_none(tmp_path: Path):
    # Nothing saved for that date — a legitimate "no briefing" answer.
    assert _svc(tmp_path).get("2026-07-28") is None


def test_unreadable_briefing_raises_instead_of_looking_absent(tmp_path: Path):
    svc = _svc(tmp_path)
    corrupt = tmp_path / "briefings" / "2026-07-28.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{ this is not valid json", encoding="utf-8")

    with pytest.raises(BriefingUnreadableError):
        svc.get("2026-07-28")


def test_a_saved_briefing_reads_back(tmp_path: Path):
    svc = _svc(tmp_path)
    svc.save_parsed({"top_priority": {"title": "Ship v2.19.2"}},
                    date_iso="2026-07-28")
    got = svc.get("2026-07-28")
    assert got is not None
    assert got["date"] == "2026-07-28"


def test_a_corrupt_briefing_can_still_be_overwritten(tmp_path: Path):
    # Writers stay lenient on purpose: if a corrupt file made saving
    # raise too, a single bad write would permanently wedge that date
    # with no way to re-import over it.
    svc = _svc(tmp_path)
    corrupt = tmp_path / "briefings" / "2026-07-28.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{ broken", encoding="utf-8")

    svc.save_parsed({"top_priority": {"title": "Recovered"}},
                    date_iso="2026-07-28")
    assert svc.get("2026-07-28") is not None
