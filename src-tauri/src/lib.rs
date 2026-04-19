use std::fs::{File, OpenOptions};
use std::io::Write;
use std::net::{Shutdown, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;

struct BackendProcess(Mutex<Option<Child>>);

const BACKEND_PORT: u16 = 17645;

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

/// Hash of the bundled zip — written to a `.version` file next to the
/// extracted runtime so app updates trigger a re-extract.
fn zip_version(zip_path: &std::path::Path) -> String {
    if let Ok(meta) = std::fs::metadata(zip_path) {
        let len = meta.len();
        let mtime = meta.modified().ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs()).unwrap_or(0);
        return format!("{}-{}", len, mtime);
    }
    "unknown".to_string()
}

/// Extract the bundled backend zip into the per-user runtime directory
/// if it doesn't already exist or if the bundled version changed.
/// Uses the Windows built-in `tar.exe` (BSD libarchive, ships in 10+).
fn ensure_runtime_extracted(zip_path: &std::path::Path) -> Result<std::path::PathBuf, String> {
    let runtime = runtime_dir();
    let version_file = runtime.join(".version");
    let server_py = runtime.join("server.py");
    let expected_version = zip_version(zip_path);

    let needs_extract = if !server_py.exists() {
        rlog("Runtime not yet extracted");
        true
    } else {
        let current = std::fs::read_to_string(&version_file).unwrap_or_default();
        if current != expected_version {
            rlog(&format!("Runtime version mismatch (have {}, need {}) — re-extracting",
                current, expected_version));
            true
        } else {
            false
        }
    };

    if needs_extract {
        // Clean slate — remove old runtime so stale .pyc files don't linger.
        let _ = std::fs::remove_dir_all(&runtime);
        std::fs::create_dir_all(&runtime).map_err(|e| format!("mkdir runtime: {}", e))?;
        rlog(&format!("Extracting {} -> {}", zip_path.display(), runtime.display()));
        let t0 = std::time::Instant::now();
        let status = Command::new("tar")
            .arg("-xf").arg(zip_path)
            .arg("-C").arg(&runtime)
            .stdout(Stdio::null()).stderr(Stdio::null())
            .status()
            .map_err(|e| format!("tar.exe failed to run: {}. Is Windows 10+?", e))?;
        if !status.success() {
            return Err(format!("tar exited with {}", status));
        }
        std::fs::write(&version_file, &expected_version)
            .map_err(|e| format!("writing .version: {}", e))?;
        rlog(&format!("Extracted in {:.1}s", t0.elapsed().as_secs_f32()));
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

/// Locate pythonw.exe inside the extracted runtime or dev venv.
fn resolve_python(backend_dir: &std::path::Path) -> Option<std::path::PathBuf> {
    // Production: embeddable Python at backend/python/
    let embed = [
        backend_dir.join("python").join("pythonw.exe"),
        backend_dir.join("python").join("python.exe"),
    ];
    for c in &embed {
        if c.exists() {
            return Some(c.clone());
        }
    }
    // Dev: venv
    let venv = [
        backend_dir.join(".venv").join("Scripts").join("pythonw.exe"),
        backend_dir.join(".venv").join("Scripts").join("python.exe"),
    ];
    for c in &venv {
        if c.exists() {
            return Some(c.clone());
        }
    }
    // Legacy
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

/// Get the log directory — %APPDATA%\MeetingRecorder\
fn log_dir() -> std::path::PathBuf {
    let appdata = std::env::var("APPDATA")
        .unwrap_or_else(|_| std::env::var("USERPROFILE").unwrap_or_default());
    let dir = std::path::PathBuf::from(appdata).join("MeetingRecorder");
    let _ = std::fs::create_dir_all(&dir);
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
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            match spawn_python_backend(app.handle()) {
                Ok(_) => rlog("Python backend sidecar spawn requested"),
                Err(e) => rlog(&format!("ERROR: Backend startup failed: {}", e)),
            }
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
    let python_exe = resolve_python(&backend_dir).ok_or(
        "Could not find Python venv inside the installed backend. The \
         installer may be corrupted; reinstall from Releases.")?;

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
