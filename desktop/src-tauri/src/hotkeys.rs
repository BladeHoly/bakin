//! Keyboard shortcuts that work anywhere, even with a game in front (0.13
//! review): each of Bakin's actions can have one, set in Configuración ›
//! General › «Atajos de teclado» and kept in `BakinData/hotkeys.json`.
//!
//! «Modo gaming» (Ctrl+Shift+G by default) is the one that matters most: the
//! small window, always on top, letting the mouse through; pressed again, the
//! mouse comes back (until now only the tray menu did that).

use std::collections::BTreeMap;
use std::sync::Mutex;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutEvent, ShortcutState};

/// (id, name shown, default)
pub const ACTIONS: [(&str, &str, &str); 7] = [
    ("gaming", "Modo gaming: modo mínimo encima de todo, el mouse pasa a través", "ctrl+shift+KeyG"),
    ("mini", "Modo mínimo (abrir o volver a la ventana)", ""),
    ("toggle", "Reproducir o pausar", ""),
    ("next", "Tema siguiente", ""),
    ("prev", "Tema anterior", ""),
    ("wallpaper", "Siguiente fondo de pantalla", ""),
    ("show", "Mostrar Bakin", ""),
];

/// What each registered shortcut does, by the shortcut's id.
#[derive(Default)]
pub struct Hotkeys(Mutex<BTreeMap<u32, String>>);

fn file() -> std::path::PathBuf {
    crate::player::data_dir().join("hotkeys.json")
}

/// action → shortcut ("" = none), the defaults filled in.
pub fn load() -> BTreeMap<String, String> {
    let saved: BTreeMap<String, String> = std::fs::read_to_string(file()).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
    ACTIONS.iter().map(|(id, _, default)| (id.to_string(), saved.get(*id).cloned().unwrap_or_else(|| default.to_string()))).collect()
}

fn save(map: &BTreeMap<String, String>) -> Result<(), String> {
    std::fs::write(file(), serde_json::to_string_pretty(map).unwrap_or_default()).map_err(|e| e.to_string())
}

/// A test copy (BAKIN_HOME) keeps and shows its shortcuts but never takes
/// them from Windows: they would be the user's keys while the test runs.
fn live() -> bool {
    std::env::var_os("BAKIN_HOME").is_none()
}

/// Registers every saved shortcut; the ones Windows refuses (another program
/// has them) are left out and reported by `hotkeys_get`.
pub fn register_all(app: &AppHandle) {
    if !live() {
        return;
    }
    let gs = app.global_shortcut();
    let _ = gs.unregister_all();
    let state = app.state::<Hotkeys>();
    let mut ids = state.0.lock().unwrap();
    ids.clear();
    for (action, combo) in load() {
        if combo.is_empty() {
            continue;
        }
        if let Ok(sc) = combo.parse::<Shortcut>() {
            if gs.register(sc).is_ok() {
                ids.insert(sc.id(), action);
            }
        }
    }
}

/// The plugin's handler: run the action of the shortcut pressed.
pub fn on_shortcut(app: &AppHandle, sc: &Shortcut, event: ShortcutEvent) {
    if event.state != ShortcutState::Pressed {
        return;
    }
    let action = app.state::<Hotkeys>().0.lock().unwrap().get(&sc.id()).cloned();
    if let Some(action) = action {
        run(app, &action);
    }
}

/// Does an action (a shortcut pressed; the UI test calls it directly).
#[tauri::command]
pub fn hotkeys_run(app: AppHandle, action: String) {
    run(&app, &action);
}

