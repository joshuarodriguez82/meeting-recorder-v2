use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;
use tauri::Manager;

#[cfg(target_os = "macos")]
mod calendar_macos;

struct BackendProcess(Mutex<Option<Child>>);

/// The backend's TCP port, picked once at startup by binding to
/// 127.0.0.1:0 and asking the OS for a free port. Stored in a OnceLock
/// so every code path (initial spawn, watchdog respawn, the
/// get_backend_port Tauri command, the port-in-use guard) sees the
/// same value for the app's lifetime.
///
/// Why dynamic rather than the old hardcoded 17645: a stale backend
/// from a previously-installed build (or a `tauri dev` session running
/// alongside the production app) was holding the fixed port, so the
/// Tauri shell would log "port already in use, skipping spawn" and
/// silently bind the frontend to whatever older code was already on
/// that port. Now each instance gets its own port and there's no
/// cross-talk.
static BACKEND_PORT: OnceLock<u16> = OnceLock::new();

// Preferred fixed port. Picked so the Chrome extension doesn't need
// to be reconfigured with a new URL after every recorder launch —
// previously the port was always dynamic via bind-port-0, which gave
// us a free port but a different one each launch. Field repro
// 2026-06-30: user complained about re-pasting the URL every launch.
// 17645 is in the IANA "Dynamic and/or Private" range (49152-65535
// is technically reserved but 17645 has been in use for this app
// since v2.1, with zero observed conflicts on real user machines).
// If 17645 is taken (typically only when a stale backend from a
// previously-running build is holding it, which the v2.10 single-
// instance lock now prevents anyway), we fall back to bind-port-0
// and the user re-pastes — same UX as before, but the common case
// is now stable.
const PREFERRED_PORT: u16 = 17645;

fn pick_free_port() -> u16 {
    // Try the preferred fixed port first. If it binds, we keep it
    // (drop the listener so Python can bind it next, same TOCTOU
    // window as the old behavior — in practice never observed).
    if let Ok(listener) = TcpListener::bind(("127.0.0.1", PREFERRED_PORT)) {
        drop(listener);
        return PREFERRED_PORT;
    }
    // Fallback: bind port 0 and let the OS pick a free one.
    let listener = TcpListener::bind("127.0.0.1:0")
        .expect("Failed to bind 127.0.0.1:0 to discover a free port");
    let port = listener
        .local_addr()
        .expect("Failed to read local_addr from free-port listener")
        .port();
    drop(listener);
    port
}

fn backend_port() -> u16 {
    *BACKEND_PORT.get_or_init(pick_free_port)
}

/// Per-launch shared secret between the three trusted parties: this
/// shell, the Python sidecar, and the WebView frontend. The sidecar
/// binds 127.0.0.1 only, but localhost is not an auth boundary — any
/// webpage in any browser on the machine can fetch http://127.0.0.1:*
/// and, before this token existed, read transcripts or start
/// recordings. The token closes that: Python rejects any request that
/// doesn't present it, and only the two channels below ever carry it
/// (env var to the child process, Tauri IPC to the frontend) — neither
/// is reachable from a foreign web page.
///
/// v2.16+: persisted to disk at USER_DATA_DIR/extension-token, so the
/// Chrome extension doesn't need its config re-pasted after every
/// recorder launch. The file gets the user's account ACL (Windows
/// default for files under %LOCALAPPDATA%), so other user accounts
/// on the same machine can't read it. Threat model is "other apps
/// running as my user" which is unchanged — those could already
/// read every file I own. To rotate the token: delete the file and
/// restart the recorder.
static BACKEND_TOKEN: OnceLock<String> = OnceLock::new();

fn generate_backend_token() -> String {
    // First, try to read a persisted token. If present and well-
    // formed (64 hex chars), reuse it so the extension's saved
    // token still works after a recorder restart.
    if let Some(persisted) = read_persisted_token() {
        return persisted;
    }
    // Generate a fresh one. 32 bytes from the OS CSPRNG, hex-
    // encoded (64 chars). getrandom is the same primitive `rand`
    // sits on top of — no need for the bigger crate just to fill
    // one buffer.
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes)
        .expect("OS CSPRNG unavailable — cannot generate backend auth token");
    let mut token = String::with_capacity(64);
    for b in bytes {
        token.push_str(&format!("{:02x}", b));
    }
    // Persist the new token. Best-effort: a write failure (read-only
    // FS, AV interference) means the user re-pastes on next launch,
    // same as the pre-v2.16 behavior — no functional regression.
    let _ = write_persisted_token(&token);
    token
}

fn token_file_path() -> std::path::PathBuf {
    data_root_dir().join("extension-token")
}

fn read_persisted_token() -> Option<String> {
    let path = token_file_path();
    let contents = std::fs::read_to_string(&path).ok()?;
    let trimmed = contents.trim().to_string();
    // Validate: 64 hex chars. Anything else, ignore and regenerate.
    if trimmed.len() == 64 && trimmed.chars().all(|c| c.is_ascii_hexdigit()) {
        Some(trimmed)
    } else {
        None
    }
}

fn write_persisted_token(token: &str) -> std::io::Result<()> {
    let path = token_file_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, token)
}

fn backend_token() -> &'static str {
    BACKEND_TOKEN.get_or_init(generate_backend_token)
}

/// Tauri command surfaced to the frontend. Called once on app start
/// (see api.ts → getBaseUrl) so the JS knows where the backend lives.
#[tauri::command]
fn get_backend_port() -> u16 {
    backend_port()
}

/// Tauri command surfaced to the frontend. Called once on app start
/// (see api.ts → getAuthToken); every backend request must carry this
/// value, either as `Authorization: Bearer <token>` or — for the
/// header-less consumers (EventSource, <audio>/<img> src) — as a
/// `?token=` query parameter.
#[tauri::command]
fn get_backend_token() -> String {
    backend_token().to_string()
}

/// Set while bootstrap_app_venv is running (can take several minutes on
/// first launch). The watchdog respects this so it doesn't try to
/// "respawn" a backend that hasn't been spawned yet.
static BOOTSTRAPPING: AtomicBool = AtomicBool::new(false);

// ─── Platform helpers ───────────────────────────────────────────────
//
// The whole shell is structured around three platform abstractions:
//   - data_root_dir():  per-user state dir (LOCALAPPDATA on Win, Application
//                        Support on Mac, $XDG_DATA_HOME on Linux)
//   - venv_python():    where Python lands inside a venv
//                        (Scripts\python.exe vs bin/python)
//   - find_system_python(): how we locate a system Python to bootstrap
//                        the venv from
// Everything else is shared.

