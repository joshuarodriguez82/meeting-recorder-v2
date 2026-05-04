use std::fs::{File, OpenOptions};
use std::io::Write;
use std::net::{Shutdown, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;

struct BackendProcess(Mutex<Option<Child>>);

const BACKEND_PORT: u16 = 17645;

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
//   - find_system_python_313(): how we locate a system Python to bootstrap
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

/// Try to find a system-installed Python 3.13 that we can use to create
/// an app venv from. Checks multiple discovery paths per platform.
#[cfg(windows)]
fn find_system_python_313() -> Option<std::path::PathBuf> {
    // 1. `py -3.13 -c "print(sys.executable)"`
    let mut cmd = Command::new("py");
    cmd.args(["-3.13", "-c", "import sys; print(sys.executable)"])
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

    // 2. `python` on PATH — verify it's 3.13
    let mut cmd = Command::new("python");
    cmd.args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print(sys.executable)"])
        .stdout(Stdio::piped()).stderr(Stdio::null());
    no_window(&mut cmd);
    if let Ok(out) = cmd.output() {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout).to_string();
            let mut lines = text.lines();
            if let (Some(ver), Some(exe)) = (lines.next(), lines.next()) {
                if ver.trim() == "3.13" {
                    let p = std::path::PathBuf::from(exe.trim());
                    if p.exists() { return Some(p); }
                }
            }
        }
    }

    // 3. Common install paths.
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(localappdata) = std::env::var("LOCALAPPDATA") {
        candidates.push(std::path::PathBuf::from(&localappdata)
            .join("Programs").join("Python").join("Python313").join("python.exe"));
    }
    candidates.push(std::path::PathBuf::from(r"C:\Program Files\Python313\python.exe"));
    candidates.push(std::path::PathBuf::from(r"C:\Program Files (x86)\Python313\python.exe"));
    for c in candidates {
        if c.exists() { return Some(c); }
    }
    None
}

#[cfg(target_os = "macos")]
fn find_system_python_313() -> Option<std::path::PathBuf> {
    // 1. `python3.13` on PATH (Homebrew exposes this — `brew install python@3.13`).
    let mut cmd = Command::new("python3.13");
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

    // 2. `python3` on PATH if it is exactly 3.13.
    let mut cmd = Command::new("python3");
    cmd.args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print(sys.executable)"])
        .stdout(Stdio::piped()).stderr(Stdio::null());
    if let Ok(out) = cmd.output() {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout).to_string();
            let mut lines = text.lines();
            if let (Some(ver), Some(exe)) = (lines.next(), lines.next()) {
                if ver.trim() == "3.13" {
                    let p = std::path::PathBuf::from(exe.trim());
                    if p.exists() { return Some(p); }
                }
            }
        }
    }

    // 3. Standard install locations: Homebrew on Apple Silicon, Homebrew on
    //    Intel, python.org installer, pyenv shim.
    let candidates: Vec<std::path::PathBuf> = vec![
        std::path::PathBuf::from("/opt/homebrew/bin/python3.13"),         // brew, Apple Silicon
        std::path::PathBuf::from("/usr/local/bin/python3.13"),             // brew, Intel
        std::path::PathBuf::from("/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"),  // python.org
        std::path::PathBuf::from(format!("{}/.pyenv/versions/3.13.0/bin/python3.13",
            std::env::var("HOME").unwrap_or_default())),
    ];
    for c in candidates {
        if c.exists() { return Some(c); }
    }
    None
}

#[cfg(all(unix, not(target_os = "macos")))]
fn find_system_python_313() -> Option<std::path::PathBuf> {
    for name in ["python3.13", "python3"] {
        let mut cmd = Command::new(name);
        cmd.args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print(sys.executable)"])
            .stdout(Stdio::piped()).stderr(Stdio::null());
        if let Ok(out) = cmd.output() {
            if out.status.success() {
                let text = String::from_utf8_lossy(&out.stdout).to_string();
                let mut lines = text.lines();
                if let (Some(ver), Some(exe)) = (lines.next(), lines.next()) {
                    if ver.trim() == "3.13" {
                        let p = std::path::PathBuf::from(exe.trim());
                        if p.exists() { return Some(p); }
                    }
                }
            }
        }
    }
    for c in ["/usr/bin/python3.13", "/usr/local/bin/python3.13"] {
        let p = std::path::PathBuf::from(c);
        if p.exists() { return Some(p); }
    }
    None
}