fn run(app: &AppHandle, action: &str) {
    let app = app.clone();
    match action {
        "gaming" => {
            tauri::async_runtime::spawn(async move { gaming(app).await });
        }
        "mini" => {
            tauri::async_runtime::spawn(async move {
                if app.get_webview_window("mini").is_some() {
                    crate::mini::mini_expand(app).await;
                } else {
                    let _ = crate::mini::mini_open(app).await;
                }
            });
        }
        "toggle" | "next" | "prev" => {
            if let Some(p) = app.try_state::<crate::player::Player>() {
                let _ = p.call(json!({ "cmd": action }));
            }
        }
        "wallpaper" => {
            std::thread::spawn(move || {
                let _ = app.state::<crate::rotation::Rotation>().advance(&app, None, true);
            });
        }
        "show" => {
            crate::background::show_main(&app);
        }
        _ => {}
    }
}

/// On: the small window, always on top, the mouse going through. Pressed
/// while the mouse goes through: the mouse comes back (the window stays).
async fn gaming(app: AppHandle) {
    if crate::mini::is_through(&app) {
        crate::mini::set_through(&app, false);
        return;
    }
    if app.get_webview_window("mini").is_none() && crate::mini::mini_open(app.clone()).await.is_err() {
        return;
    }
    crate::mini::force_layer(&app, "top");
    crate::mini::set_through(&app, true);
}

/// For «Atajos de teclado»: each action, its shortcut, and whether Windows took it.
#[tauri::command]
pub fn hotkeys_get(app: AppHandle) -> Value {
    let registered: Vec<String> = app.state::<Hotkeys>().0.lock().unwrap().values().cloned().collect();
    let map = load();
    json!(ACTIONS
        .iter()
        .map(|(id, name, default)| {
            let combo = map.get(*id).cloned().unwrap_or_default();
            json!({ "id": id, "name": name, "combo": combo, "default": default,
                    "active": combo.is_empty() || !live() || registered.iter().any(|a| a == id) })
        })
        .collect::<Vec<_>>())
}

/// Sets (or clears, with "") an action's shortcut. Refused when it is not a
/// valid combination, another action has it, or another program holds it.
#[tauri::command]
pub fn hotkeys_set(app: AppHandle, action: String, combo: String) -> Result<Value, String> {
    if !ACTIONS.iter().any(|(id, _, _)| *id == action) {
        return Err("Esa acción no existe.".into());
    }
    let combo = combo.trim().to_string();
    let mut map = load();
    if !combo.is_empty() {
        let sc: Shortcut = combo.parse().map_err(|_| "Esa combinación no sirve: usá Ctrl, Alt o Shift con una tecla.".to_string())?;
        if map.iter().any(|(a, c)| a != &action && c.parse::<Shortcut>().is_ok_and(|o| o.id() == sc.id())) {
            return Err("Esa combinación ya es de otra acción de Bakin.".into());
        }
        // try it alone first: a program that holds it makes Windows refuse
        let gs = app.global_shortcut();
        let held_by_us = app.state::<Hotkeys>().0.lock().unwrap().contains_key(&sc.id());
        if !held_by_us && live() {
            gs.register(sc).map_err(|_| "Esa combinación ya la usa otro programa.".to_string())?;
            let _ = gs.unregister(sc);
        }
    }
    map.insert(action, combo);
    save(&map)?;
    register_all(&app);
    Ok(hotkeys_get(app))
}

/// While the page records a combination: Windows swallows a registered one,
/// so Bakin lets them all go for that moment (and takes them back after).
#[tauri::command]
pub fn hotkeys_pause(app: AppHandle, paused: bool) {
    if !live() {
        return;
    }
    if paused {
        let _ = app.global_shortcut().unregister_all();
        app.state::<Hotkeys>().0.lock().unwrap().clear();
    } else {
        register_all(&app);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_defaults_parse_and_differ() {
        let mut ids = std::collections::HashSet::new();
        for (_, _, default) in ACTIONS {
            if !default.is_empty() {
                let sc: Shortcut = default.parse().expect(default);
                assert!(ids.insert(sc.id()), "{default} twice");
            }
        }
        assert!("ctrl+shift+KeyG".parse::<Shortcut>().is_ok());
        assert!("KeyG".parse::<Shortcut>().is_ok());  // the page refuses a key alone
    }
}