#[cfg(windows)]
fn data_root_dir() -> std::path::PathBuf {
    let base = std::env::var("LOCALAPPDATA")
        .or_else(|_| std::env::var("APPDATA"))
        .unwrap_or_else(|_| std::env::var("USERPROFILE").unwrap_or_default());
    let dir = std::path::PathBuf::from(base).join("MeetingRecorder");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

#[cfg(target_os = "macos")]
fn data_root_dir() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    let dir = std::path::PathBuf::from(home)
        .join("Library").join("Application Support").join("MeetingRecorder");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

#[cfg(all(unix, not(target_os = "macos")))]
fn data_root_dir() -> std::path::PathBuf {
    let base = std::env::var("XDG_DATA_HOME")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| {
            let home = std::env::var("HOME").unwrap_or_default();
            format!("{}/.local/share", home)
        });
    let dir = std::path::PathBuf::from(base).join("MeetingRecorder");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// Python interpreter path inside a venv directory. POSIX venvs put it
/// at `<venv>/bin/python`; Windows at `<venv>\Scripts\python.exe`.
#[cfg(windows)]
fn venv_python_candidates(venv: &std::path::Path) -> Vec<std::path::PathBuf> {
    vec![
        venv.join("Scripts").join("pythonw.exe"),
        venv.join("Scripts").join("python.exe"),
    ]
}

#[cfg(unix)]
fn venv_python_candidates(venv: &std::path::Path) -> Vec<std::path::PathBuf> {
    // pythonw doesn't exist on POSIX. `python` is a symlink to python3 in
    // every modern venv layout, but we check both for old-toolchain venvs.
    vec![
        venv.join("bin").join("python3"),
        venv.join("bin").join("python"),
    ]
}

/// Kill any pythonw/python processes that were spawned by a previous
/// launch of this app but never cleaned up — leaving them running can
/// keep recording audio silently in the background. Identifies orphans
/// by executable path matching `python_exe` (the venv's pythonw.exe).
///
/// Windows-only because the orphan-accumulation scenario only reproduces
/// there reliably; macOS process management handles this case cleanly.
fn kill_orphan_backends(python_exe: &std::path::Path) {
    #[cfg(windows)]
    {
        // Pull every process whose ExecutablePath matches our venv's
        // pythonw.exe. The `wmic` CLI is deprecated in Windows 11 but
        // still ships; PowerShell's Get-CimInstance is the modern
        // replacement. Try Get-CimInstance first, fall back to wmic.
        let target = python_exe.to_string_lossy().replace('\\', "\\\\");
        let ps_cmd = format!(
            "Get-CimInstance Win32_Process | \
             Where-Object {{ $_.ExecutablePath -eq '{}' }} | \
             ForEach-Object {{ \
                 Write-Output \"orphan-pid=$($_.ProcessId)\"; \
                 Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue \
             }}",
            python_exe.to_string_lossy().replace('\'', "''")
        );

        let mut cmd = Command::new("powershell");
        cmd.arg("-NoProfile").arg("-NonInteractive")
           .arg("-Command").arg(&ps_cmd);
        no_window(&mut cmd);

        match cmd.output() {
            Ok(out) => {
                let stdout = String::from_utf8_lossy(&out.stdout);
                let pids: Vec<&str> = stdout.lines()
                    .filter(|l| l.starts_with("orphan-pid="))
                    .collect();
                if pids.is_empty() {
                    rlog("No orphan backends found");
                } else {
                    rlog(&format!(
                        "Killed {} orphan backend(s): {} (target: {})",
                        pids.len(),
                        pids.join(", "),
                        target,
                    ));
                }
            }
            Err(e) => {
                rlog(&format!(
                    "Orphan-backend scan failed (continuing anyway): {}", e));
            }
        }
    }
    #[cfg(not(windows))]
    {
        let _ = python_exe; // silence unused-var warning
    }
}

/// Apply the Windows CREATE_NO_WINDOW flag if compiled for Windows. No-op
/// on POSIX where there's no console-flash issue to suppress.
fn no_window(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}

/// Where the backend zip is installed on disk by Tauri.
fn resolve_bundle_zip() -> Option<std::path::PathBuf> {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            // Tauri installs resources alongside the exe by default; check
            // a few conventional subdirs just to be safe across bundler
            // versions (wix / nsis / portable on Win; .app/Contents/Resources
            // on Mac).
            let mut candidates = vec![
                dir.join("resources").join("backend-bundle.zip"),
                dir.join("backend-bundle.zip"),
                dir.join("resources").join("_up_").join("backend-bundle.zip"),
            ];
            // macOS: the binary lives at MyApp.app/Contents/MacOS/<exe>;
            // resources land at MyApp.app/Contents/Resources/.
            #[cfg(target_os = "macos")]
            {
                if let Some(macos_dir) = dir.parent() {
                    candidates.push(
                        macos_dir.join("Resources").join("backend-bundle.zip"));
                    candidates.push(
                        macos_dir.join("Resources").join("_up_").join("backend-bundle.zip"));
                }
            }
            for c in &candidates {
                if c.exists() {
                    return Some(c.clone());
                }
            }
        }
    }
    // Dev checkout — same path on every platform: a sibling backend-bundle.zip
    // in the working directory or at the repo root if launched from src-tauri.
    for dev in [
        std::path::PathBuf::from("backend-bundle.zip"),
        std::path::PathBuf::from("../backend-bundle.zip"),
        #[cfg(windows)]
        std::path::PathBuf::from(r"C:\meeting-recorder-v2\backend-bundle.zip"),
    ] {
        if dev.exists() {
            return Some(dev);
        }
    }
    None
}

/// Where the extracted runtime lives per-user. Writable, survives app
/// updates, cleaned up only if the user explicitly removes the data root.
fn runtime_dir() -> std::path::PathBuf {
    let d = data_root_dir().join("runtime");
    let _ = std::fs::create_dir_all(&d);
    d
}

/// Content fingerprint of the bundled zip — stable across rebuilds that
/// produce byte-identical zips and across installed-vs-dev zip paths.
/// Used to decide whether the extracted runtime is stale and must be
/// re-extracted (which nukes user-installed GPU torch). Reads the first
/// 64 KB + last 64 KB + file size; collision probability is effectively
/// zero for our use case and it avoids pulling in a hash crate.
fn zip_version(zip_path: &std::path::Path) -> String {
    use std::io::{Read, Seek, SeekFrom};
    let mut f = match std::fs::File::open(zip_path) {
        Ok(f) => f,
        Err(_) => return "unknown".to_string(),
    };
    let len = f.metadata().ok().map(|m| m.len()).unwrap_or(0);

    let window: u64 = 64 * 1024;
    let mut head = vec![0u8; window.min(len) as usize];
    let _ = f.seek(SeekFrom::Start(0));
    let _ = f.read_exact(&mut head);
    let mut tail = vec![0u8; window.min(len) as usize];
    if len > window {
        let _ = f.seek(SeekFrom::Start(len - window));
        let _ = f.read_exact(&mut tail);
    }
    // Simple FNV-1a over head + tail. Good enough for "is this the same
    // zip?" — not a security boundary.
    let mut hash: u64 = 0xcbf29ce484222325;
    for b in head.iter().chain(tail.iter()) {
        hash ^= *b as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("len={}-fnv={:016x}", len, hash)
}

/// Extract the bundled backend zip into the per-user runtime directory
/// if it doesn't already exist or if the bundled version changed.
/// Uses the system `tar` (BSD libarchive on macOS / Windows 10+; GNU tar
/// on Linux). All three understand `tar -xf foo.zip`.
fn ensure_runtime_extracted(zip_path: &std::path::Path) -> Result<std::path::PathBuf, String> {
    let runtime = runtime_dir();
    let version_file = runtime.join(".version");
    let expected_version = zip_version(zip_path);

    // Essential files that MUST exist for the runtime to work. v2.0.3+
    // ships a source-only bundle (no embeddable Python), so server.py is
    // the only essential.
    let essentials = [runtime.join("server.py")];
    let missing: Vec<_> = essentials.iter()
        .filter(|p| !p.exists())
        .map(|p| p.to_path_buf())
        .collect();

    let marker = std::fs::read_to_string(&version_file).unwrap_or_default();
    let version_changed = marker != expected_version;
    let needs_extract = !missing.is_empty() || version_changed;

    if needs_extract {
        if version_changed && missing.is_empty() {
            rlog(&format!(
                "Bundled zip changed (was '{}', now '{}') — re-extracting so \
                 the new backend code takes effect.",
                if marker.is_empty() { "<none>" } else { &marker },
                expected_version));
        } else {
            rlog(&format!("Runtime extraction needed (missing: {:?})", missing));
        }
        // Clean slate — remove old runtime so stale .pyc files don't linger.
        let _ = std::fs::remove_dir_all(&runtime);
        std::fs::create_dir_all(&runtime).map_err(|e| format!("mkdir runtime: {}", e))?;
        rlog(&format!("Extracting {} -> {}", zip_path.display(), runtime.display()));
        let t0 = std::time::Instant::now();
        let mut tar_cmd = Command::new("tar");
        tar_cmd
            .arg("-xf").arg(zip_path)
            .arg("-C").arg(&runtime)
            .stdout(Stdio::null()).stderr(Stdio::null());
        no_window(&mut tar_cmd);
        let status = tar_cmd
            .status()
            .map_err(|e| format!("tar failed to run: {}", e))?;
        if !status.success() {
            return Err(format!("tar exited with {}", status));
        }
        std::fs::write(&version_file, &expected_version)
            .map_err(|e| format!("writing .version: {}", e))?;
        rlog(&format!("Extracted in {:.1}s", t0.elapsed().as_secs_f32()));
    }

    Ok(runtime)
}

/// Resolve the installed backend directory.
///
/// Two backend sources can exist on disk simultaneously:
///   - The extracted runtime under <data_root>/runtime/, populated from
///     a backend-bundle.zip resource. This is what production .app /
///     installer builds always use.
///   - The dev-checkout `backend/` source dir next to the repo root.
///     This is what we want during `cargo run` / `tauri dev` so that
///     edits to backend/*.py are picked up the moment the watchdog
///     respawns the Python child — no `python zip-bundle.py` step.
///
/// Resolution order:
///   - Debug builds (`cargo run`, `tauri dev`): dev `backend/` first,
///     extracted runtime as fallback. Frees iteration from the zip
///     dance during development.
///   - Release builds (`tauri build`): extracted runtime first, dev
///     fallback only if extraction failed and there's a co-located
///     source tree (rare — covers the case of running the release exe
///     directly from a checkout for debugging).
fn resolve_backend_dir() -> Option<std::path::PathBuf> {
    // Same dev candidates regardless of build mode — used either as
    // primary (debug) or fallback (release).
    let dev_candidates: Vec<std::path::PathBuf> = vec![
        std::path::PathBuf::from("backend"),
        std::path::PathBuf::from("../backend"),
        #[cfg(windows)]
        std::path::PathBuf::from(r"C:\meeting-recorder-v2\backend"),
    ];
    let pick_dev = || -> Option<std::path::PathBuf> {
        for c in &dev_candidates {
            if c.join("server.py").exists() {
                return Some(c.clone());
            }
        }
        None
    };

    if cfg!(debug_assertions) {
        if let Some(d) = pick_dev() {
            rlog(&format!(
                "Debug build: using dev backend dir at {} (zip ignored \
                 in debug mode so backend/*.py edits land immediately).",
                d.display()));
            return Some(d);
        }
        // No dev source — fall through to the bundled-zip path so a
        // standalone debug binary still has a backend to spawn.
    }

    if let Some(zip) = resolve_bundle_zip() {
        match ensure_runtime_extracted(&zip) {
            Ok(d) => {
                if d.join("server.py").exists() {
                    return Some(d);
                }
                rlog("Extraction ran but server.py not found — bundle may be corrupted");
            }
            Err(e) => rlog(&format!("Runtime extract failed: {}", e)),
        }
    }

    // Final fallback (release-mode only path here; debug already tried it).
    pick_dev()
}

/// Where the app-managed venv lives. Created by bootstrap_app_venv on
/// first launch if no other Python is available.
fn app_venv_dir() -> std::path::PathBuf {
    data_root_dir().join(".venv")
}

/// Locate a working Python interpreter.
///
/// Priority:
///   1. App-managed venv created by bootstrap (production path on
///      clean machines: first launch creates this via `python -m venv`
///      against a detected system Python 3.13).
///   2. Dev checkout venv next to server.py.
///   3. Legacy v1 venv (Windows only, original dev machine).
fn resolve_python(backend_dir: &std::path::Path) -> Option<std::path::PathBuf> {
    let app_venv = app_venv_dir();
    for c in venv_python_candidates(&app_venv) {
        if c.exists() {
            return Some(c);
        }
    }
    let dev_venv = backend_dir.join(".venv");
    for c in venv_python_candidates(&dev_venv) {
        if c.exists() {
            return Some(c);
        }
    }
    #[cfg(windows)]
    {
        let legacy = std::path::PathBuf::from(r"C:\meeting_recorder\.venv\Scripts\pythonw.exe");
        if legacy.exists() {
            return Some(legacy);
        }
    }
    None
}

/// Locate a system Python to build the app venv from. Prefers 3.12,
/// falls back to 3.13. Why prefer the older one: the deliberate
/// `huggingface_hub==0.23` pin (kept until pyannote fixes its newer-hub
/// breakage) drags in tokenizers 0.19, which ships NO cp313 wheels —
/// on 3.13 pip silently switches to compiling it from Rust source and
/// fails on any machine without a toolchain (the sales-laptop case).
/// On 3.12 every pinned wheel is prebuilt, so the install just works.
#[cfg(windows)]
fn find_system_python() -> Option<std::path::PathBuf> {
    for ver in ["3.12", "3.13"] {
        // 1. `py -<ver> -c "print(sys.executable)"`
        let mut cmd = Command::new("py");
        cmd.arg(format!("-{ver}"))
            .args(["-c", "import sys; print(sys.executable)"])
            .stdout(Stdio::piped()).stderr(Stdio::null());
        no_window(&mut cmd);
        if let Ok(out) = cmd.output() {
            if out.status.success() {
                let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
                if !path.is_empty() {
                    let p = std::path::PathBuf::from(&path);
                    if p.exists() { return Some(p); }
                }
            }
        }

        // 2. Common per-user / machine install paths.
        let dir = format!("Python{}", ver.replace('.', ""));
        let mut candidates: Vec<std::path::PathBuf> = Vec::new();
        if let Ok(localappdata) = std::env::var("LOCALAPPDATA") {
            candidates.push(std::path::PathBuf::from(&localappdata)
                .join("Programs").join("Python").join(&dir).join("python.exe"));
        }
        candidates.push(std::path::PathBuf::from(
            format!(r"C:\Program Files\{dir}\python.exe")));
        candidates.push(std::path::PathBuf::from(
            format!(r"C:\Program Files (x86)\{dir}\python.exe")));
        for c in candidates {
            if c.exists() { return Some(c); }
        }
    }

    // 3. Last resort: `python` on PATH, accepted only if 3.12 / 3.13.
    let mut cmd = Command::new("python");
    cmd.args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print(sys.executable)"])
        .stdout(Stdio::piped()).stderr(Stdio::null());
    no_window(&mut cmd);
    if let Ok(out) = cmd.output() {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout).to_string();
            let mut lines = text.lines();
            if let (Some(ver), Some(exe)) = (lines.next(), lines.next()) {
                if matches!(ver.trim(), "3.12" | "3.13") {
                    let p = std::path::PathBuf::from(exe.trim());
                    if p.exists() { return Some(p); }
                }
            }
        }
    }
    None
}

