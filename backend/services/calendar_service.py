"""
Reads today's Outlook calendar appointments via COM.
Uses default calendar first (fast path), falls back to recursive scan.
Works with Classic Outlook. Gracefully fails with New Outlook.
"""

import datetime
import time
import pythoncom
import win32com.client
from typing import List, Optional
from utils.logger import get_logger

logger = get_logger(__name__)

# Outlook default folder constants
OL_FOLDER_CALENDAR = 9
OL_APPOINTMENT_ITEM = 26


def _get_outlook(retries: int = 3, delay: float = 1.0):
    """
    Connect to Outlook. Tries GetActiveObject first (fast if Outlook is
    already open), falls back to Dispatch (starts Outlook if needed).
    """
    pythoncom.CoInitialize()
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            outlook = win32com.client.GetActiveObject("Outlook.Application")
            logger.info("Connected to running Outlook instance")
            return outlook
        except Exception as e:
            last_err = e
            logger.debug(f"GetActiveObject attempt {attempt+1} failed: {e}")
            try:
                outlook = win32com.client.Dispatch("Outlook.Application")
                logger.info("Connected via Dispatch (started Outlook)")
                return outlook
            except Exception as e2:
                last_err = e2
                logger.debug(f"Dispatch attempt {attempt+1} failed: {e2}")
            if attempt < retries - 1:
                time.sleep(delay)

    logger.warning(
        f"Could not connect to Outlook: {last_err}. "
        "If using New Outlook, switch to Classic or meetings won't load."
    )
    return None


def _parse_appointment(item, today: datetime.date) -> Optional[dict]:
    """Extract meeting info from an Outlook AppointmentItem."""
    try:
        start = item.Start       # pywin32 returns timezone-aware local datetime
        end = item.End

        # Convert pywin32 datetime to a naive local datetime we can compare
        start_local = datetime.datetime(
            start.year, start.month, start.day,
            start.hour, start.minute, start.second)
        end_local = datetime.datetime(
            end.year, end.month, end.day,
            end.hour, end.minute, end.second)

        if start_local.date() != today:
            return None

        duration_min = max(1, int((end_local - start_local).total_seconds() / 60))

        # Extract attendees if available
        attendees = []
        try:
            for r in item.Recipients:
                try:
                    addr = str(getattr(r, "Address", "") or "")
                    name = str(getattr(r, "Name", "") or "")
                    if addr:
                        attendees.append(addr)
                    elif name:
                        attendees.append(name)
                except Exception:
                    continue
        except Exception:
            pass

        return {
            "subject":  str(item.Subject) if item.Subject else "Untitled Meeting",
            "start":    start_local,
            "end":      end_local,
            "location": str(getattr(item, "Location", "") or ""),
            "organizer": str(getattr(item, "Organizer", "") or ""),
            "attendees": attendees,
            "duration": duration_min,
        }
    except Exception as e:
        logger.debug(f"Skipping appointment: {e}")
        return None


def _read_appointments(folder, target_date: datetime.date) -> List[dict]:
    """
    Read appointments from a single calendar folder for a given date.
    Handles recurring meetings by expanding occurrences.

    CRITICAL: For Outlook COM to expand recurrences, you MUST:
      1. Call Sort("[Start]") FIRST
      2. Set IncludeRecurrences = True AFTER sort
      3. Re-apply both to the filtered collection after Restrict
    Otherwise, recurring meetings only return the master event.
    """
    meetings: List[dict] = []
    try:
        items = folder.Items
        # Order matters for recurrence expansion:
        items.Sort("[Start]")
        items.IncludeRecurrences = True

        # Restriction window — one day either side to cover TZ edge cases
        yesterday = target_date - datetime.timedelta(days=1)
        tomorrow = target_date + datetime.timedelta(days=2)
        restriction = (
            f"[Start] >= '{yesterday.strftime('%m/%d/%Y')} 12:00 AM' AND "
            f"[Start] <= '{tomorrow.strftime('%m/%d/%Y')} 11:59 PM'"
        )

        try:
            filtered = items.Restrict(restriction)
            # Must re-apply sort + recurrences to the filtered collection
            filtered.Sort("[Start]")
            filtered.IncludeRecurrences = True
        except Exception as e:
            logger.debug(f"Restrict failed, iterating all items: {e}")
            filtered = items

        count = 0
        for item in filtered:
            count += 1
            if count > 500:
                logger.warning(f"Folder '{folder.Name}' has >500 items, truncating")
                break
            parsed = _parse_appointment(item, target_date)
            if parsed:
                meetings.append(parsed)

        logger.info(f"  '{folder.Name}': {len(meetings)} meetings for "
                    f"{target_date} (scanned {count} items)")
    except Exception as e:
        logger.warning(f"Could not read folder '{getattr(folder, 'Name', '?')}': {e}")
    return meetings


