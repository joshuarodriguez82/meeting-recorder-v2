"""
Manage the auto-launch-on-login entry so the app comes up when the user
logs in.

  - Windows: a `.lnk` in
    %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup
  - macOS:   a LaunchAgent plist at
    ~/Library/LaunchAgents/com.joshuarodriguez.meeting-recorder.plist
  - Linux:   not implemented (returns False)

Note: this module is currently NOT imported by server.py; the
launch_on_startup setting is persisted but no code applies it. Keeping the
file portable so future wiring works on every supported platform.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

APP_NAME = "Meeting Recorder"
APP_BUNDLE_ID = "com.joshuarodriguez.meeting-recorder"


# ── Path helpers ─────────────────────────────────────────────────────

def install_dir() -> Path:
    """Root folder of the app (where main.py / server.py lives)."""
    return Path(__file__).resolve().parent.parent


# ── Windows backend ──────────────────────────────────────────────────

def _win_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup")
    return (Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def _win_shortcut_path() -> Path:
    return _win_startup_dir() / f"{APP_NAME}.lnk"


def _win_is_enabled() -> bool:
    return _win_shortcut_path().exists()


def _win_enable() -> bool:
    app_root = install_dir()
    pyexe = app_root / ".venv" / "Scripts" / "pythonw.exe"
    main_py = app_root / "main.py"
    icon = app_root / "meeting_recorder.ico"

    if not pyexe.exists() or not main_py.exists():
        logger.warning("Cannot enable startup — venv or main.py missing")
        return False

    lnk = _win_shortcut_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)

    try:
        import pythoncom            # type: ignore
        import win32com.client      # type: ignore
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            sc = shell.CreateShortcut(str(lnk))
            sc.TargetPath = str(pyexe)
            sc.Arguments = f'"{main_py}"'
            sc.WorkingDirectory = str(app_root)
            sc.Description = f"Launch {APP_NAME}"
            if icon.exists():
                sc.IconLocation = str(icon)
            sc.Save()
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        if lnk.exists():
            logger.info(f"Startup shortcut installed: {lnk}")
            return True
        return False
    except Exception as e:
        logger.warning(f"Startup shortcut creation errored: {e}")
        return False


def _win_disable() -> bool:
    lnk = _win_shortcut_path()
    if lnk.exists():
        try:
            lnk.unlink()
            logger.info(f"Startup shortcut removed: {lnk}")
            return True
        except OSError as e:
            logger.warning(f"Could not remove startup shortcut: {e}")
            return False
    return True


# ── macOS backend (LaunchAgent) ──────────────────────────────────────

def _mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{APP_BUNDLE_ID}.plist"


def _mac_is_enabled() -> bool:
    return _mac_plist_path().exists()


def _mac_app_executable() -> Path:
    """The path the LaunchAgent should exec. Prefer the installed .app
    bundle's CLI launcher (`open -a "Meeting Recorder"`), falling back to
    the dev-mode binary if it's all we have."""
    return Path("/usr/bin/open")


def _mac_enable() -> bool:
    plist_path = _mac_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    # Use `open -a` so the LaunchAgent is independent of where the user
    # installed the .app (Applications, ~/Applications, or a custom path —
    # `open -a` resolves the bundle by display name).
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{APP_BUNDLE_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_mac_app_executable()}</string>
        <string>-a</string>
        <string>{APP_NAME}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""
    try:
        plist_path.write_text(plist, encoding="utf-8")
        # Load it now so the user doesn't have to log out/in to verify.
        # `launchctl load -w` is the canonical "enable + start" command.
        import subprocess
        subprocess.run(
            ["/bin/launchctl", "load", "-w", str(plist_path)],
            capture_output=True, timeout=10,
        )
        logger.info(f"LaunchAgent installed: {plist_path}")
        return True
    except Exception as e:
        logger.warning(f"LaunchAgent install failed: {e}")
        return False


def _mac_disable() -> bool:
    plist_path = _mac_plist_path()
    if not plist_path.exists():
        return True
    try:
        import subprocess
        subprocess.run(
            ["/bin/launchctl", "unload", "-w", str(plist_path)],
            capture_output=True, timeout=10,
        )
        plist_path.unlink()
        logger.info(f"LaunchAgent removed: {plist_path}")
        return True
    except Exception as e:
        logger.warning(f"LaunchAgent removal failed: {e}")
        return False


# ── Public API (platform router) ─────────────────────────────────────

def is_enabled() -> bool:
    if sys.platform.startswith("win"):
        return _win_is_enabled()
    if sys.platform == "darwin":
        return _mac_is_enabled()
    return False


def enable() -> bool:
    if sys.platform.startswith("win"):
        return _win_enable()
    if sys.platform == "darwin":
        return _mac_enable()
    logger.warning(f"Auto-launch not supported on {sys.platform}")
    return False


def disable() -> bool:
    if sys.platform.startswith("win"):
        return _win_disable()
    if sys.platform == "darwin":
        return _mac_disable()
    return True


def apply(enabled: bool) -> bool:
    return enable() if enabled else disable()


def startup_shortcut_path() -> Path:
    """Back-compat alias for the path of the OS-specific entry."""
    if sys.platform.startswith("win"):
        return _win_shortcut_path()
    if sys.platform == "darwin":
        return _mac_plist_path()
    return Path("/dev/null")