#[cfg(target_os = "macos")]
fn find_system_python() -> Option<std::path::PathBuf> {
    for ver in ["3.12", "3.13"] {
        // 1. pythonX.YY on PATH (Homebrew: `brew install python@3.12`).
        let mut cmd = Command::new(format!("python{ver}"));
        cmd.args(["-c", "import sys; print(sys.executable)"])
            .stdout(Stdio::piped()).stderr(Stdio::null());
        if let Ok(out) = cmd.output() {
            if out.status.success() {
                let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
                if !path.is_empty() {
                    let p = std::path::PathBuf::from(&path);
                    if p.exists() { return Some(p); }
                }
            }
        }

        // 2. Standard install locations: Homebrew (ARM/Intel),
        //    python.org installer, pyenv shim.
        let candidates: Vec<std::path::PathBuf> = vec![
            std::path::PathBuf::from(format!("/opt/homebrew/bin/python{ver}")),
            std::path::PathBuf::from(format!("/usr/local/bin/python{ver}")),
            std::path::PathBuf::from(format!(
                "/Library/Frameworks/Python.framework/Versions/{ver}/bin/python{ver}")),
            std::path::PathBuf::from(format!("{}/.pyenv/versions/{ver}.0/bin/python{ver}",
                std::env::var("HOME").unwrap_or_default())),
        ];
        for c in candidates {
            if c.exists() { return Some(c); }
        }
    }

    // 3. Last resort: `python3` on PATH, accepted only if 3.12 / 3.13.
    let mut cmd = Command::new("python3");
    cmd.args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print(sys.executable)"])
        .stdout(Stdio::piped()).stderr(Stdio::null());
    if let Ok(out) = cmd.output() {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout).to_string();
            let mut lines = text.lines();
            if let (Some(ver), Some(exe)) = (lines.next(), lines.next()) {
                if matches!(ver.trim(), "3.12" | "3.13") {
                    let p = std::path::PathBuf::from(exe.trim());
                    if p.exists() { return Some(p); }
                }
            }
        }
    }
    None
}

#[cfg(all(unix, not(target_os = "macos")))]
fn find_system_python() -> Option<std::path::PathBuf> {
    for ver in ["3.12", "3.13"] {
        let mut cmd = Command::new(format!("python{ver}"));
        cmd.args(["-c", "import sys; print(sys.executable)"])
            .stdout(Stdio::piped()).stderr(Stdio::null());
        if let Ok(out) = cmd.output() {
            if out.status.success() {
                let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
                if !path.is_empty() {
                    let p = std::path::PathBuf::from(&path);
                    if p.exists() { return Some(p); }
                }
            }
        }
        for c in [format!("/usr/bin/python{ver}"),
                  format!("/usr/local/bin/python{ver}")] {
            let p = std::path::PathBuf::from(&c);
            if p.exists() { return Some(p); }
        }
    }

    // Last resort: `python3` on PATH, accepted only if 3.12 / 3.13.
    let mut cmd = Command::new("python3");
    cmd.args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print(sys.executable)"])
        .stdout(Stdio::piped()).stderr(Stdio::null());
    if let Ok(out) = cmd.output() {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout).to_string();
            let mut lines = text.lines();
            if let (Some(ver), Some(exe)) = (lines.next(), lines.next()) {
                if matches!(ver.trim(), "3.12" | "3.13") {
                    let p = std::path::PathBuf::from(exe.trim());
                    if p.exists() { return Some(p); }
                }
            }
        }
    }
    None
}

/// Human-readable instruction for installing Python 3.12. Surfaced in the
/// error message when we can't find one — different per platform because
/// the install method differs (py.org installer vs Homebrew vs apt).
/// 3.12 (not 3.13) on purpose: see find_system_python().
fn python_install_instructions() -> &'static str {
    #[cfg(windows)]
    { "Install Python 3.12 from https://www.python.org/downloads/ \
       (per-user install, no admin needed; check 'Add python.exe to PATH'), \
       then restart Meeting Recorder." }
    #[cfg(target_os = "macos")]
    { "Install Python 3.12 with Homebrew: `brew install python@3.12`. \
       (If you don't have Homebrew, install it from https://brew.sh first.) \
       Then restart Meeting Recorder." }
    #[cfg(all(unix, not(target_os = "macos")))]
    { "Install Python 3.12 from your distro's package manager (e.g. `apt install python3.12`) \
       and restart Meeting Recorder." }
}