def _scan_folder_recursively(folder, today: datetime.date,
                              seen: set, results: List[dict], depth: int = 0):
    """Walk folder tree looking for calendar folders."""
    if depth > 5:
        return
    try:
        # DefaultItemType 1 = calendar items
        if getattr(folder, "DefaultItemType", -1) == 1:
            name = folder.Name.lower()
            # Skip noise calendars
            if not any(skip in name for skip in
                       ("birthday", "holiday", "contacts", "us holidays")):
                for m in _read_appointments(folder, today):
                    key = (m["subject"], m["start"].isoformat())
                    if key not in seen:
                        seen.add(key)
                        results.append(m)

        # Recurse into subfolders
        for sub in folder.Folders:
            _scan_folder_recursively(sub, today, seen, results, depth + 1)
    except Exception as e:
        logger.debug(f"Skipping folder at depth {depth}: {e}")


def get_todays_meetings() -> List[dict]:
    """
    Return today's meetings from Outlook calendar.
    Returns [] if Outlook isn't accessible.
    """
    outlook = _get_outlook()
    if not outlook:
        return []

    try:
        ns = outlook.GetNamespace("MAPI")
        today = datetime.datetime.now().date()
        all_meetings: List[dict] = []
        seen: set = set()

        # Fast path: default calendar
        try:
            default_cal = ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
            logger.info(f"Reading default calendar: {default_cal.Name}")
            for m in _read_appointments(default_cal, today):
                key = (m["subject"], m["start"].isoformat())
                if key not in seen:
                    seen.add(key)
                    all_meetings.append(m)
        except Exception as e:
            logger.warning(f"Could not read default calendar: {e}")

        # Slow path: scan all stores for additional calendars
        # (shared calendars, resource calendars, etc.)
        try:
            for store in ns.Stores:
                try:
                    root = store.GetRootFolder()
                    logger.info(f"Scanning store: {store.DisplayName}")
                    for folder in root.Folders:
                        _scan_folder_recursively(folder, today, seen, all_meetings)
                except Exception as e:
                    logger.debug(f"Store scan failed: {e}")
        except Exception as e:
            logger.debug(f"Could not enumerate stores: {e}")

        all_meetings.sort(key=lambda m: m["start"])
        logger.info(f"Found {len(all_meetings)} meetings for today")
        return all_meetings

    except Exception as e:
        logger.error(f"Failed to read calendar: {e}", exc_info=True)
        return []
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def is_outlook_available() -> bool:
    """Quick check — true if we can connect to Outlook COM."""
    try:
        pythoncom.CoInitialize()
        try:
            win32com.client.GetActiveObject("Outlook.Application")
            return True
        except Exception:
            try:
                win32com.client.Dispatch("Outlook.Application")
                return True
            except Exception:
                return False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def make_session_name(meeting: dict) -> str:
    """Generate a clean session name from a meeting dict."""
    date_str = meeting["start"].strftime("%Y-%m-%d")
    time_str = meeting["start"].strftime("%H%M")
    subject = meeting["subject"]
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in subject)
    safe = safe.strip().replace("  ", " ")[:48]
    return f"{date_str} {time_str} {safe}"
