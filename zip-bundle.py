"""Create backend-bundle.zip using Python's zipfile with DEFLATE compression.
Much faster than Compress-Archive for large trees (~3 min vs ~7 min for 1.9GB)."""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
BACKEND = ROOT / "backend"
OUT = ROOT / "backend-bundle.zip"

INCLUDE_DIRS = ["python", "config", "core", "meeting_recorder", "models", "services", "utils"]
INCLUDE_FILES = ["server.py", "requirements-cpu.txt"]
# Skip __pycache__ dirs (they contain compiled bytecode for .py files
# in site-packages, which Python regenerates on first import — no need
# to ship them). Do NOT add ".pyc" here: the EMBEDDED Python's stdlib
# lives under python/Lib as bare .pyc files (no .py source alongside),
# and excluding .pyc silently strips the entire Python stdlib, making
# Python fail at startup with "No module named 'encodings'".
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

out_mb = OUT.stat().st_size / 1024 / 1024
print(f"Done. {file_count} files, {total_bytes/1024/1024:.0f} MB source -> {out_mb:.0f} MB zip")