/// Pick the right requirements file for this platform. macOS and Linux
/// don't have pyaudiowpatch / pywin32 in their wheel index, so they need
/// a slimmed list. requirements-cpu.txt is the canonical Windows file;
/// requirements-mac.txt is its Unix sibling.
fn requirements_filename() -> &'static str {
    #[cfg(target_os = "macos")]
    { "requirements-mac.txt" }
    #[cfg(target_os = "linux")]
    { "requirements-mac.txt" }   // same dependency set works on Linux
    #[cfg(windows)]
    { "requirements-cpu.txt" }
}

/// Create the app venv and pip install requirements into it. Blocks for
/// several minutes on first launch while wheels download (~1.5 GB). All
/// pip output goes to <data_root>/bootstrap.log so the user can tail it.
fn bootstrap_app_venv(runtime_dir: &std::path::Path) -> Result<std::path::PathBuf, String> {
    let venv = app_venv_dir();
    let req_name = requirements_filename();
    let reqs = runtime_dir.join(req_name);
    if !reqs.exists() {
        return Err(format!(
            "{} not found at {} — bundle may be corrupted",
            req_name, reqs.display()));
    }
    let current_reqs = std::fs::read_to_string(&reqs).map_err(|e| {
        format!("read {} failed: {}", reqs.display(), e)
    })?;
    // Marker file lets us detect when the bundled requirements changed
    // between app versions (e.g. an upgrade adds sentence-transformers
    // for semantic search). Without it, an existing venv from v2.0.x
    // would be reused for v2.1.0 even though the new code expects
    // packages the venv doesn't have. Storing the file content rather
    // than a hash makes the diff trivially auditable from the user's
    // bootstrap.log if anything goes wrong.
    let reqs_marker = venv.join("requirements.installed.txt");

    let venv_py_existing = venv_python_candidates(&venv).into_iter()
        .find(|p| p.exists());

    if venv_py_existing.is_some() {
        let installed_reqs = std::fs::read_to_string(&reqs_marker).unwrap_or_default();
        if !installed_reqs.is_empty() && installed_reqs == current_reqs {
            rlog("App venv up to date — bootstrap skipped");
            return Ok(venv);
        }
        rlog("App venv exists but requirements changed — re-running pip install");
    }

    let bootstrap_log_path = log_dir().join("bootstrap.log");
    let open_log = || -> Result<(File, File), String> {
        let f = OpenOptions::new().create(true).append(true)
            .open(&bootstrap_log_path)
            .map_err(|e| format!("opening bootstrap.log: {}", e))?;
        let f2 = f.try_clone().map_err(|e| format!("cloning log fd: {}", e))?;
        Ok((f, f2))
    };

    // Capture whether we're about to create a fresh venv BEFORE the
    // match below moves `venv_py_existing` out — used downstream by the
    // pip-install failure policy (fresh → fatal, upgrade → best-effort).
    let was_fresh_venv = venv_py_existing.is_none();

    let venv_py = match venv_py_existing {
        Some(p) => p,
        None => {
            // First-time install: create the venv from scratch with system Python.
            let system_py = find_system_python().ok_or_else(|| {
                format!("Python 3.12 not found on this machine. {}",
                        python_install_instructions())
            })?;
            rlog(&format!("Bootstrap: system Python at {}", system_py.display()));

            // Step 1: python -m venv
            rlog(&format!("Bootstrap: creating venv at {}", venv.display()));
            let (out, err) = open_log()?;
            let mut c = Command::new(&system_py);
            c.args(["-m", "venv"]).arg(&venv)
                .stdout(Stdio::from(out)).stderr(Stdio::from(err));
            no_window(&mut c);
            let status = c.status().map_err(|e| format!("venv cmd failed: {}", e))?;
            if !status.success() {
                return Err(format!("python -m venv exited with {} (see bootstrap.log)", status));
            }

            let p = venv_python_candidates(&venv).into_iter()
                .find(|p| p.exists())
                .ok_or_else(|| format!("venv python missing after create under {}", venv.display()))?;
            p
        }
    };

    // Step 2: enforce a pip version range the project was actually
    // tested against. Idempotent — if pip is already inside the range
    // this is a sub-second no-op; if it's too new it gets downgraded.
    //
    // Why a cap at all: the bootstrap used to do `pip install --upgrade
    // pip` unconditionally on first create. Every new pip release ships
    // resolver and metadata-validation changes (24.1 added strict
    // metadata, 25/26 tightened it further), and they routinely break
    // requirement sets that were green on the previous pip. v2.10.4 in
    // the field hit exactly this: a fresh venv self-upgraded to pip
    // 26.1.2, which then refused the pyannote.audio → omegaconf →
    // antlr4-python3-runtime chain ("ResolutionImpossible … no matching
    // distributions available for your environment: antlr4-python3-runtime"),
    // and the watchdog respawned the backend five times into the same
    // failure before giving up.
    //
    // Capping pip below 25 pins the resolver to the version the wheel
    // pinset in requirements-cpu.txt was last validated against. When
    // we eventually move to a lockfile + `--no-deps` install, the
    // resolver stops mattering and this cap can go.
    //
    // Runs on BOTH the fresh-create path AND the "venv exists but
    // requirements changed" path so an already-broken venv from an
    // earlier launch self-heals on next start instead of needing the
    // user to delete %LOCALAPPDATA%\MeetingRecorder\.venv manually.
    rlog("Bootstrap: enforcing pip>=24,<25");
    {
        let (out, err) = open_log()?;
        let mut c = Command::new(&venv_py);
        c.args(["-m", "pip", "install", "--upgrade", "pip>=24,<25"])
            .stdout(Stdio::from(out)).stderr(Stdio::from(err));
        no_window(&mut c);
        if let Err(e) = c.status() {
            // Non-fatal: continue to the install step. If the install
            // then trips on the same resolver bug we already log a
            // pointed error; better than refusing to start because the
            // pip pin couldn't be enforced.
            rlog(&format!("Warning: pip pin step failed: {} — continuing", e));
        }
    }

    // Step 3: pip install -r requirements. Idempotent: pip skips packages
    // that are already at the right version, only installs deltas. On a
    // fresh venv this is the slow part (3–5 min, ~1.5 GB of wheels). On
    // an upgrade where one package was added (e.g. sentence-transformers)
    // it's tens of seconds.
    //
    // FAILURE POLICY: A pip install failure is FATAL on a fresh venv (no
    // Python modules installed yet — there's nothing to fall back to) but
    // SOFT on an upgrade where an older venv already had working
    // dependencies. The v2.10.6 install failure (huggingface_hub==0.23.0
    // forcing pip to backtrack into Python-3.13-unbuildable tokenizers
    // 0.19.1) bricked the bootstrap end-to-end even on machines whose
    // existing venv from v2.10.5 was perfectly capable of running the
    // backend. We now ask Python directly whether the critical modules
    // import; if they do, the pip install miss becomes a logged warning
    // instead of a hard refusal to start.
    rlog(&format!("Bootstrap: pip install -r {} (see bootstrap.log)", req_name));
    let (out, err) = open_log()?;
    let t0 = std::time::Instant::now();
    let mut c = Command::new(&venv_py);
    // --only-binary=:all: forbids pip from source-building any package.
    // Without it, a missing wheel silently triggers maturin / a C compiler
    // inside pip's isolated build env, which (a) takes 5-30 min and (b)
    // pops up its own console windows that no_window() can't suppress on
    // grandchildren. With the flag, pip fails fast with a readable
    // "no matching distribution found" message that we can act on.
    c.args(["-m", "pip", "install", "--only-binary=:all:", "-r"]).arg(&reqs)
        // Belt-and-suspenders for the 3.13 fallback: if we did land on
        // 3.13 and pip has to compile a pyo3 extension (tokenizers),
        // let it build against 3.13's stable ABI instead of hard-erroring.
        // Now a no-op with --only-binary, kept for defense in depth.
        .env("PYO3_USE_ABI3_FORWARD_COMPATIBILITY", "1")
        .stdout(Stdio::from(out)).stderr(Stdio::from(err));
    no_window(&mut c);
    let status = c.status().map_err(|e| format!("pip install cmd failed: {}", e))?;
    if !status.success() {
        let elapsed = t0.elapsed().as_secs_f32();
        if was_fresh_venv {
            return Err(format!(
                "pip install exited with {} after {:.0}s (see bootstrap.log)",
                status, elapsed));
        }
        // Upgrade path: the existing venv may still be runnable. Probe
        // the critical imports before declaring the backend dead.
        rlog(&format!(
            "Bootstrap: pip install exited with {} after {:.0}s on an \
             existing venv — probing whether the venv can still run the \
             backend before failing closed", status, elapsed));
        if critical_modules_import(&venv_py) {
            rlog("Bootstrap: critical modules import OK — continuing \
                  with the existing venv (pip-install failure logged but \
                  not fatal). The marker file is NOT updated, so the \
                  next launch will retry the install.");
            return Ok(venv);
        }
        return Err(format!(
            "pip install exited with {} after {:.0}s and the existing \
             venv is missing critical modules (see bootstrap.log)",
            status, elapsed));
    }
    rlog(&format!("Bootstrap: pip install completed in {:.0}s",
        t0.elapsed().as_secs_f32()));

    // Record what we installed so the next launch can tell whether
    // requirements changed again.
    if let Err(e) = std::fs::write(&reqs_marker, &current_reqs) {
        rlog(&format!("Warning: could not write {}: {}",
                      reqs_marker.display(), e));
    }

    Ok(venv)
}

