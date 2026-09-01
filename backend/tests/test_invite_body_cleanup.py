"""
The app cleans invite bodies it ALREADY HAS, at read time.

FIELD SCREENSHOT 2026-08-21, v2.59.0 installed. The panel rendered:

    □
    □
    No location added
    GG
    <organizer> invited you.
    Accepted 1, Didn't respond 5
    Prepare for this meeting
    □
    Turning this to "Daily" 15min on the following and ...
    □
    □
    Accepted

One line of that is the invite. The rest is Outlook's RSVP control,
its attendee tally, Copilot's prompt suggestions, and its toolbar
icons — which are a private-use font, so each icon is a Unicode
Private Use Area character that innerText returns and every font draws
as a hollow box.

Extension 1.17.0 fixes this AT CAPTURE TIME, structurally: the invite
body is read out of its own frame, where Outlook's UI is definitionally
not present. But that only helps text captured AFTERWARDS. Every body
already in the store stays exactly as ugly until something re-captures
that meeting — and the user, reasonably, judges the app by what it is
showing right now.

This is the lesson v2.52.0 already paid for with the attendee scrub: a
cleanup of data the APP OWNS must not be held hostage by whether a
browser is running, or by which extension version the user managed to
reload. So the read path cleans it too.

Deliberately a DROP-ONLY transform on display: it removes lines and
characters, never adds or rewrites, so the worst case for an unknown
tenant is text that is still there. And it is not the primary fix —
the frame-scoped capture is. This is what makes the primary fix visible
today instead of after the next capture.
"""

from __future__ import annotations

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from services.extension_calendar_service import clean_invite_body  # noqa: E402


FIELD_BODY = (
    "\n"
    "\n"
    "No location added\n"
    "GG\n"
    "Jordan Poe invited you.\n"
    "Accepted 1, Didn't respond 5\n"
    "Prepare for this meeting\n"
    "\n"
    'Turning this to "Daily" 15min on the following and Jordan (Acme) '
    "added to the audience for the rest of the month.\n"
    "\n"
    "Accepted\n"
    "Change\n"
    "What are key talking points?\n"
    "Help me prepare for this meeting\n"
    "Help me understand the risks"
)


def test_the_field_body_is_reduced_to_the_invite():
    out = clean_invite_body(FIELD_BODY)
    assert "Turning this to" in out, "the invite itself was dropped"
    # The boxes.
    assert "" not in out and "" not in out
    # Outlook's own controls and tallies.
    for junk in ("No location added", "invited you.", "Didn't respond",
                 "Prepare for this meeting", "Accepted", "Change"):
        assert junk not in out, f"chrome line survived: {junk!r}"
    # Copilot's suggested prompts.
    for junk in ("key talking points", "Help me prepare",
                 "understand the risks"):
        assert junk not in out, f"Copilot prompt survived: {junk!r}"
    # A two-letter avatar monogram is not an agenda.
    assert "\nGG" not in out and out.strip() != "GG"


def test_a_real_invite_is_left_alone():
    """Drop-only means a genuine description passes through intact."""
    body = ("Agenda:\n"
            "1. Review the migration timeline\n"
            "2. Confirm the cutover window\n"
            "\n"
            "Please read the draft SOW before we meet.")
    assert clean_invite_body(body) == body


def test_a_body_that_was_only_chrome_becomes_empty():
    """So the UI can say 'no description' honestly rather than showing
    a wall of buttons."""
    assert clean_invite_body("Join\nChat\nNo location added\n") == ""


def test_lines_that_merely_contain_a_keyword_are_kept():
    """The match is on the whole line, not a substring anywhere — an
    invite that happens to discuss acceptance criteria keeps its text."""
    body = "We need to agree the acceptance criteria and change process."
    assert clean_invite_body(body) == body


def test_none_and_empty_are_safe():
    assert clean_invite_body("") == ""
    assert clean_invite_body(None) == ""


def test_the_cards_echoed_subject_heading_is_dropped():
    """Field screenshot 2026-08-21: the captured agenda opened by
    repeating the meeting title already rendered directly above it."""
    body = ("Northwind demo - use cases\n"
            "Scheduling this time to discuss the use cases for the demo.\n"
            "Email organizer")
    out = clean_invite_body(body, subject="Northwind demo - use cases")
    assert out == "Scheduling this time to discuss the use cases for the demo."


def test_a_subject_repeated_later_in_a_real_body_is_kept():
    """Only the LEADING echo is chrome. An invite that names the
    meeting inside its own text is writing, not a heading."""
    body = ("Agenda below.\n"
            "Northwind demo - use cases\n"
            "Bring the slides.")
    out = clean_invite_body(body, subject="Northwind demo - use cases")
    assert "Northwind demo - use cases" in out


def test_the_series_label_line_is_dropped():
    """Field screenshot 2026-08-23: a recurring meeting's captured
    agenda opened with the card's "Series" toggle label."""
    body = "Series\nTurning this to Daily 15min for the rest of the month."
    assert clean_invite_body(body) == (
        "Turning this to Daily 15min for the rest of the month.")


def test_a_join_link_inside_a_stored_body_is_recovered_at_read_time():
    """A meeting whose body carries the link but whose join_url is
    empty must still offer the link — without waiting for a
    re-capture. Same principle as the v2.52 attendee scrub: fix what
    the app already holds."""
    from services.extension_calendar_service import ExtensionCalendarService

    ev = ExtensionCalendarService._deserialize({
        "subject": "Pulse", "start": "2026-08-25T09:30:00",
        "end": "2026-08-25T09:45:00", "join_url": "",
        "body": "Join the meeting now https://teams.microsoft.com/l/meetup-join/19%3aX/0",
    })
    assert ev["join_url"].startswith(
        "https://teams.microsoft.com/l/meetup-join/")


def test_an_explicit_join_url_is_never_overridden_by_the_body():
    from services.extension_calendar_service import ExtensionCalendarService

    ev = ExtensionCalendarService._deserialize({
        "subject": "Pulse", "start": "2026-08-25T09:30:00",
        "end": "2026-08-25T09:45:00",
        "join_url": "https://acme.webex.com/meet/real",
        "body": "stale https://zoom.us/j/999 forwarded from another invite",
    })
    assert ev["join_url"] == "https://acme.webex.com/meet/real"
