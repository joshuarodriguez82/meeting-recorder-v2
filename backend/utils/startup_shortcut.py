"""
Manage the Windows startup shortcut so the app auto-launches on login.
Shortcut lives in %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup.
"""

import os
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

APP_NAME = "Meeting Recorder"


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup")
    # Fallback
    return (Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def startup_shortcut_path() -> Path:
    return _startup_dir() / f"{APP_NAME}.lnk"


def install_dir() -> Path:
    """Root folder of the app (where main.py lives)."""
    return Path(__file__).resolve().parent.parent


def is_enabled() -> bool:
    return startup_shortcut_path().exists()


def enable() -> bool:
    """
    Create a .lnk in the Windows Startup folder.

    Uses win32com's WScript.Shell directly rather than shelling out to
    powershell.exe. Two reasons:
      1. PowerShell spawn pops a console window even under pythonw on
         locked-down Windows, which the user sees as a black flash.
      2. On corporate laptops, EDR/AppLocker can kill unsigned-parent
         PowerShell invocations, which would take the whole backend down
         (see HANDOFF.md bug #4).
    """
    app_root = install_dir()
    pyexe = app_root / ".venv" / "Scripts" / "pythonw.exe"
    main_py = app_root / "main.py"
    icon = app_root / "meeting_recorder.ico"

    if not pyexe.exists() or not main_py.exists():
        logger.warning("Cannot enable startup — venv or main.py missing")
        return False

    lnk = startup_shortcut_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)

    try:
        import pythoncom
        import win32com.client
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
        logger.warning("Startup shortcut creation returned no file")
        return False
    except Exception as e:
        logger.warning(f"Startup shortcut creation errored: {e}")
        return False


def disable() -> bool:
    """Remove the startup shortcut if it exists."""
    lnk = startup_shortcut_path()
    if lnk.exists():
        try:
            lnk.unlink()
            logger.info(f"Startup shortcut removed: {lnk}")
            return True
        except OSError as e:
            logger.warning(f"Could not remove startup shortcut: {e}")
            return False
    return True


def apply(enabled: bool) -> bool:
    """Sync startup shortcut state to match the desired setting."""
    if enabled:
        return enable()
    return disable()
