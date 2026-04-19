use std::fs::{File, OpenOptions};
use std::io::Write;
use std::net::{Shutdown, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;

struct BackendProcess(Mutex<Option<Child>>);

const BACKEND_PORT: u16 = 17645;

/// Locations to check for the Python venv (in priority order).
const VENV_CANDIDATES: &[&str] = &[
    r"C:\meeting-recorder-v2\backend\.venv\Scripts\pythonw.exe",
    r"C:\meeting-recorder-v2\backend\.venv\Scripts\python.exe",
    r"C:\meeting_recorder\.venv\Scripts\pythonw.exe",
    r"C:\meeting_recorder\.venv\Scripts\python.exe",
];

const SERVER_CANDIDATES: &[&str] = &[
    r"C:\meeting-recorder-v2\backend\server.py",
];

fn find_first_existing(candidates: &[&str]) -> Option<std::path::PathBuf> {
    for c in candidates {
        let p = std::path::PathBuf::from(c);
        if p.exists() {
            return Some(p);
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

    let python_exe = find_first_existing(VENV_CANDIDATES)
        .ok_or("Could not find Python venv. Checked:\n  \
                C:\\meeting-recorder-v2\\backend\\.venv\\Scripts\\pythonw.exe\n  \
                C:\\meeting_recorder\\.venv\\Scripts\\pythonw.exe\n\
                Run `python setup.py` in the meeting-recorder-v2 folder.")?;
    let server_py = find_first_existing(SERVER_CANDIDATES)
        .ok_or("Could not find backend/server.py at C:\\meeting-recorder-v2\\backend\\server.py")?;

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
