"""
Meeting Recorder v2 — one-command backend setup.

Creates a Python venv inside backend/, installs all dependencies,
and writes a fresh .env if none exists. Run once after cloning.

    cd meeting-recorder-v2
    python setup.py

After this, `npm run tauri dev` or the release .exe will find the
venv at backend/.venv and launch the Python sidecar automatically.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VENV = BACKEND / ".venv"
PYEXE = VENV / "Scripts" / "python.exe"
PIP = VENV / "Scripts" / "pip.exe"


def run(cmd, check=True):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, check=check)


def detect_gpu():
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


def main():
    print("=" * 60)
    print("  Meeting Recorder v2 — Backend Setup")
    print("=" * 60)

    # 1. Venv
    if not VENV.exists():
        print("\n[1/5] Creating virtual environment...")
        run([sys.executable, "-m", "venv", str(VENV)])
    else:
        print("\n[1/5] Venv already exists — skipping")

    # 2. Upgrade bootstrap packages
    print("\n[2/5] Upgrading pip / setuptools / wheel...")
    run([str(PYEXE), "-m", "pip", "install", "--upgrade",
         "pip", "setuptools", "wheel"])

    # 3. Torch (GPU-specific)
    print("\n[3/5] Installing PyTorch...")
    torch_ver, idx = detect_gpu()
    run([str(PIP), "install",
         f"torch=={torch_ver}", f"torchaudio=={torch_ver}",
         "--index-url", idx])

    # 4. All backend deps
    print("\n[4/5] Installing backend dependencies...")
    pkgs = [
        "fastapi>=0.115.0",
        "uvicorn[standard]>=0.30.0",
        "numpy==2.1.3",
        "pyaudiowpatch",
        "anthropic",
        "python-dotenv",
        "sounddevice",
        "soundfile",
        "scipy",
        "matplotlib",
        "faster-whisper",
        "huggingface_hub==0.23.0",
        "pyannote.audio==3.3.2",
        "pywin32",
    ]
    run([str(PIP), "install", *pkgs])

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