/// Ask the venv's Python whether the modules the backend hard-requires
/// at startup are importable. Used by `bootstrap_app_venv` to decide
/// whether a pip-install failure on an existing venv is recoverable
/// (the venv can still serve the backend) or terminal (the backend
/// would crash anyway, so surface the pip error instead).
///
/// The list mirrors the imports that fail loudest in `backend/server.py`
/// and `backend/services/recording_service.py` — fastapi for the HTTP
/// surface, pyannote for diarization, faster_whisper for transcription,
/// sounddevice + soundfile for capture, torch for the ML stack. If any
/// of these is missing we let the original pip error stand.
fn critical_modules_import(venv_py: &std::path::Path) -> bool {
    let probe = "\
import importlib, sys\n\
mods = ['fastapi', 'pydantic', 'sounddevice', 'soundfile', \
'faster_whisper', 'pyannote.audio', 'torch', 'huggingface_hub']\n\
missing = []\n\
for m in mods:\n\
    try:\n\
        importlib.import_module(m)\n\
    except Exception as e:\n\
        missing.append(f'{m}: {e!r}')\n\
if missing:\n\
    sys.stderr.write('\\n'.join(missing) + '\\n')\n\
    sys.exit(1)\n";
    let mut c = Command::new(venv_py);
    c.args(["-c", probe]);
    // Pipe stderr into bootstrap.log so a partial import failure leaves
    // breadcrumbs for the next session to follow.
    if let Ok(f) = OpenOptions::new().create(true).append(true).open(log_dir().join("bootstrap.log")) {
        if let Ok(f2) = f.try_clone() {
            c.stdout(Stdio::from(f)).stderr(Stdio::from(f2));
        }
    }
    no_window(&mut c);
    match c.status() {
        Ok(s) => s.success(),
        Err(e) => {
            rlog(&format!("critical_modules_import: launching probe failed: {}", e));
            false
        }
    }
}

/// Get the log directory. Same as data_root_dir on every platform —
/// kept as a separate function so callers' intent is clear.
fn log_dir() -> std::path::PathBuf {
    let dir = data_root_dir();
    // Migration breadcrumb on Windows: if we find stale logs from v2.1.1
    // under %APPDATA% (Roaming), drop a README pointing to the new spot.
    #[cfg(windows)]
    {
        if let Ok(roaming) = std::env::var("APPDATA") {
            let old = std::path::PathBuf::from(roaming).join("MeetingRecorder");
            if old.exists() && old != dir {
                let readme = old.join("LOGS_MOVED.txt");
                if !readme.exists() {
                    let _ = std::fs::write(&readme,
                        format!("Logs moved to {} in v2.1.2+.\n\
                                 Open %LOCALAPPDATA%\\MeetingRecorder in File Explorer \
                                 (paste in the address bar and press Enter).\n",
                                 dir.display()));
                }
            }
        }
    }
    dir
}

fn rust_log_path() -> std::path::PathBuf {
    log_dir().join("rust.log")
}

fn backend_log_path() -> std::path::PathBuf {
    log_dir().join("backend.log")
}

/// Append to the rust log file — always available, even in release.
fn rlog(msg: &str) {
    let timestamp = chrono_like_timestamp();
    let line = format!("[{}] {}\n", timestamp, msg);
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(rust_log_path()) {
        let _ = f.write_all(line.as_bytes());
    }
    log::info!("{}", msg);
}

fn chrono_like_timestamp() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs()).unwrap_or(0);
    // Simple HH:MM:SS from seconds since epoch (local-ish, no tz)
    let s = secs % 60;
    let m = (secs / 60) % 60;
    let h = (secs / 3600) % 24;
    format!("{:02}:{:02}:{:02}", h, m, s)
}

/// Check if port is already in use (another backend running).
fn port_in_use(port: u16) -> bool {
    match TcpStream::connect_timeout(
        &format!("127.0.0.1:{}", port).parse().unwrap(),
        Duration::from_millis(500),
    ) {
        Ok(s) => {
            let _ = s.shutdown(Shutdown::Both);
            true
        }
        Err(_) => false,
    }
}

/// Can the backend actually SERVE a request right now?
///
/// `try_wait()` answers only "is the process alive", which is the wrong
/// question. A backend can be running and still be useless to the app:
/// wedged on a native model load, its listening socket broken across a
/// sleep/resume cycle, or its event loop blocked on a stalled network
/// write. In every one of those states the old watchdog saw a live PID,
/// concluded all was well, and left the user with an app that couldn't
/// record until they restarted it manually.
///
/// `/health` is auth-exempt server-side (`_AUTH_EXEMPT_PATHS` in
/// server.py), so this needs no token. Hand-rolled HTTP/1.1 over
/// `TcpStream` to avoid pulling an HTTP client into the shell for one
/// request. Generous timeouts: a busy backend is not a dead one, and a
/// false negative here costs a kill.
fn backend_healthy() -> bool {
    probe_health(backend_port())
}

/// Port-parameterised body of [`backend_healthy`], split out so the
/// probe can be exercised against a fake server in tests.
fn probe_health(port: u16) -> bool {
    let addr = match format!("127.0.0.1:{}", port).parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(1500)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_write_timeout(Some(Duration::from_millis(1500)));
    let _ = stream.set_read_timeout(Some(Duration::from_millis(3000)));
    let req = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        port
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 32];
    let healthy = match stream.read(&mut buf) {
        Ok(n) if n > 0 => {
            let head = &buf[..n];
            head.starts_with(b"HTTP/1.1 200") || head.starts_with(b"HTTP/1.0 200")
        }
        _ => false,
    };
    let _ = stream.shutdown(Shutdown::Both);
    healthy
}

/// What the watchdog should do on a given tick.
#[derive(Debug, PartialEq, Eq)]
enum WatchdogAction {
    /// Backend is serving requests — nothing to do.
    Idle,
    /// Alive but not answering yet. Keep counting; a backend mid-model-load
    /// or mid-transcription is briefly unresponsive and must not be killed.
    WaitUnhealthy,
    /// Alive but unreachable past the limit — wedged. Kill it so it can
    /// be replaced.
    KillThenRespawn,
    /// Down, but the port hasn't been released yet. A wait state, NOT a
    /// failure — see the regression note in the tests.
    WaitForPort,
    /// Down and the port is free — start a fresh one.
    Respawn,
}

/// Pure watchdog policy, extracted so the decision that used to strand
/// users without a backend is unit-testable.
///
/// `unhealthy_streak` counts consecutive failed health probes INCLUDING
/// the current tick.
fn next_watchdog_action(
    child_alive: bool,
    healthy: bool,
    unhealthy_streak: u32,
    unhealthy_limit: u32,
    port_held: bool,
) -> WatchdogAction {
    if child_alive {
        if healthy {
            return WatchdogAction::Idle;
        }
        if unhealthy_streak < unhealthy_limit {
            return WatchdogAction::WaitUnhealthy;
        }
        return WatchdogAction::KillThenRespawn;
    }
    if port_held {
        return WatchdogAction::WaitForPort;
    }
    WatchdogAction::Respawn
}

