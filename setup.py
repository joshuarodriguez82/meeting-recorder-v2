"""
Meeting Recorder v2 — one-command backend setup.

Creates a Python venv inside backend/, installs all dependencies,
and writes a fresh .env if none exists. Run once after cloning.

    cd meeting-recorder-v2
    python setup.py        # Windows
    python3.13 setup.py    # macOS / Linux

IMPORTANT: invoke this with Python 3.13 specifically (`python3.13`, not
`python3`). The default `python3` on macOS is Apple's stock 3.9, which
can't install numpy 2.x or torch 2.6. We bail with an explanatory error
if the running interpreter is older than 3.10.

After this, `npm run tauri dev` or the release .exe / .app will find
the venv at backend/.venv and launch the Python sidecar automatically.
"""

import os
import platform
import re
import subprocess
import sys
from pathlib import Path

# Hard-fail early on too-old Python rather than letting pip print a
# 50-line wall of "Requires-Python >=3.10 ignored" before failing on
# numpy. Mac users hit this when they run `python3 setup.py` (system
# Python 3.9) instead of `python3.13 setup.py` (Homebrew install).
if sys.version_info < (3, 10):
    sys.stderr.write(
        f"\n[ERROR] This setup needs Python 3.10 or newer.\n"
        f"You're running Python {sys.version.split()[0]} from {sys.executable}.\n\n"
    )
    if sys.platform == "darwin":
        sys.stderr.write(
            "On macOS, the default `python3` is Apple's stock 3.9. Install\n"
            "Python 3.13 via Homebrew and rerun explicitly:\n\n"
            "    brew install python@3.13\n"
            "    rm -rf backend/.venv\n"
            "    python3.13 setup.py\n\n"
        )
    else:
        sys.stderr.write(
            "Install a newer Python from python.org or your distro's package\n"
            "manager and rerun this script with that interpreter.\n\n"
        )
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VENV = BACKEND / ".venv"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# Venv layout differs by OS: Scripts/python.exe on Windows, bin/python on POSIX.
if IS_WINDOWS:
    PYEXE = VENV / "Scripts" / "python.exe"
    PIP = VENV / "Scripts" / "pip.exe"
else:
    PYEXE = VENV / "bin" / "python"
    PIP = VENV / "bin" / "pip"


def run(cmd, check=True):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, check=check)


