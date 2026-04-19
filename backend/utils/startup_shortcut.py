"""
Manage the Windows startup shortcut so the app auto-launches on login.
Shortcut lives in %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

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
    """Create a .lnk in the Windows Startup folder."""
    app_root = install_dir()
    pyexe = app_root / ".venv" / "Scripts" / "pythonw.exe"
    main_py = app_root / "main.py"
    icon = app_root / "meeting_recorder.ico"

    if not pyexe.exists() or not main_py.exists():
        logger.warning(f"Cannot enable startup — venv or main.py missing")
        return False

    lnk = startup_shortcut_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)

    icon_line = f'$sc.IconLocation = "{icon}"\n' if icon.exists() else ""
    ps_content = (
        f'$ws = New-Object -ComObject WScript.Shell\n'
        f'$sc = $ws.CreateShortcut("{lnk}")\n'
        f'$sc.TargetPath = "{pyexe}"\n'
        f'$sc.Arguments = \'"{main_py}"\'\n'
        f'$sc.WorkingDirectory = "{app_root}"\n'
        f'$sc.Description = "Launch {APP_NAME}"\n'
        f'{icon_line}'
        f'$sc.Save()\n'
    )

    ps_file = app_root / "_startup_shortcut.ps1"
    ps_file.write_text(ps_content, encoding="utf-8")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps_file)],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and lnk.exists():
            logger.info(f"Startup shortcut installed: {lnk}")
            return True
        logger.warning(f"Startup shortcut creation failed: {r.stderr}")
        return False
    except Exception as e:
        logger.warning(f"Startup shortcut creation errored: {e}")
        return False
    finally:
        try:
            ps_file.unlink()
        except OSError:
            pass


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
