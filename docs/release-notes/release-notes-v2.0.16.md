# v2.0.16

## Custom Summary Templates

The five built-in templates (General, Requirements Gathering, Design Review, Sprint Planning, Stakeholder Update) are now editable, and you can add your own.

- **Edit in Settings → Summary Templates.** Click any template row to edit the prompt Claude follows when summarizing. Adjust the defaults to your style, or write entirely new ones like "AWS Connect Discovery," "SOW Kickoff," "Post-Mortem."
- **Reset to default.** Edited built-ins show an "edited" badge with a revert icon that restores the shipped prompt.
- **Delete / hide.** Custom templates fully delete; defaults hide (so they can be restored).
- Stored in `%LOCALAPPDATA%\MeetingRecorder\summary_templates.json` — backup-able, portable between machines.

## Auto-tag Client from calendar attendees

Clicking **Use** on a calendar meeting now auto-fills the Client field when attendee email domains match a client you've tagged before.

- Learns from your own history — no setup, no config. First few times you tag meetings with `@acme.com` attendees to "Acme," the next matching meeting auto-fills.
- Internal domain (your own `@ttecdigital.com`) is auto-detected as the one that appears in the most sessions and excluded — otherwise every client would look like a match.
- Ties or no overlap → leaves Client blank, you pick manually as before.

## Internals

- New `services/template_service.py` — atomic JSON store in USER_DATA_DIR, seeded with the five built-in defaults on first launch.
- Summarizer `summarize(transcript, prompt, ...)` now takes a resolved prompt string; the template-name-to-prompt lookup moves to the `/sessions/{id}/summarize` endpoint so users can edit prompts without restarting the backend.
- `GET /templates` now returns full `{name, prompt, is_default, default_prompt}` entries instead of just names; Record view + Session Detail dialog extract names for the dropdown.
- Session list API includes `attendees[]` so the Record view can compute client suggestions without a per-session fetch.

## Usage Guide

New **Summary Templates** section. Recording + Calendar sections updated to mention auto-tag.
