//! Bakin desktop shell: a frameless window that hosts the glass-midas UI and
//! talks to the Python download engine (bakin.sidecar) running as a child
//! process. Window chrome (drag, resize, maximize) is native Tauri.
//!
//! The window is opaque on purpose, with glass-midas frost surfaces on a plain
//! dark base. Windows' own materials (Acrylic, Mica) were tried and dropped:
//! inside a Tauri/WebView2 window they only blur while the window has focus
//! and turn solid grey otherwise.

mod background;
mod covers;
mod engine;
mod home;
mod hotkeys;
mod i18n;
mod lan;
mod links;
mod mini;
mod player;
mod rotation;
mod scheduler;
#[cfg(windows)]
mod smtc;
#[cfg(windows)]
mod usage;
mod wallpaper;
mod updater;

use std::time::Duration;

use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, RunEvent};

use engine::Engine;

/// Methods the page may call on the engine (the engine checks again).
const ENGINE_METHODS: &[&str] = &[
    "get_state", "job_running",
    "start", "cancel", "cancel_current", "remove_queued", "resume_queue", "pause_queue", "set_out_dir", "undo_move", "open_dir", "log_info", "open_logs", "sleep_if_idle", "resume_due", "wallpaper_sources", "wallhaven_account", "wallhaven_account_set", "wallhaven_account_clear", "wallhaven_my_collections", "wallhaven_follow", "wallhaven_collection", "queue_pause", "queue_resume",
    "library_overview", "library_items", "library_set_favorite", "library_tag", "library_delete",
    "library_deleted", "library_restore", "library_forget", "library_add_folder",
    "library_remove_folder", "library_open", "library_reveal",
    "media_settings", "media_analyze", "music_tracks", "music_cover", "music_played", "music_state_get", "music_state_set", "music_overview", "music_groups", "music_playlist_save", "music_playlist_add", "music_playlist_remove", "music_playlist_delete", "music_playlist_export", "music_mirror_set", "music_mirror_off", "music_mirror_sync", "music_mirror_open", "lan_library", "lan_star", "lan_scrobble", "lan_cover", "lan_lyrics", "lan_download", "lan_code", "first_run_done", "cookies_status", "set_hours", "follows_list", "follow_add", "follow_remove", "follow_check_now", "media_save_preset", "media_delete_preset", "media_restore_builtins", "media_set_option",
    "media_import_presets", "media_set_site_pref", "media_scan_folder", "media_redownload", "media_maintenance",
    "tools_install", "tools_check",
    "library_upgrade_audio", "convert_formats", "convert_inspect", "convert_start", "convert_open",
    "wallhaven_search", "wallhaven_download", "wallhaven_open", "music_search_open", "cookies_vault", "cookies_import", "cookies_delete_original", "cookies_forget", "cookies_use_vault", "library_redownload", "music_clean_propose", "music_clean_apply", "music_tags_undo", "music_mb_start", "music_mb_enable", "catalog_search", "catalog_page", "catalog_thumbs", "catalog_download", "spotify_resolve", "spotify_cancel", "read_text_file", "settings_set", "music_loudness_scan", "music_gains", "music_lyrics", "music_lyrics_fetch", "data_dir_info", "data_dir_move", "backup_export", "backup_inspect", "backup_import", "music_mb_cancel", "music_dupes", "music_dupes_resolve", "music_stats",
];

/// A play counted while the engine sleeps: one line in music/plays-pending.jsonl
/// (bakin/music.py adds them up when it next reads the play counts). Skips always go this way,
/// to music/skips-pending.jsonl (they matter only when the mixes are made).
fn pending_play(id: &str) {
    pending_line("plays-pending.jsonl", id);
}

fn pending_line(file: &str, id: &str) {
    use std::io::Write as _;
    if id.is_empty() {
        return;
    }
    let dir = player::data_dir().join("music");
    let _ = std::fs::create_dir_all(&dir);
    let at = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join(file)) {
        let _ = writeln!(f, "{}", serde_json::json!({ "id": id, "at": at }));
    }
}

