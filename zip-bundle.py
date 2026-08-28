"""Create backend-bundle.zip using Python's zipfile with DEFLATE compression.
Much faster than Compress-Archive for large trees (~3 min vs ~7 min for 1.9GB)."""
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
BACKEND = ROOT / "backend"
# Optional output path so a test can build the real bundle without
# clobbering the one a developer or the release workflow just produced
# (backend/tests/test_bundle_contents.py). No argument = the name every
# build step already expects.
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "backend-bundle.zip"

# Written into the zip (never onto disk) as a sibling of server.py so the
# backend can name its own build. A release build runs the backend out of
# the extracted runtime dir, which has no tauri.conf.json and no
# package.json in it — that is why every exported diagnostics bundle
# carried "app_version": null. The Tauri shell also passes
# MEETING_RECORDER_APP_VERSION now; this file is the belt to that
# braces, and it covers a backend started outside the shell.
VERSION_FILE = "app_version.txt"
VERSION_SOURCE = ROOT / "src-tauri" / "tauri.conf.json"

INCLUDE_DIRS = ["config", "core", "meeting_recorder", "models", "scripts", "services", "utils"]
# Both requirements files ship in the bundle so the Rust shell can pick the
# right one for the host platform at first-launch venv bootstrap time.
INCLUDE_FILES = ["server.py", "requirements-cpu.txt", "requirements-mac.txt",
                 # CI-resolved transitive pin sets (freeze-deps.yml). The
                 # bootstrap passes them to pip via -c; absence degrades
                 # to floating resolution (and the existing exists()
                 # check below already tolerates it).
                 "constraints-cpu.txt", "constraints-mac.txt"]
# Directories rooted at the REPO ROOT (not backend/) that still need to
# ship inside the runtime bundle, written into the zip under their own
# name so they land as a SIBLING of server.py once extracted (see
# src-tauri/src/lib.rs's ensure_runtime_extracted, which extracts this
# whole zip flat into <data_root>/runtime/). chrome-extension/ ships here
# so the app can write it out to a stable folder on demand instead of the
# user hunting the release page for a separate zip every time it changes
# — see services/extension_bundle_service.py.
# mcp-server/ ships for the same reason: an AI assistant reaches the
# archive by launching mcp-server/run_mcp_server.py with the app's own
# venv Python, and someone who INSTALLED the app has no checkout to
# launch it from. Settings' "AI assistant access" card resolves both
# absolute paths from the running backend — see
# services/mcp_bundle_service.py.
EXTRA_ROOT_DIRS = ["chrome-extension", "mcp-server"]
# Subdirectories of an EXTRA_ROOT_DIRS entry that must not ship. The MCP
# server's tests/ is excluded for a reason beyond size: run_mcp_server.py
# puts its own directory on sys.path, so a shipped tests/__init__.py
# would become an importable top-level package named `tests` in the
# client's process.
EXTRA_ROOT_SKIP = {"mcp-server": {"tests", "scripts"}}
# Skip __pycache__ dirs (they contain compiled bytecode for .py files
# which Python regenerates on first import — no need to ship them), and
# the tool caches that accumulate next to a dev checkout's sources.
SKIP_PATTERNS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")

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
    try:
        version = json.loads(
            VERSION_SOURCE.read_text(encoding="utf-8"))["version"]
        zf.writestr(VERSION_FILE, f"{version}\n")
        file_count += 1
        print(f"Stamped {VERSION_FILE} = {version}")
    except Exception as e:
        # Not fatal — the shell's env var still carries the version, and
        # a missing marker degrades to the same None the export already
        # handles. But say so loudly in the build log.
        print(f"WARN: could not stamp {VERSION_FILE} from "
              f"{VERSION_SOURCE}: {e}")
    for d in EXTRA_ROOT_DIRS:
        src_dir = ROOT / d
        if not src_dir.exists():
            print(f"WARN: {src_dir} does not exist, skipping")
            continue
        skip_top = EXTRA_ROOT_SKIP.get(d, set())
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [x for x in dirs if not should_skip(Path(root) / x)]
            if Path(root) == src_dir:
                dirs[:] = [x for x in dirs if x not in skip_top]
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