def detect_gpu_windows():
    """Probe for an NVIDIA GPU on Windows via nvidia-smi."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            name = out.stdout.strip().split("\n")[0]
            print(f"  GPU detected: {name}")
            m = re.search(r"RTX\s+(\d{4})", name, re.IGNORECASE)
            if m and int(m.group(1)) >= 5000:
                return ("2.7.0", "https://download.pytorch.org/whl/cu128")
            return ("2.6.0", "https://download.pytorch.org/whl/cu124")
    except Exception:
        pass
    print("  No NVIDIA GPU — using CPU-only torch")
    return ("2.6.0", "https://download.pytorch.org/whl/cpu")


def detect_pytorch_index():
    """Pick the right PyTorch wheel index for this OS + hardware.

    macOS: official wheels include MPS (Apple Silicon GPU acceleration)
    out of the box; no special index needed. Apple Silicon vs Intel is
    autodetected by pip (universal2 wheels).

    Linux: same fallback as Windows-CPU until/unless we add CUDA support.
    """
    if IS_WINDOWS:
        return detect_gpu_windows()
    if IS_MACOS:
        arch = platform.machine()
        if arch == "arm64":
            print(f"  macOS Apple Silicon ({arch}) — torch with MPS support")
        else:
            print(f"  macOS Intel ({arch}) — CPU torch")
        # Default PyPI index ships universal2 wheels for macOS.
        return ("2.6.0", None)
    # Linux fallback — CPU build.
    print("  Linux — using CPU-only torch")
    return ("2.6.0", "https://download.pytorch.org/whl/cpu")


def main():
    print("=" * 60)
    print(f"  Meeting Recorder v2 — Backend Setup ({platform.system()})")
    print("=" * 60)

    # 1. Venv
    if not VENV.exists():
        print("\n[1/5] Creating virtual environment...")
        run([sys.executable, "-m", "venv", str(VENV)])
    else:
        print("\n[1/5] Venv already exists — skipping")

    if not PYEXE.exists():
        sys.exit(f"\n[ERROR] Expected venv Python at {PYEXE} but it's missing. "
                 f"Delete {VENV} and re-run setup.")

    # 2. Upgrade bootstrap packages
    print("\n[2/5] Upgrading pip / setuptools / wheel...")
    run([str(PYEXE), "-m", "pip", "install", "--upgrade",
         "pip", "setuptools", "wheel"])

    # 3. Torch (platform / GPU specific)
    print("\n[3/5] Installing PyTorch...")
    torch_ver, idx = detect_pytorch_index()
    torch_cmd = [str(PYEXE), "-m", "pip", "install",
                 f"torch=={torch_ver}", f"torchaudio=={torch_ver}"]
    if idx:
        torch_cmd += ["--index-url", idx]
    run(torch_cmd)

    # 4. All other backend deps
    print("\n[4/5] Installing backend dependencies...")
    pkgs = [
        "fastapi>=0.115.0",
        "uvicorn[standard]>=0.30.0",
        "numpy==2.1.3",
        "anthropic",
        "openai",
        "python-dotenv",
        "sounddevice",
        "soundfile",
        "scipy",
        "matplotlib",
        "faster-whisper",
        "huggingface_hub==0.23.0",
        "pyannote.audio==3.3.2",
        "speechbrain==1.0.3",
        "pytorch-lightning==2.6.1",
        "lightning==2.6.1",
    ]
    if IS_WINDOWS:
        pkgs += ["pywin32", "pyaudiowpatch"]
    if IS_MACOS:
        # EventKit bridges for the macOS calendar backend. AppleScript
        # email drafting needs no Python binding (uses /usr/bin/osascript).
        pkgs += [
            "pyobjc-core>=10.0",
            "pyobjc-framework-Cocoa>=10.0",
            "pyobjc-framework-EventKit>=10.0",
        ]
    run([str(PYEXE), "-m", "pip", "install", *pkgs])

    # 5. .env
    env_path = BACKEND / ".env"
    if not env_path.exists():
        print("\n[5/5] Creating empty backend/.env...")
        env_path.write_text(
            "ANTHROPIC_API_KEY=\n"
            "HF_TOKEN=\n"
            "WHISPER_MODEL=base\n"
            "MAX_SPEAKERS=10\n"
            f"RECORDINGS_DIR={(ROOT / 'recordings').as_posix()}\n"
            "EMAIL_TO=\n"
            "CLAUDE_MODEL=claude-haiku-4-5\n"
            "NOTIFY_MINUTES_BEFORE=2\n"
            "AUTO_PROCESS_AFTER_STOP=false\n"
            "LAUNCH_ON_STARTUP=false\n"
            "AUTO_FOLLOW_UP_EMAIL=false\n"
            "RETENTION_ENABLED=false\n"
            "RETENTION_PROCESSED_DAYS=7\n"
            "RETENTION_UNPROCESSED_DAYS=30\n",
            encoding="utf-8")
    else:
        print("\n[5/5] backend/.env exists — keeping it")

    print("\n" + "=" * 60)
    print("  BACKEND SETUP COMPLETE")
    print("=" * 60)
    print("\nNext steps:")
    if IS_MACOS:
        print("  1. Install BlackHole for system-audio loopback:")
        print("       brew install blackhole-2ch")
        print("     (See MAC_SETUP.md for the full audio routing walkthrough.)")
        print("  2. Build the app:        npm install && npm run tauri build")
        print("  3. Patch Info.plist:     ./scripts/macos-postbuild.sh")
        print("  4. Open the .app once    so macOS prompts for Mic/Calendar perms")
        print("  5. Open Settings inside  the app to paste your API keys")
    else:
        print("  1. Build the app:       npm install && npm run tauri build")
        print("  2. Create shortcut:     python make_shortcut.py")
        print("  3. Open File > Settings inside the app to paste your API keys")
    print()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] command failed: {e}")
        sys.exit(1)