/// Kill the current sidecar so the watchdog's respawn path can replace
/// it. Used when the process is alive but has stopped answering.
fn kill_backend_process(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<BackendProcess>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(mut child) = guard.take() {
                match child.kill() {
                    Ok(_) => {
                        // Reap it so the OS releases the PID and the
                        // listening socket, instead of leaving a zombie
                        // that keeps the port busy.
                        let _ = child.wait();
                        rlog("Unresponsive backend killed");
                    }
                    Err(e) => rlog(&format!("Failed to kill unresponsive backend: {}", e)),
                }
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend = BackendProcess(Mutex::new(None));

    // Rotate rust.log on each launch
    let _ = std::fs::write(
        rust_log_path(),
        format!("=== Meeting Recorder launch ===\n"),
    );
    rlog(&format!("Log dir: {}", log_dir().display()));
    rlog(&format!("Backend port: {}", backend_port()));

    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        // HTTP plugin for the GitHub Releases updater. WebView fetch()
        // from origin http://tauri.localhost gets CORS-blocked even
        // when the upstream sends Access-Control-Allow-Origin: * (the
        // WebView's preflight has different semantics from curl). The
        // plugin proxies the request through Rust where CORS doesn't
        // apply. Scope in capabilities/default.json restricts which
        // hosts the WebView can ask the plugin to reach.
        .plugin(tauri_plugin_http::init())
        .manage(backend)
        .invoke_handler(tauri::generate_handler![
            restart_backend,
            get_backend_port,
            get_backend_token,
            capture_screenshot,
            download_and_run_update,
            open_external,
            open_system_settings
        ])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            // Spawn backend in a background thread. If this is a fresh
            // install with no Python venv, bootstrap_app_venv may block
            // for 3-5 minutes while pip downloads wheels; we don't want
            // to block setup (the window wouldn't even appear until it
            // finished). BOOTSTRAPPING covers the whole initial spawn
            // so the watchdog doesn't try to respawn while extraction /
            // venv creation / pip install is running.
            let spawn_handle = app.handle().clone();
            std::thread::spawn(move || {
                BOOTSTRAPPING.store(true, Ordering::Relaxed);
                let result = spawn_python_backend(&spawn_handle);
                BOOTSTRAPPING.store(false, Ordering::Relaxed);
                match result {
                    Ok(_) => rlog("Python backend sidecar spawn requested"),
                    Err(e) => rlog(&format!("ERROR: Backend startup failed: {}", e)),
                }
            });
            // macOS calendar bridge. Reads EventKit from inside the
            // .app bundle (Python can't because its venv lives outside
            // and TCC keys by code-signing identity, not parent
            // process), writes events to a JSON sidecar Python reads.
            // No-op on Windows — Python's _calendar_outlook.py path
            // continues to handle calendar via Outlook COM there.
            #[cfg(target_os = "macos")]
            calendar_macos::spawn_polling_thread();

            // Watchdog: if the Python process dies unexpectedly (killed
            // by corporate AV, OOM, unhandled exception), respawn it
            // after a short delay.
            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                // Consecutive health-probe failures against a process
                // that is still ALIVE. Six polls ≈ 30s of sustained
                // unreachability before we conclude it's wedged and
                // kill it. Deliberately patient: a backend mid-model-load
                // or mid-transcription can be briefly unresponsive, and
                // killing one of those costs the user real work.
                const UNHEALTHY_LIMIT: u32 = 6;
                let mut unhealthy_streak: u32 = 0;
                // Backoff applies ONLY to genuinely failed spawn attempts.
                let mut respawn_backoff_secs: u64 = 5;

                loop {
                    std::thread::sleep(std::time::Duration::from_secs(5));
                    if BOOTSTRAPPING.load(Ordering::Relaxed) {
                        unhealthy_streak = 0;
                        respawn_backoff_secs = 5;
                        continue;
                    }

                    let child_alive = if let Some(state) = app_handle.try_state::<BackendProcess>() {
                        if let Ok(mut guard) = state.0.lock() {
                            match guard.as_mut() {
                                Some(c) => match c.try_wait() {
                                    Ok(Some(status)) => {
                                        rlog(&format!(
                                            "Backend exited unexpectedly: {:?}", status));
                                        *guard = None;
                                        false
                                    }
                                    Ok(None) => true,
                                    Err(e) => {
                                        rlog(&format!("try_wait error: {}", e));
                                        false
                                    }
                                },
                                None => false,
                            }
                        } else { true }
                    } else { true };

                    // Alive is necessary but not sufficient — ask whether
                    // it can actually answer. Probe only when alive; a
                    // dead backend needs no probe.
                    let healthy = child_alive && backend_healthy();
                    if child_alive && !healthy {
                        unhealthy_streak += 1;
                    }

                    match next_watchdog_action(
                        child_alive,
                        healthy,
                        unhealthy_streak,
                        UNHEALTHY_LIMIT,
                        port_in_use(backend_port()),
                    ) {
                        WatchdogAction::Idle => {
                            unhealthy_streak = 0;
                            respawn_backoff_secs = 5;
                            continue;
                        }
                        WatchdogAction::WaitUnhealthy => continue,
                        WatchdogAction::WaitForPort => {
                            // The port can stay bound after the process
                            // dies (Windows TIME_WAIT, a lingering
                            // grandchild). This is a WAIT state, not a
                            // failure. The old code counted it against a
                            // 5-strike budget and then permanently
                            // `break`-ed out of this loop — so a crash
                            // whose port took ~25s to clear left the app
                            // with no backend and no recovery until the
                            // user restarted it. That was the "reconnect
                            // before you can record" bug.
                            rlog("Backend down, port still held — waiting for it to clear");
                            continue;
                        }
                        WatchdogAction::KillThenRespawn => {
                            rlog(&format!(
                                "Backend alive but unreachable for ~{}s — killing it \
                                 so it can be replaced",
                                unhealthy_streak * 5));
                            kill_backend_process(&app_handle);
                            unhealthy_streak = 0;
                            // Port likely still held for a moment; next
                            // tick handles the respawn.
                            continue;
                        }
                        WatchdogAction::Respawn => {}
                    }
                    unhealthy_streak = 0;

                    rlog("Respawning backend");
                    match spawn_python_backend(&app_handle) {
                        Ok(_) => {
                            respawn_backoff_secs = 5;
                        }
                        Err(e) => {
                            // Never give up permanently — back off and
                            // keep trying. A transient failure (AV lock,
                            // disk pressure) must not brick recovery for
                            // the rest of the app's lifetime.
                            rlog(&format!(
                                "Respawn failed: {} — retrying in {}s",
                                e, respawn_backoff_secs));
                            std::thread::sleep(
                                std::time::Duration::from_secs(respawn_backoff_secs));
                            respawn_backoff_secs = (respawn_backoff_secs * 2).min(60);
                        }
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                rlog("Window close requested — killing backend");
                if let Some(state) = window.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            match child.kill() {
                                Ok(_) => rlog("Backend killed cleanly"),
                                Err(e) => rlog(&format!("Failed to kill backend: {}", e)),
                            }
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// Tauri command: kill the current Python sidecar and spawn a fresh one.
/// Used by the GPU toggle UI to activate a newly-installed torch flavour
/// without forcing the user to close and reopen the whole app.
#[tauri::command]
fn restart_backend(
    state: tauri::State<'_, BackendProcess>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    rlog("restart_backend command invoked");
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut child) = guard.take() {
            match child.kill() {
                Ok(_) => rlog("Old backend killed"),
                Err(e) => rlog(&format!("Failed to kill old backend: {}", e)),
            }
            let _ = child.wait();
        }
    }
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while port_in_use(backend_port()) && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(200));
    }
    spawn_python_backend(&app).map_err(|e| e.to_string())?;
    Ok(())
}

