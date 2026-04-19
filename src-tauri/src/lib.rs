use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

struct BackendProcess(Mutex<Option<Child>>);

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
            spawn_python_backend(app.handle())?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                // Kill the Python sidecar when the app closes
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
    // Dev: point to the existing venv's python.exe for fast iteration
    // Prod: expect a bundled pythonw.exe under resources/python/
    let python_exe = if cfg!(debug_assertions) {
        std::path::PathBuf::from(r"C:\meeting_recorder\.venv\Scripts\python.exe")
    } else {
        let resource_dir = app.path().resource_dir()?;
        resource_dir.join("python").join("pythonw.exe")
    };

    let server_py = if cfg!(debug_assertions) {
        std::path::PathBuf::from(r"C:\meeting-recorder-v2\backend\server.py")
    } else {
        let resource_dir = app.path().resource_dir()?;
        resource_dir.join("backend").join("server.py")
    };

    let child = Command::new(python_exe)
        .arg(server_py)
        .spawn()
        .map_err(|e| {
            log::error!("Failed to start Python backend: {}", e);
            e
        })?;

    if let Some(state) = app.try_state::<BackendProcess>() {
        *state.0.lock().unwrap() = Some(child);
    }

    log::info!("Python backend sidecar started");
    Ok(())
}