/// Human-readable instruction for installing Python 3.13. Surfaced in the
/// error message when we can't find one — different per platform because
/// the install method differs (py.org installer vs Homebrew vs apt).
fn python_install_instructions() -> &'static str {
    #[cfg(windows)]
    { "Install Python 3.13 from https://www.python.org/downloads/ \
       (per-user install, no admin needed; check 'Add python.exe to PATH'), \
       then restart Meeting Recorder." }
    #[cfg(target_os = "macos")]
    { "Install Python 3.13 with Homebrew: `brew install python@3.13`. \
       (If you don't have Homebrew, install it from https://brew.sh first.) \
       Then restart Meeting Recorder." }
    #[cfg(all(unix, not(target_os = "macos")))]
    { "Install Python 3.13 from your distro's package manager (e.g. `apt install python3.13`) \
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

    let venv_py = match venv_py_existing {
        Some(p) => p,
        None => {
            // First-time install: create the venv from scratch with system Python.
            let system_py = find_system_python_313().ok_or_else(|| {
                format!("Python 3.13 not found on this machine. {}",
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

            // Step 2: upgrade pip — only on first create. On the upgrade
            // path we trust whatever pip is already in the venv.
            rlog("Bootstrap: upgrading pip");
            let (out, err) = open_log()?;
            let mut c = Command::new(&p);
            c.args(["-m", "pip", "install", "--upgrade", "pip"])
                .stdout(Stdio::from(out)).stderr(Stdio::from(err));
            no_window(&mut c);
            let _ = c.status();
            p
        }
    };

    // Step 3: pip install -r requirements. Idempotent: pip skips packages
    // that are already at the right version, only installs deltas. On a
    // fresh venv this is the slow part (3–5 min, ~1.5 GB of wheels). On
    // an upgrade where one package was added (e.g. sentence-transformers)
    // it's tens of seconds.
    rlog(&format!("Bootstrap: pip install -r {} (see bootstrap.log)", req_name));
    let (out, err) = open_log()?;
    let t0 = std::time::Instant::now();
    let mut c = Command::new(&venv_py);
    c.args(["-m", "pip", "install", "-r"]).arg(&reqs)
        .stdout(Stdio::from(out)).stderr(Stdio::from(err));
    no_window(&mut c);
    let status = c.status().map_err(|e| format!("pip install cmd failed: {}", e))?;
    if !status.success() {
        return Err(format!(
            "pip install exited with {} after {:.0}s (see bootstrap.log)",
            status, t0.elapsed().as_secs_f32()));
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend = BackendProcess(Mutex::new(None));

    // Rotate rust.log on each launch
    let _ = std::fs::write(
        rust_log_path(),
        format!("=== Meeting Recorder launch ===\n"),
    );
    rlog(&format!("Log dir: {}", log_dir().display()));
    rlog(&format!("Backend port: {}", BACKEND_PORT));

    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(backend)
        .invoke_handler(tauri::generate_handler![restart_backend])
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
            // Watchdog: if the Python process dies unexpectedly (killed
            // by corporate AV, OOM, unhandled exception), respawn it
            // after a short delay.
            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                let mut consecutive_restarts = 0;
                loop {
                    std::thread::sleep(std::time::Duration::from_secs(5));
                    if BOOTSTRAPPING.load(Ordering::Relaxed) {
                        consecutive_restarts = 0;
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
                    if child_alive {
                        consecutive_restarts = 0;
                        continue;
                    }
                    consecutive_restarts += 1;
                    if consecutive_restarts > 5 {
                        rlog("Backend crashed 5+ times in a row — giving up. \
                              Check backend.log for the cause, reinstall if needed.");
                        break;
                    }
                    if port_in_use(BACKEND_PORT) {
                        continue;
                    }
                    rlog(&format!("Respawning backend (attempt {})", consecutive_restarts));
                    if let Err(e) = spawn_python_backend(&app_handle) {
                        rlog(&format!("Respawn failed: {}", e));
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
    while port_in_use(BACKEND_PORT) && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(200));
    }
    spawn_python_backend(&app).map_err(|e| e.to_string())?;
    Ok(())
}

fn spawn_python_backend(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    if port_in_use(BACKEND_PORT) {
        rlog(&format!(
            "Port {} already in use — skipping spawn (another Meeting Recorder \
             instance is probably running, or a zombie backend is holding the port)",
            BACKEND_PORT
        ));
        return Ok(());
    }

    let backend_dir = resolve_backend_dir().ok_or(
        "Could not find bundled backend/ directory. The installer may be \
         corrupted; reinstall from Releases.")?;
    let server_py = backend_dir.join("server.py");
    let python_exe = match resolve_python(&backend_dir) {
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
    };

    rlog(&format!("Backend dir: {}", backend_dir.display()));
    rlog(&format!("Spawning Python: {}", python_exe.display()));
    rlog(&format!("  server.py: {}", server_py.display()));
    rlog(&format!("  backend.log: {}", backend_log_path().display()));

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
