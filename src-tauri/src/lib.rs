use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

struct BackendProcess(Mutex<Option<Child>>);

/// Locations to check for the Python venv (in priority order).
/// Single-user tool — first existing path wins.
const VENV_CANDIDATES: &[&str] = &[
    r"C:\meeting_recorder\.venv\Scripts\pythonw.exe",
    r"C:\meeting-recorder-v2\backend\.venv\Scripts\pythonw.exe",
];

const SERVER_CANDIDATES: &[&str] = &[
    r"C:\meeting-recorder-v2\backend\server.py",
    r".\backend\server.py",
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend = BackendProcess(Mutex::new(None));

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
                Ok(_) => log::info!("Python backend sidecar started"),
                Err(e) => log::error!("Backend startup failed: {}", e),
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(state) = window.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn spawn_python_backend(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let python_exe = find_first_existing(VENV_CANDIDATES)
        .ok_or("Could not find Python venv (checked v1 and v2 backend venvs)")?;
    let server_py = find_first_existing(SERVER_CANDIDATES)
        .ok_or("Could not find backend/server.py")?;

    log::info!("Spawning Python: {} {}",
               python_exe.display(), server_py.display());

    let child = Command::new(&python_exe)
        .arg(&server_py)
        .spawn()
        .map_err(|e| {
            log::error!("Failed to start Python backend: {}", e);
            e
        })?;

    if let Some(state) = app.try_state::<BackendProcess>() {
        *state.0.lock().unwrap() = Some(child);
    }
    Ok(())
}