/// Tauri command: download a release installer to the temp dir and
/// launch it, so an in-app update doesn't dump the user in a browser.
/// Windows only — the NSIS `.exe` runs itself (UAC prompts as usual);
/// on macOS the unsigned `.zip` can't auto-install (Gatekeeper), so the
/// frontend keeps the browser path there. We shell out via PowerShell
/// (already the pattern in this file) to avoid pulling an HTTP crate.
/// Best-effort: any failure returns Err and the caller falls back to
/// opening the asset URL in the browser, so this can't make updating
/// worse than before.
#[tauri::command]
fn download_and_run_update(url: String) -> Result<(), String> {
    #[cfg(windows)]
    {
        let dest = std::env::temp_dir().join("MeetingRecorder-Update-Setup.exe");
        let dest_s = dest.to_string_lossy().replace('\'', "''");
        let url_s = url.replace('\'', "''");
        let ps = format!(
            "$ErrorActionPreference='Stop'; \
             $ProgressPreference='SilentlyContinue'; \
             Invoke-WebRequest -Uri '{url}' -OutFile '{dest}'; \
             Start-Process -FilePath '{dest}'",
            url = url_s,
            dest = dest_s,
        );
        let mut cmd = Command::new("powershell");
        cmd.args(["-NoProfile", "-Command", ps.as_str()]);
        no_window(&mut cmd);
        let status = cmd.status().map_err(|e| e.to_string())?;
        if !status.success() {
            return Err(format!("update download/launch failed ({status})"));
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = url;
        Err("auto-run installer is only supported on Windows".into())
    }
}

/// Tauri command: capture a screenshot of the user's screen into `dir`
/// and return the saved file's absolute path.
///
/// Capture lives in the Rust shell on purpose. On macOS, Screen
/// Recording permission (TCC) is attributed to the signed app bundle —
/// the same reason the calendar bridge moved here. The Python sidecar
/// runs from a venv outside the bundle and its capture would be denied.
/// We shell out to the OS screenshot tool rather than pull in a heavy
/// capture crate: keeps the CI bundle small and avoids the macos-14
/// runner brittleness called out in AGENTS.md.
/// `x`/`y`/`width`/`height` are the chosen monitor's bounds in PHYSICAL
/// pixels (from the frontend's Tauri monitor list); `scale` is that
/// monitor's scale factor. When `width`/`height` are 0 we fall back to
/// a full primary-screen grab — covers the single-monitor / no-info
/// path so the button never silently no-ops.
#[tauri::command]
fn capture_screenshot(
    dir: String,
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    scale: f64,
) -> Result<String, String> {
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    let target_dir = PathBuf::from(&dir);
    std::fs::create_dir_all(&target_dir)
        .map_err(|e| format!("Could not create screenshot dir: {}", e))?;
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let out_path = target_dir.join(format!("screenshot_{}.png", millis));
    let out = out_path.to_string_lossy().to_string();

    let region = width > 0 && height > 0;

    #[cfg(target_os = "macos")]
    let result = {
        // screencapture -R takes points; Tauri bounds are physical
        // pixels, so divide by the monitor's scale factor. Only the
        // macOS path needs this, so it's scoped here to avoid an
        // unused-variable warning on Windows/Linux builds.
        let sc = if scale > 0.0 { scale } else { 1.0 };
        if region {
            let rx = (x as f64 / sc).round() as i64;
            let ry = (y as f64 / sc).round() as i64;
            let rw = (width as f64 / sc).round() as i64;
            let rh = (height as f64 / sc).round() as i64;
            let r = format!("-R{},{},{},{}", rx, ry, rw, rh);
            Command::new("screencapture")
                .args(["-x", "-t", "png", r.as_str(), out.as_str()])
                .status()
        } else {
            Command::new("screencapture")
                .args(["-x", "-t", "png", out.as_str()])
                .status()
        }
    };

    #[cfg(target_os = "windows")]
    let result = {
        // SetProcessDPIAware so CopyFromScreen coordinates are physical
        // pixels and line up with the Tauri monitor bounds. Falls back
        // to the primary screen when no region was supplied.
        let grab = if region {
            format!(
                "$bmp = New-Object System.Drawing.Bitmap {w}, {h}; \
                 $g = [System.Drawing.Graphics]::FromImage($bmp); \
                 $g.CopyFromScreen({x}, {y}, 0, 0, $bmp.Size);",
                w = width, h = height, x = x, y = y
            )
        } else {
            "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; \
             $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; \
             $g = [System.Drawing.Graphics]::FromImage($bmp); \
             $g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size);"
                .to_string()
        };
        let ps = format!(
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; \
             Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] \
             public static extern bool SetProcessDPIAware();' \
             -Name U -Namespace W; [W.U]::SetProcessDPIAware() | Out-Null; \
             {grab} \
             $bmp.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png); \
             $g.Dispose(); $bmp.Dispose()",
            grab = grab,
            out = out.replace('\'', "''")
        );
        let mut cmd = Command::new("powershell");
        cmd.args(["-NoProfile", "-STA", "-Command", ps.as_str()]);
        // Without CREATE_NO_WINDOW the powershell console flashes up and
        // gets captured INTO the screenshot (and pops over a live
        // meeting). Every other Command in this file already hides it;
        // this one was missed.
        no_window(&mut cmd);
        cmd.status()
    };

    #[cfg(all(unix, not(target_os = "macos")))]
    let result = {
        // Try the common Wayland/X11 tools in turn; first one that's
        // installed and succeeds wins. With a region we pass the
        // geometry; without one we grab everything.
        let mut last: std::io::Result<std::process::ExitStatus> =
            Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "no screenshot tool",
            ));
        let geom_grim = format!("{},{} {}x{}", x, y, width, height);
        let geom_im = format!("{}x{}+{}+{}", width, height, x, y);
        let geom_scrot = format!("{},{},{},{}", x, y, width, height);
        let attempts: Vec<(&str, Vec<&str>)> = if region {
            vec![
                ("grim", vec!["-g", geom_grim.as_str(), out.as_str()]),
                ("import", vec!["-window", "root", "-crop",
                                geom_im.as_str(), out.as_str()]),
                ("scrot", vec!["-a", geom_scrot.as_str(), out.as_str()]),
            ]
        } else {
            vec![
                ("grim", vec![out.as_str()]),
                ("gnome-screenshot", vec!["-f", out.as_str()]),
                ("scrot", vec![out.as_str()]),
                ("import", vec!["-window", "root", out.as_str()]),
            ]
        };
        for (bin, args) in attempts.iter() {
            match Command::new(bin).args(args).status() {
                Ok(s) if s.success() => {
                    last = Ok(s);
                    break;
                }
                other => last = other,
            }
        }
        last
    };

    match result {
        Ok(status) if status.success() => {
            if std::path::Path::new(&out).exists() {
                rlog(&format!("Screenshot captured: {}", out));
                Ok(out)
            } else {
                Err("Screenshot tool reported success but no file was \
                     written. On macOS, grant Screen Recording permission \
                     in System Settings → Privacy & Security."
                    .to_string())
            }
        }
        Ok(status) => Err(format!(
            "Screenshot tool exited with status {}. On macOS, grant \
             Screen Recording permission in System Settings → Privacy \
             & Security, then try again.",
            status
        )),
        Err(e) => Err(format!("Could not run screenshot tool: {}", e)),
    }
}

/// Tauri command: open an http(s) URL in the user's default browser.
///
/// In a Tauri webview a plain `<a target="_blank">` does NOT reach the
/// system browser, so the "Join meeting" link (and every other external
/// link in the app) silently did nothing in the packaged build. We
/// shell out to the OS handler — same no-extra-crates approach as the
/// screenshot command. The URL is passed as a single argument (no shell
/// re-parsing) and scheme-validated so this can't be turned into an
/// arbitrary-command/launch primitive from the web layer.
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    let u = url.trim();
    let low = u.to_ascii_lowercase();
    if !(low.starts_with("https://") || low.starts_with("http://")) {
        return Err("Only http/https URLs can be opened.".to_string());
    }
    // Defense-in-depth: reject control chars / whitespace that could
    // confuse a downstream handler.
    if u.chars().any(|c| c.is_control() || c == '"') {
        return Err("URL contains invalid characters.".to_string());
    }

    #[cfg(target_os = "windows")]
    // rundll32 FileProtocolHandler takes the URL as ONE opaque arg and
    // launches the default browser — unlike `cmd /c start`, it doesn't
    // re-parse `&` in the query string (Teams/Zoom links are full of
    // them).
    let res = Command::new("rundll32.exe")
        .args(["url.dll,FileProtocolHandler", u])
        .spawn();

    #[cfg(target_os = "macos")]
    let res = Command::new("open").arg(u).spawn();

    #[cfg(all(unix, not(target_os = "macos")))]
    let res = Command::new("xdg-open").arg(u).spawn();

    match res {
        Ok(_) => {
            rlog(&format!("Opened external URL: {}", u));
            Ok(())
        }
        Err(e) => Err(format!("Could not open browser: {}", e)),
    }
}

/// Open a specific OS system-settings panel. Allowlist-only — the JS
/// side passes a short tag, never an arbitrary path, so this can't be
/// abused to launch shellexec on anything the WebView feeds us.
///
/// Currently the only supported panel is "sound" (the audio devices
/// dialog), used by the Record view to deep-link users into fixing a
/// mic↔system-audio default-format mismatch. Add more panel keys here
/// as new flows need them; never accept a free-form path.
#[tauri::command]
fn open_system_settings(panel: String) -> Result<(), String> {
    let key = panel.trim().to_ascii_lowercase();
    match key.as_str() {
        "sound" => open_sound_panel(),
        other => Err(format!("Unknown settings panel: {}", other)),
    }
}

#[cfg(target_os = "windows")]
fn open_sound_panel() -> Result<(), String> {
    // mmsys.cpl is the classic Sound applet (Recording / Playback /
    // Sounds / Communications tabs) — exactly where the user needs to
    // go to match the mic and speakers default formats. control.exe
    // launches it via the standard Control Panel surface.
    Command::new("control.exe")
        .arg("mmsys.cpl")
        .spawn()
        .map(|_| { rlog("Opened Sound Control Panel (mmsys.cpl)"); })
        .map_err(|e| format!("Could not open Sound Control Panel: {}", e))
}

#[cfg(target_os = "macos")]
fn open_sound_panel() -> Result<(), String> {
    // System Settings.app on macOS 13+ ; System Preferences.app on
    // earlier versions. `open -b` resolves either by bundle id, then
    // the `x-apple.systempreferences:` URL deep-links into the Sound
    // pane. Falls back to opening the pref pane file directly when the
    // URL scheme isn't honoured (very old macOS).
    let res = Command::new("open")
        .args(["-b", "com.apple.systempreferences",
               "x-apple.systempreferences:com.apple.preference.sound"])
        .spawn();
    if res.is_ok() {
        rlog("Opened macOS Sound preferences");
        return Ok(());
    }
    Command::new("open")
        .arg("/System/Library/PreferencePanes/Sound.prefPane")
        .spawn()
        .map(|_| { rlog("Opened Sound.prefPane fallback"); })
        .map_err(|e| format!("Could not open Sound preferences: {}", e))
}

