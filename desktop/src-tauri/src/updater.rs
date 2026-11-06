//! Update check and install (tauri-plugin-updater).
//!
//! Releases are built by the public repo's workflow and published there
//! (BladeHoly/bakin). Every installer is signed with Bakin's updater key
//! and checked against the public key in tauri.conf.json before it runs, so a
//! tampered download is rejected. Nothing is installed without the user
//! pressing "Actualizar ahora", and never while a download is running.

use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_updater::{Update, UpdaterExt};

use crate::engine::Engine;

#[derive(Default)]
pub struct PendingUpdate(Mutex<Option<Update>>);

#[derive(Serialize)]
pub struct UpdateInfo {
    version: String,
    current_version: String,
    /// Changelog of every release (Markdown, newest first); the page keeps the
    /// sections newer than the installed version.
    notes: Option<String>,
}

#[tauri::command]
pub async fn check_update(app: AppHandle, pending: State<'_, PendingUpdate>) -> Result<Option<UpdateInfo>, String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| format!("No se pudo buscar actualizaciones: {e}"))?;
    let info = update.as_ref().map(|u| UpdateInfo {
        version: u.version.clone(),
        current_version: u.current_version.clone(),
        notes: u.body.clone(),
    });
    *pending.0.lock().unwrap() = update;
    Ok(info)
}

#[tauri::command]
pub async fn install_update(app: AppHandle, pending: State<'_, PendingUpdate>) -> Result<(), String> {
    let busy = {
        let handle = app.clone();
        tauri::async_runtime::spawn_blocking(move || {
            handle.state::<Engine>().call("job_running", json!({}), Duration::from_secs(10))
        })
        .await
        .map_err(|e| e.to_string())?
    };
    if busy.unwrap_or(Value::Bool(false)) == Value::Bool(true) {
        return Err("Hay una descarga en curso: actualiza cuando termine.".into());
    }
    let Some(found) = pending.0.lock().unwrap().take() else {
        return Err("No hay ninguna actualización pendiente.".into());
    };
    // Rebuild with an exit hook: on Windows the installer starts and this
    // process ends right away, so the engine has to be stopped first.
    let exit_handle = app.clone();
    let updater = app
        .updater_builder()
        .on_before_exit(move || {
            if let Some(engine) = exit_handle.try_state::<Engine>() {
                engine.shutdown();
            }
        })
        .build()
        .map_err(|e| e.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|e| e.to_string())?
        .filter(|u| u.version == found.version)
        .ok_or("La actualización ya no está disponible.")?;

    let progress = app.clone();
    let mut downloaded: u64 = 0;
    update
        .download_and_install(
            move |chunk, total| {
                downloaded += chunk as u64;
                let _ = progress.emit("update-progress", json!({"downloaded": downloaded, "total": total}));
            },
            || {},
        )
        .await
        .map_err(|e| format!("No se pudo instalar la actualización: {e}"))?;
    app.restart();
}
