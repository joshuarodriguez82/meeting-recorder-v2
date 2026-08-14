"""Create backend-bundle.zip using Python's zipfile with DEFLATE compression.
Much faster than Compress-Archive for large trees (~3 min vs ~7 min for 1.9GB)."""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
BACKEND = ROOT / "backend"
OUT = ROOT / "backend-bundle.zip"

INCLUDE_DIRS = ["config", "core", "meeting_recorder", "models", "scripts", "services", "utils"]
# Both requirements files ship in the bundle so the Rust shell can pick the
# right one for the host platform at first-launch venv bootstrap time.
INCLUDE_FILES = ["server.py", "requirements-cpu.txt", "requirements-mac.txt"]
# Directories rooted at the REPO ROOT (not backend/) that still need to
# ship inside the runtime bundle, written into the zip under their own
# name so they land as a SIBLING of server.py once extracted (see
# src-tauri/src/lib.rs's ensure_runtime_extracted, which extracts this
# whole zip flat into <data_root>/runtime/). chrome-extension/ ships here
# so the app can write it out to a stable folder on demand instead of the
# user hunting the release page for a separate zip every time it changes
# — see services/extension_bundle_service.py.
EXTRA_ROOT_DIRS = ["chrome-extension"]
# Skip __pycache__ dirs (they contain compiled bytecode for .py files
# which Python regenerates on first import — no need to ship them).
SKIP_PATTERNS = ("__pycache__",)

def should_skip(path: Path) -> bool:
    return any(p in str(path) for p in SKIP_PATTERNS)

if OUT.exists():
    OUT.unlink()

total_bytes = 0
file_count = 0
print(f"Zipping {BACKEND} -> {OUT}")
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for d in INCLUDE_DIRS:
        src_dir = BACKEND / d
        if not src_dir.exists():
            print(f"WARN: {src_dir} does not exist, skipping")
            continue
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [x for x in dirs if not should_skip(Path(root) / x)]
            for f in files:
                full = Path(root) / f
                if should_skip(full):
                    continue
                rel = full.relative_to(BACKEND)
                zf.write(full, rel)
                total_bytes += full.stat().st_size
                file_count += 1
                if file_count % 5000 == 0:
                    print(f"  ... {file_count} files, {total_bytes/1024/1024:.0f} MB source")
    for f in INCLUDE_FILES:
        src = BACKEND / f
        if src.exists():
            zf.write(src, f)
            total_bytes += src.stat().st_size
            file_count += 1
    for d in EXTRA_ROOT_DIRS:
        src_dir = ROOT / d
        if not src_dir.exists():
            print(f"WARN: {src_dir} does not exist, skipping")
            continue
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [x for x in dirs if not should_skip(Path(root) / x)]
            for f in files:
                full = Path(root) / f
                if should_skip(full):
                    continue
                rel = full.relative_to(ROOT)
                zf.write(full, rel)
                total_bytes += full.stat().st_size
                file_count += 1

out_mb = OUT.stat().st_size / 1024 / 1024
print(f"Done. {file_count} files, {total_bytes/1024/1024:.0f} MB source -> {out_mb:.0f} MB zip")