#[cfg(all(unix, not(target_os = "macos")))]
fn open_sound_panel() -> Result<(), String> {
    // Best effort on Linux — desktop environments differ wildly. Try
    // pavucontrol (PulseAudio mixer) first since it's the most common
    // sound-format adjustment UI; fall back to gnome-control-center.
    for (cmd, args) in [
        ("pavucontrol", vec![]),
        ("gnome-control-center", vec!["sound"]),
    ] {
        if Command::new(cmd).args(&args).spawn().is_ok() {
            rlog(&format!("Opened {} for sound settings", cmd));
            return Ok(());
        }
    }
    Err("No supported sound-settings launcher found on this system".to_string())
}

fn spawn_python_backend(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let port = backend_port();
    if port_in_use(port) {
        // With dynamic ports this only fires if the OS handed us a port
        // that another process grabbed in the TOCTOU window between
        // pick_free_port's drop and the Python child's bind. Extremely
        // rare; surface it instead of silently aborting like the old
        // hardcoded-port code did.
        rlog(&format!(
            "ERROR: backend port {} is in use immediately after selection — \
             aborting spawn. Restart the app to pick a fresh port.",
            port
        ));
        return Err(format!("Port {} unavailable at spawn time", port).into());
    }

    let backend_dir = resolve_backend_dir().ok_or(
        "Could not find bundled backend/ directory. The installer may be \
         corrupted; reinstall from Releases.")?;
    let server_py = backend_dir.join("server.py");
    // Run the venv bootstrap on every launch when an app-managed venv
    // already exists — the function is idempotent (skips pip install
    // when `requirements.installed.txt` matches the bundled
    // requirements file) but DOES re-install when requirements have
    // changed between releases. Previously this was gated behind
    // `resolve_python() == None`, which meant bootstrap only ran on
    // machines with NO Python at all — every user with a venv from a
    // prior install was frozen at that install's package set forever
    // (openpyxl added in v2.7.0 never landed for anyone who installed
    // v2.6.x or earlier; the "marker missing → re-install" code path
    // inside bootstrap_app_venv was effectively dead). For dev-mode
    // (dev checkout venv) and the legacy-v1 venv path we skip the app
    // bootstrap so we don't create an unwanted second venv.
    let app_venv_exists = venv_python_candidates(&app_venv_dir())
        .into_iter().any(|p| p.exists());
    let python_exe = if app_venv_exists {
        if let Err(e) = bootstrap_app_venv(&backend_dir) {
            rlog(&format!("ERROR: bootstrap (upgrade-check) failed: {}", e));
            return Err(e.into());
        }
        resolve_python(&backend_dir).ok_or(
            "Bootstrap reported success but Python still not found")?
    } else {
        match resolve_python(&backend_dir) {
            Some(p) => p,
            None => {
                rlog("No Python found — starting venv bootstrap");
                bootstrap_app_venv(&backend_dir).map_err(|e| {
                    rlog(&format!("ERROR: bootstrap failed: {}", e));
                    e
                })?;
                resolve_python(&backend_dir).ok_or(
                    "Bootstrap reported success but Python still not found")?
            }
        }
    };

    rlog(&format!("Backend dir: {}", backend_dir.display()));
    rlog(&format!("Spawning Python: {}", python_exe.display()));
    rlog(&format!("  server.py: {}", server_py.display()));
    rlog(&format!("  backend.log: {}", backend_log_path().display()));

    // CRITICAL: kill any orphan Python backends from previous app
    // launches before we spawn a new one. Without this, force-quits /
    // crash-restarts / double-launches accumulate orphan processes
    // that keep recording audio in the background. One user
    // accumulated three orphans over 30 hours, one of which captured
    // 4h17m of audio silently.
    kill_orphan_backends(&python_exe);

    let log_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(backend_log_path())
        .map_err(|e| format!("Couldn't open backend log file: {}", e))?;
    {
        let mut sep = log_file.try_clone()
            .map_err(|e| format!("Couldn't clone log fd for separator: {}", e))?;
        let _ = sep.write_all(
            format!("\n=== backend spawn @ {} ===\n",
                chrono_like_timestamp()).as_bytes());
    }
    let log_file2 = log_file.try_clone()
        .map_err(|e| format!("Couldn't clone log fd: {}", e))?;

    let mut cmd = Command::new(&python_exe);
    cmd.arg("-u")
       .arg(&server_py)
       .env("PYTHONUNBUFFERED", "1")
       // Hand the chosen port to server.py via env. server.py reads
       // MEETING_RECORDER_PORT and falls back to 17645 only if it's
       // unset (e.g. running standalone for debugging).
       .env("MEETING_RECORDER_PORT", port.to_string())
       // Per-launch shared secret — the sidecar refuses any request
       // that doesn't present it. See BACKEND_TOKEN docs above.
       .env("MEETING_RECORDER_TOKEN", backend_token())
       // Pass our (Tauri shell) PID so the Python backend can watch
       // for our death. If the shell exits without cleanly killing
       // the backend (force-quit, crash, BSOD), the backend's
       // parent-PID watchdog detects this within seconds and shuts
       // itself down — preventing the orphan-recording scenario.
       .env("MEETING_RECORDER_PARENT_PID", std::process::id().to_string())
       // Intel Fortran runtime workarounds — Windows-only but harmless on
       // POSIX (FOR_DISABLE_* are simply ignored when MKL isn't present).
       .env("FOR_DISABLE_CONSOLE_CTRL_HANDLER", "1")
       .env("FOR_DISABLE_STACK_TRACE", "1")
       .stdout(Stdio::from(log_file))
       .stderr(Stdio::from(log_file2));

    no_window(&mut cmd);

    let child = cmd.spawn()
        .map_err(|e| {
            rlog(&format!("ERROR: failed to start Python: {}", e));
            format!("Failed to start Python: {}", e)
        })?;

    rlog(&format!("Python process started, PID ~{}", child.id()));

    if let Some(state) = app.try_state::<BackendProcess>() {
        *state.0.lock().unwrap() = Some(child);
    }
    Ok(())
}

#[cfg(test)]
mod watchdog_tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    // ── Policy: the decision that used to strand users ──────────────

    #[test]
    fn serving_backend_is_left_alone() {
        assert_eq!(
            next_watchdog_action(true, true, 0, 6, true),
            WatchdogAction::Idle
        );
    }

    #[test]
    fn briefly_unresponsive_backend_is_not_killed() {
        // Mid-model-load / mid-transcription: alive, not answering, but
        // well under the limit. Killing here would cost the user work.
        for streak in 1..6 {
            assert_eq!(
                next_watchdog_action(true, false, streak, 6, true),
                WatchdogAction::WaitUnhealthy,
                "streak {streak} should still be tolerated"
            );
        }
    }

    #[test]
    fn wedged_backend_is_killed_once_past_the_limit() {
        // The case try_wait() can never see: process alive, socket dead
        // (sleep/resume), unreachable for ~30s.
        assert_eq!(
            next_watchdog_action(true, false, 6, 6, true),
            WatchdogAction::KillThenRespawn
        );
    }

    #[test]
    fn dead_backend_with_held_port_waits_instead_of_giving_up() {
        // THE REGRESSION. A crashed backend leaves the port in TIME_WAIT
        // (up to ~2 min on Windows). The old watchdog counted each of
        // these ticks as a failed restart and permanently exited after
        // five — leaving the app with no backend for the rest of its
        // life, which is why recording required restarting the app.
        // This must be a wait, forever if necessary, never a give-up.
        for streak in [0, 5, 50, 5_000] {
            assert_eq!(
                next_watchdog_action(false, false, streak, 6, true),
                WatchdogAction::WaitForPort,
                "a held port must never be treated as a failed restart"
            );
        }
    }

    #[test]
    fn dead_backend_with_free_port_respawns() {
        assert_eq!(
            next_watchdog_action(false, false, 0, 6, false),
            WatchdogAction::Respawn
        );
    }

    // ── Probe: "is it actually serving?" ────────────────────────────

    fn serve_once(response: &'static [u8]) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            if let Ok((mut s, _)) = listener.accept() {
                let mut buf = [0u8; 512];
                let _ = s.read(&mut buf);
                let _ = s.write_all(response);
            }
        });
        port
    }

    #[test]
    fn probe_accepts_a_200() {
        let port = serve_once(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}");
        assert!(probe_health(port));
    }

    #[test]
    fn probe_rejects_a_500() {
        let port = serve_once(b"HTTP/1.1 500 Internal Server Error\r\n\r\n");
        assert!(!probe_health(port));
    }

    #[test]
    fn probe_rejects_nothing_listening() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener); // free it, so the connect refuses
        assert!(!probe_health(port));
    }

    #[test]
    fn probe_rejects_a_socket_that_accepts_but_never_answers() {
        // The wedged backend: TCP accept succeeds (so port_in_use and a
        // liveness check both look fine) but no HTTP response ever comes.
        // The read timeout must make this a failure, not a hang.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            let _held = listener.accept();
            thread::sleep(Duration::from_secs(10));
        });
        assert!(!probe_health(port));
    }
}