#[tauri::command]
async fn engine_call(app: AppHandle, method: String, params: Option<Value>) -> Result<Value, String> {
    if !ENGINE_METHODS.contains(&method.as_str()) {
        return Err(format!("método no permitido: {method}"));
    }
    // Never block the UI thread. Library reads build thumbnails from full-size
    // photos and Wallhaven waits its turn between requests: generous timeouts.
    let slow = method == "set_out_dir"
        || method.starts_with("library_") || method.starts_with("music_")
        || matches!(method.as_str(), "media_scan_folder" | "media_redownload" | "tools_check" | "media_settings" | "convert_inspect")
        || method.starts_with("wallhaven_");
    let timeout = Duration::from_secs(if method == "media_analyze" || method == "follow_add" { 320 } else if slow { 60 } else { 15 });
    let params = params.unwrap_or(Value::Object(Default::default()));
    tauri::async_runtime::spawn_blocking(move || app.state::<Engine>().call(&method, params, timeout))
        .await
        .map_err(|e| e.to_string())?
}

/// Let the window show files (videos, audio, full-size images) from the
/// library's folders only. The list comes from the engine, not from the page.
#[tauri::command]
async fn sync_media_scope(app: AppHandle) -> Result<(), String> {
    let handle = app.clone();
    let roots = tauri::async_runtime::spawn_blocking(move || {
        handle.state::<Engine>().call("library_roots", Value::Object(Default::default()), Duration::from_secs(60))
    })
    .await
    .map_err(|e| e.to_string())??;
    let scope = app.asset_protocol_scope();
    for root in roots.as_array().into_iter().flatten().filter_map(Value::as_str) {
        scope.allow_directory(root, true).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Bakin starts again (PLAN 0.18.4: the data folder was moved; the new one is used from the start).
#[tauri::command]
fn app_restart(app: AppHandle) {
    if let Some(p) = app.try_state::<player::Player>() {
        let _ = p.call(serde_json::json!({ "cmd": "save" }));
    }
    app.restart();
}

/// WebView2's memory target (PLAN 0.17.2): LOW for the mini window (a remote control: slower
/// scripts don't matter) and for the main one while it's hidden before it closes. Fails quietly on
/// an older WebView2 runtime.
#[cfg(target_os = "windows")]
pub(crate) fn webview_memory_low(window: &tauri::WebviewWindow, low: bool) {
    let _ = window.with_webview(move |wv| {
        use webview2_com::Microsoft::Web::WebView2::Win32::{
            ICoreWebView2_19, COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_LOW, COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_NORMAL,
        };
        use windows_core::Interface;
        // SAFETY: COM calls on the webview's own controller, on its thread (with_webview runs there).
        unsafe {
            if let Ok(core) = wv.controller().CoreWebView2() {
                if let Ok(v19) = core.cast::<ICoreWebView2_19>() {
                    let level = if low { COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_LOW } else { COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_NORMAL };
                    let _ = v19.SetMemoryUsageTargetLevel(level);
                }
            }
        }
    });
}

/// Windows 11 draws a 1 px light border around undecorated windows with a
/// shadow; make it a quiet hairline that matches the glass rims.
#[cfg(target_os = "windows")]
pub(crate) fn quiet_window_border(window: &tauri::WebviewWindow) {
    use windows_sys::Win32::Graphics::Dwm::DwmSetWindowAttribute;
    const DWMWA_BORDER_COLOR: u32 = 34;
    if let Ok(hwnd) = window.hwnd() {
        let colorref: u32 = 0x0032_2C2A; // COLORREF 0x00BBGGRR -> rgb(42, 44, 50)
        // SAFETY: valid HWND of our own window; the value outlives the call.
        unsafe {
            DwmSetWindowAttribute(
                hwnd.0 as _,
                DWMWA_BORDER_COLOR as _,
                &colorref as *const u32 as *const core::ffi::c_void,
                core::mem::size_of::<u32>() as u32,
            );
        }
    }
}

/// The page's remote control for the player (src/player.rs).
#[tauri::command]
async fn player_call(app: AppHandle, cmd: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || app.state::<player::Player>().call(cmd))
        .await
        .map_err(|e| e.to_string())?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        // First: a second launch just shows the running Bakin (one engine, one queue).
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            background::show_main(app);
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_autostart::Builder::new().args([background::BACKGROUND_ARG]).build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(updater::PendingUpdate::default())
        .manage(background::KeepAlive::default())
        .manage(background::Notify::default())
        .manage(background::Hidden::default())
        .manage(background::QueueSeen::default())
        .manage(links::Inbox::default())
        .manage(home::LanState::default())
        .manage(mini::MiniState::default())
        .manage(scheduler::Scheduler::default())
        .manage(rotation::Rotation::default())
        .manage(hotkeys::Hotkeys::default())
        .plugin(tauri_plugin_global_shortcut::Builder::new().with_handler(hotkeys::on_shortcut).build())
        .manage(links::ClipboardWatch::default())
        .setup(|app| {
            app.manage(Engine::spawn(app.handle().clone()));
            // the library's previews are files in Bakin's preview cache (PLAN 0.17.1): only that
            // folder, never the rest of BakinData (the vault, settings…)
            let _ = app.asset_protocol_scope().allow_directory(player::data_dir().join("thumbs"), true);
            let handle = app.handle().clone();
            app.manage(player::Player::start(move |kind, data| {
                if kind == "played" {
                    // play counts live with the library, in the engine; while it
                    // sleeps they wait in a file it adds up when it starts
                    let h = handle.clone();
                    let id = data.get("id").and_then(|v| v.as_str()).unwrap_or_default().to_string();
                    std::thread::spawn(move || {
                        let engine = h.state::<Engine>();
                        if engine.running() {
                            let _ = engine.call("music_played", serde_json::json!({ "item_id": id }), Duration::from_secs(15));
                        } else {
                            pending_play(&id);
                        }
                    });
                }
                if kind == "skipped" {
                    let id = data.get("id").and_then(|v| v.as_str()).unwrap_or_default().to_string();
                    std::thread::spawn(move || pending_line("skips-pending.jsonl", &id));
                }
                #[cfg(windows)]
                if kind == "state" {
                    if let Some(smtc) = handle.try_state::<smtc::Smtc>() {
                        smtc.update(&handle, &data);
                    }
                }
                let _ = handle.emit("player", serde_json::json!({ "kind": kind, "data": data }));
            }));
            #[cfg(windows)]
            if let Some(controls) = smtc::Smtc::create(app.handle()) {
                app.manage(controls);
            }
            #[cfg(target_os = "windows")]
            if let Some(window) = app.get_webview_window("main") {
                quiet_window_border(&window);
            }
            background::build_tray(app.handle())?;
            scheduler::start(app.handle().clone());
            hotkeys::register_all(app.handle());
            links::setup(app.handle());
            home::autostart(app.handle()); // «En tu casa», if it was on (PLAN 0.21.1)
            // Started with Windows: stay in the tray; otherwise show the window.
            if !background::started_in_background() {
                background::show_main(app.handle());
            } else if let Some(w) = app.get_webview_window("main") {
                // started with Windows: the window isn't needed until opened
                let _ = w.destroy();
            }
            Ok(())
        })
        .on_window_event(background::on_window_event)
        .invoke_handler(tauri::generate_handler![
            app_restart,
            engine_call,
            player_call,
            home::lan_status,
            home::lan_set,
            home::lan_new_code,
            sync_media_scope,
            updater::check_update,
            updater::install_update,
            background::set_keep_alive,
            background::set_notify,
            usage::usage,
            mini::mini_open,
            mini::mini_popup,
            covers::track_cover,
            wallpaper::wallpaper_monitors,
            wallpaper::wallpaper_set,
            wallpaper::lockscreen_set,
            rotation::wallpaper_rotation_get,
            rotation::wallpaper_rotation_set,
            rotation::wallpaper_next,
            rotation::wallpaper_rotation_pause,
            rotation::wallpaper_shown,
            rotation::wallpaper_focus_get,
            rotation::wallpaper_focus_set,
            hotkeys::hotkeys_get,
            hotkeys::hotkeys_set,
            hotkeys::hotkeys_pause,
            hotkeys::hotkeys_run,
            mini::mini_expand,
            mini::mini_layer,
            mini::mini_through,
            mini::mini_state,
            mini::mini_layout,
            mini::mini_prefs_get,
            mini::mini_prefs_set,
            background::notify,
            background::autostart_get,
            links::pending_links,
            links::set_clipboard_watch,
            links::open_extension_folder,
            background::autostart_set
        ])
        .build(tauri::generate_context!())
        .expect("error while building Bakin");

    app.run(|handle, event| {
        // the window closed in the tray is not "the last window closed":
        // Bakin keeps going while it has music or downloads (KeepAlive)
        if let RunEvent::ExitRequested { api, code: None, .. } = &event {
            // work, music, or backgrounds rotating: Bakin stays in the tray
            if handle.state::<background::KeepAlive>().0.load(std::sync::atomic::Ordering::Relaxed) || rotation::any_on() {
                api.prevent_exit();
            }
        }
        if let RunEvent::Exit = event {
            if let Some(player) = handle.try_state::<player::Player>() {
                let _ = player.call(serde_json::json!({ "cmd": "save" }));  // where the music was
                player.shutdown();
            }
            if let Some(engine) = handle.try_state::<Engine>() {
                engine.shutdown();
            }
        }
    });
}
