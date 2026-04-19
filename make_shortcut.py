"""
Creates a Meeting Recorder v2 desktop shortcut.

Run once after building the release binary:
    cd meeting-recorder-v2
    npm run tauri build
    python make_shortcut.py
"""

import os
import subprocess
import sys
from pathlib import Path


def get_desktop() -> Path:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        desktop, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        p = Path(desktop)
        if p.exists():
            return p
    except Exception:
        pass
    userprofile = os.environ.get("USERPROFILE", "")
    for c in (Path(userprofile) / "OneDrive" / "Desktop",
              Path(userprofile) / "Desktop"):
        if c.exists():
            return c
    return Path.home() / "Desktop"


def main():
    app_root = Path(__file__).resolve().parent
    exe = app_root / "src-tauri" / "target" / "release" / "meeting-recorder.exe"
    icon = app_root / "src-tauri" / "icons" / "icon.ico"

    if not exe.exists():
        print(f"ERROR: release exe not found at {exe}")
        print("Run `npm run tauri build` first.")
        sys.exit(1)

    desktop = get_desktop()
    lnk = desktop / "Meeting Recorder.lnk"
    print(f"Creating shortcut: {lnk}")
    print(f"  -> {exe}")

    icon_line = f'$sc.IconLocation = "{icon}"\n' if icon.exists() else ""
    ps_content = (
        f'$ws = New-Object -ComObject WScript.Shell\n'
        f'$sc = $ws.CreateShortcut("{lnk}")\n'
        f'$sc.TargetPath = "{exe}"\n'
        f'$sc.WorkingDirectory = "{app_root}"\n'
        f'$sc.Description = "Launch Meeting Recorder"\n'
        f'{icon_line}'
        f'$sc.Save()\n'
        f'Write-Output "OK"\n'
    )
    ps_file = app_root / "_make_shortcut_temp.ps1"
    ps_file.write_text(ps_content, encoding="utf-8")

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps_file)],
            capture_output=True, text=True, timeout=15)
        if "OK" in r.stdout and lnk.exists():
            print(f"\n[SUCCESS] Shortcut created: {lnk}")
            return
        print(f"[WARN] PowerShell output: {r.stdout.strip()}")
        if r.stderr.strip():
            print(f"[WARN] stderr: {r.stderr.strip()}")
    except Exception as e:
        print(f"[WARN] PowerShell method failed: {e}")
    finally:
        try:
            ps_file.unlink()
        except OSError:
            pass

    # Fallback: .bat
    print("Falling back to .bat launcher...")
    bat = desktop / "Meeting Recorder.bat"
    bat.write_text(
        f'@echo off\n'
        f'cd /d "{app_root}"\n'
        f'start "" "{exe}"\n',
        encoding="utf-8")
    print(f"[SUCCESS] .bat launcher: {bat}")


if __name__ == "__main__":
    main()
