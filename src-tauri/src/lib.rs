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

/// Where the backend zip is installed on disk by Tauri.
fn resolve_bundle_zip() -> Option<std::path::PathBuf> {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            // Tauri installs resources alongside the exe by default; check
            // a few conventional subdirs just to be safe across bundler
            // versions (wix / nsis / portable).
            let candidates = [
                dir.join("resources").join("backend-bundle.zip"),
                dir.join("backend-bundle.zip"),
                dir.join("resources").join("_up_").join("backend-bundle.zip"),
            ];
            for c in &candidates {
                if c.exists() {
                    return Some(c.clone());
                }
            }
        }
    }
    // Dev checkout
    let dev = std::path::PathBuf::from(r"C:\meeting-recorder-v2\backend-bundle.zip");
    if dev.exists() {
        return Some(dev);
    }
    None
}

/// Where the extracted runtime lives per-user. Writable, survives app
/// updates, cleaned up only if the user explicitly removes %APPDATA%.
fn runtime_dir() -> std::path::PathBuf {
    let base = std::env::var("LOCALAPPDATA")
        .or_else(|_| std::env::var("APPDATA"))
        .unwrap_or_else(|_| std::env::var("USERPROFILE").unwrap_or_default());
    let d = std::path::PathBuf::from(base).join("MeetingRecorder").join("runtime");
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
/// Uses the Windows built-in `tar.exe` (BSD libarchive, ships in 10+).
fn ensure_runtime_extracted(zip_path: &std::path::Path) -> Result<std::path::PathBuf, String> {
    let runtime = runtime_dir();
    let version_file = runtime.join(".version");
    let server_py = runtime.join("server.py");
    let expected_version = zip_version(zip_path);

    // Essential files that MUST exist for the runtime to work. If any
    // are missing, we re-extract. We don't re-extract purely on version
    // mismatch because that destroys user-installed GPU torch wheels.
    // Note: we no longer check for python/pythonw.exe here — the
    // bundle doesn't ship an embeddable Python anymore (see
    // HANDOFF.md bug #1,2 and bootstrap_app_venv below).
    let essentials = [
        runtime.join("server.py"),
    ];
    let missing: Vec<_> = essentials.iter()
        .filter(|p| !p.exists())
        .map(|p| p.to_path_buf())
        .collect();

    let needs_extract = !missing.is_empty();

    if needs_extract {
        rlog(&format!("Runtime extraction needed (missing: {:?})", missing));
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
        // Critical: without CREATE_NO_WINDOW, tar.exe pops a console
        // window on Windows and the app can't proceed until it closes.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            tar_cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let status = tar_cmd
            .status()
            .map_err(|e| format!("tar.exe failed to run: {}. Is Windows 10+?", e))?;
        if !status.success() {
            return Err(format!("tar exited with {}", status));
        }
        std::fs::write(&version_file, &expected_version)
            .map_err(|e| format!("writing .version: {}", e))?;
        rlog(&format!("Extracted in {:.1}s", t0.elapsed().as_secs_f32()));
    } else {
        let marker = std::fs::read_to_string(&version_file).unwrap_or_default();
        if marker != expected_version {
            // Different zip shipped with this exe than what last
            // extracted the runtime — but the runtime is still intact.
            // Update the marker silently so we don't log this mismatch
            // forever. User-installed GPU wheels are preserved.
            let _ = std::fs::write(&version_file, &expected_version);
            rlog("Runtime version marker updated without re-extract");
        }
    }

    Ok(runtime)
}

/// Resolve the installed backend directory: prefer the extracted runtime
/// when a bundle zip is present; fall back to the dev checkout.
fn resolve_backend_dir() -> Option<std::path::PathBuf> {
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
    // Dev fallback — git checkout that already has backend/server.py
    let dev = std::path::PathBuf::from(r"C:\meeting-recorder-v2\backend");
    if dev.join("server.py").exists() {
        return Some(dev);
    }
    None
}

/// Where the app-managed venv lives. Created by bootstrap_app_venv on
/// first launch if no other Python is available.
fn app_venv_dir() -> std::path::PathBuf {
    let base = std::env::var("LOCALAPPDATA")
        .or_else(|_| std::env::var("APPDATA"))
        .unwrap_or_else(|_| std::env::var("USERPROFILE").unwrap_or_default());
    std::path::PathBuf::from(base).join("MeetingRecorder").join(".venv")
}

/// Locate a working Python interpreter.
///
/// Note: the embeddable Python at `runtime/python/` is intentionally NOT
/// checked here. The v2.1.x bundle ships a broken embeddable stdlib and
/// the whole embeddable approach hit dead ends (speechbrain LazyModule
/// recursion, missing DiagnosticOptions, etc — see HANDOFF.md bug #1,2).
/// We rely on a real Python install + venv instead.
fn resolve_python(backend_dir: &std::path::Path) -> Option<std::path::PathBuf> {
    // 1. App-managed venv created by bootstrap (production path on
    //    clean laptops: first launch creates this via `python -m venv`
    //    against a detected system Python 3.13).
    let app_venv = app_venv_dir();
    let app_candidates = [
        app_venv.join("Scripts").join("pythonw.exe"),
        app_venv.join("Scripts").join("python.exe"),
    ];
    for c in &app_candidates {
        if c.exists() {
            return Some(c.clone());
        }
    }
    // 2. Dev checkout venv next to server.py.
    let venv = [
        backend_dir.join(".venv").join("Scripts").join("pythonw.exe"),
        backend_dir.join(".venv").join("Scripts").join("python.exe"),
    ];
    for c in &venv {
        if c.exists() {
            return Some(c.clone());
        }
    }
    // 3. Legacy v1 venv — only present on the original dev machine.
    let legacy = [
        std::path::PathBuf::from(r"C:\meeting_recorder\.venv\Scripts\pythonw.exe"),
    ];
    for c in &legacy {
        if c.exists() {
            return Some(c.clone());
        }
    }
    None
}

/// Try to find a system-installed Python 3.13 that we can use to create
/// an app venv from. Checks py launcher, PATH, and common install paths.
fn find_system_python_313() -> Option<std::path::PathBuf> {
    #[cfg(windows)]
    use std::os::windows::process::CommandExt;
    #[cfg(windows)]
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    // 1. `py -3.13 -c "print(sys.executable)"`
    let mut cmd = Command::new("py");
    cmd.args(["-3.13", "-c", "import sys; print(sys.executable)"])
        .stdout(Stdio::piped()).stderr(Stdio::null());
    #[cfg(windows)] cmd.creation_flags(CREATE_NO_WINDOW);
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
    #[cfg(windows)] cmd.creation_flags(CREATE_NO_WINDOW);
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

/// Create the app venv and pip install requirements into it. Blocks for
/// several minutes on first launch while wheels download (~1.5 GB). All
/// pip output goes to %LOCALAPPDATA%\MeetingRecorder\bootstrap.log so the
/// user can tail it.
fn bootstrap_app_venv(runtime_dir: &std::path::Path) -> Result<std::path::PathBuf, String> {
    #[cfg(windows)]
    use std::os::windows::process::CommandExt;
    #[cfg(windows)]
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    let venv = app_venv_dir();
    if venv.join("Scripts").join("python.exe").exists() {
        rlog("App venv already exists — bootstrap skipped");
        return Ok(venv);
    }

    let system_py = find_system_python_313().ok_or_else(|| {
        "Python 3.13 not found on this machine. Install Python 3.13 from \
         https://www.python.org/downloads/ (per-user install, no admin needed; \
         check 'Add python.exe to PATH'), then restart Meeting Recorder.".to_string()
    })?;
    rlog(&format!("Bootstrap: system Python at {}", system_py.display()));

    let reqs = runtime_dir.join("requirements-cpu.txt");
    if !reqs.exists() {
        return Err(format!(
            "requirements-cpu.txt not found at {} — bundle may be corrupted",
            reqs.display()));
    }

    let bootstrap_log_path = log_dir().join("bootstrap.log");
    let open_log = || -> Result<(File, File), String> {
        let f = OpenOptions::new().create(true).append(true)
            .open(&bootstrap_log_path)
            .map_err(|e| format!("opening bootstrap.log: {}", e))?;
        let f2 = f.try_clone().map_err(|e| format!("cloning log fd: {}", e))?;
        Ok((f, f2))
    };

    // Step 1: python -m venv
    rlog(&format!("Bootstrap: creating venv at {}", venv.display()));
    let (out, err) = open_log()?;
    let mut c = Command::new(&system_py);
    c.args(["-m", "venv"]).arg(&venv)
        .stdout(Stdio::from(out)).stderr(Stdio::from(err));
    #[cfg(windows)] c.creation_flags(CREATE_NO_WINDOW);
    let status = c.status().map_err(|e| format!("venv cmd failed: {}", e))?;
    if !status.success() {
        return Err(format!("python -m venv exited with {} (see bootstrap.log)", status));
    }

    let venv_py = venv.join("Scripts").join("python.exe");
    if !venv_py.exists() {
        return Err(format!("venv python.exe missing after create: {}", venv_py.display()));
    }

    // Step 2: upgrade pip
    rlog("Bootstrap: upgrading pip");
    let (out, err) = open_log()?;
    let mut c = Command::new(&venv_py);
    c.args(["-m", "pip", "install", "--upgrade", "pip"])
        .stdout(Stdio::from(out)).stderr(Stdio::from(err));
    #[cfg(windows)] c.creation_flags(CREATE_NO_WINDOW);
    let _ = c.status();

    // Step 3: pip install -r requirements-cpu.txt — this is the slow part
    rlog("Bootstrap: pip install -r requirements-cpu.txt (3-5 min, see bootstrap.log)");
    let (out, err) = open_log()?;
    let t0 = std::time::Instant::now();
    let mut c = Command::new(&venv_py);
    c.args(["-m", "pip", "install", "-r"]).arg(&reqs)
        .stdout(Stdio::from(out)).stderr(Stdio::from(err));
    #[cfg(windows)] c.creation_flags(CREATE_NO_WINDOW);
    let status = c.status().map_err(|e| format!("pip install cmd failed: {}", e))?;
    if !status.success() {
        return Err(format!(
            "pip install exited with {} after {:.0}s (see bootstrap.log)",
            status, t0.elapsed().as_secs_f32()));
    }
    rlog(&format!("Bootstrap: pip install completed in {:.0}s",
        t0.elapsed().as_secs_f32()));

    Ok(venv)
}

/// Get the log directory. Use %LOCALAPPDATA% (non-roaming) — it's not
/// subject to OneDrive Known Folder Move on corporate laptops and
/// matches where other Windows desktop apps put their non-roaming data.
fn log_dir() -> std::path::PathBuf {
    let localappdata = std::env::var("LOCALAPPDATA")
        .or_else(|_| std::env::var("APPDATA"))
        .unwrap_or_else(|_| std::env::var("USERPROFILE").unwrap_or_default());
    let dir = std::path::PathBuf::from(localappdata).join("MeetingRecorder");
    let _ = std::fs::create_dir_all(&dir);
    // Migration breadcrumb: if we find stale logs from v2.1.1 under
    // %APPDATA% (Roaming), drop a README next to them pointing to the
    // new location so users who go hunting there see the redirect.
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
            // after a short delay. Corporate security agents like
            // SentinelOne / CrowdStrike sometimes kill subprocesses
            // during runtime; without this the app becomes dead-weight
            // with no recovery short of the user quitting and reopening.
            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                let mut consecutive_restarts = 0;
                loop {
                    std::thread::sleep(std::time::Duration::from_secs(5));
                    // During bootstrap (first-launch pip install), no
                    // child exists yet and we don't want to flap-respawn.
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
                    // Don't loop on a broken install — if Python keeps
                    // dying within seconds of spawn, back off.
                    consecutive_restarts += 1;
                    if consecutive_restarts > 5 {
                        rlog("Backend crashed 5+ times in a row — giving up. \
                              Check backend.log for the cause, reinstall if needed.");
                        break;
                    }
                    if port_in_use(BACKEND_PORT) {
                        continue; // something is listening, possibly another instance
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
    // Kill the existing child if we own one.
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut child) = guard.take() {
            match child.kill() {
                Ok(_) => rlog("Old backend killed"),
                Err(e) => rlog(&format!("Failed to kill old backend: {}", e)),
            }
            let _ = child.wait();
        }
    }
    // Wait briefly for the TCP port to free up so the new spawn binds cleanly.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while port_in_use(BACKEND_PORT) && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(200));
    }
    spawn_python_backend(&app).map_err(|e| e.to_string())?;
    Ok(())
}

fn spawn_python_backend(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    // If port is already bound, assume another instance is running and skip.
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
            // No usable Python — bootstrap an app venv from a system
            // Python 3.13. This blocks for 3-5 minutes on first launch
            // while pip downloads wheels.
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

    // Redirect Python stdout+stderr to a log file so we can see what
    // happens if the server crashes or is slow to start.
    let log_file = File::create(backend_log_path())
        .map_err(|e| format!("Couldn't create backend log file: {}", e))?;
    let log_file2 = log_file.try_clone()
        .map_err(|e| format!("Couldn't clone log fd: {}", e))?;

    let mut cmd = Command::new(&python_exe);
    cmd.arg("-u")  // unbuffered stdout/stderr — critical so logs flush immediately
       .arg(&server_py)
       .env("PYTHONUNBUFFERED", "1")
       .stdout(Stdio::from(log_file))
       .stderr(Stdio::from(log_file2));

    // Hide the Python console window on Windows
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

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
