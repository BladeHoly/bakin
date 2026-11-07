//! Bakin keeps working with its window closed: a tray icon (open, pause,
//! resume, quit), close-to-tray while the queue has work, start with Windows
//! (hidden, with `--background`) and a single running instance (opening Bakin
//! again shows the existing window instead of starting a second engine).
//!
//! In the tray the window doesn't just hide: after a short while it is
//! destroyed, and WebView2 with it (hundreds of MB). Music plays in the shell
//! (player.rs) and downloads in the engine, so nothing depends on it; opening
//! Bakin creates it again and the page asks both for their state. While it
//! doesn't exist the shell sends the notifications the page would have sent.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde_json::json;
use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_notification::NotificationExt;

use crate::engine::Engine;
use crate::i18n::tr;

pub const BACKGROUND_ARG: &str = "--background";

/// Set by the page: true while there is work (a job, a queue, a timed pause)
/// and the user wants Bakin to keep going with the window closed.
#[derive(Default)]
pub struct KeepAlive(pub AtomicBool);

pub fn started_in_background() -> bool {
    has_background_arg(std::env::args())
}

fn has_background_arg(mut args: impl Iterator<Item = String>) -> bool {
    args.any(|a| a == BACKGROUND_ARG)
}

/// Whether Windows notifications are wanted (the page's «Avisos de Windows»).
pub struct Notify(pub AtomicBool);

impl Default for Notify {
    fn default() -> Self {
        Notify(AtomicBool::new(true))
    }
}

#[tauri::command]
pub fn set_notify(state: tauri::State<'_, Notify>, value: bool) {
    state.0.store(value, Ordering::Relaxed);
}

/// Counts hides and shows: a close scheduled before the window came back is dropped.
#[derive(Default)]
pub struct Hidden(pub AtomicU64);

/// How long a hidden window waits before it is closed: reopening right
/// away stays instant.
const CLOSE_AFTER: Duration = Duration::from_secs(20);

pub fn schedule_close(app: &AppHandle) {
    let generation = app.state::<Hidden>().0.fetch_add(1, Ordering::SeqCst) + 1;
    // hidden until it closes: WebView2 may give memory back meanwhile (PLAN 0.17.2)
    #[cfg(target_os = "windows")]
    if let Some(w) = app.get_webview_window("main") {
        crate::webview_memory_low(&w, true);
    }
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(CLOSE_AFTER);
        if app.state::<Hidden>().0.load(Ordering::SeqCst) != generation {
            return;
        }
        if let Some(w) = app.get_webview_window("main") {
            if !w.is_visible().unwrap_or(true) {
                let _ = w.destroy();
            }
        }
    });
}

/// Shows Bakin's window, creating it again if it was closed in the tray.
/// True when it had to be created (its page is still loading).
pub fn show_main(app: &AppHandle) -> bool {
    app.state::<Hidden>().0.fetch_add(1, Ordering::SeqCst); // a pending close no longer applies
    // wake the engine while the page loads (it may be asleep, scheduler.rs)
    let h = app.clone();
    std::thread::spawn(move || {
        let _ = h.state::<Engine>().ensure();
    });
    crate::mini::set_through(app, false); // a small window that let clicks through takes them again
    if let Some(w) = app.get_webview_window("main") {
        #[cfg(target_os = "windows")]
        crate::webview_memory_low(&w, false);  // seen again: normal memory
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        return false;
    }
    let Some(config) = app.config().app.windows.iter().find(|w| w.label == "main").cloned() else { return false };
    match WebviewWindowBuilder::from_config(app, &config).and_then(|b| b.build()) {
        Ok(w) => {
            crate::quiet_window_border(&w);
            let _ = w.show();
            let _ = w.set_focus();
            true
        }
        Err(_) => false,
    }
}

/// What the page would have notified, sent by the shell while the window
/// doesn't exist (the queue finished or paused, cookies stopped working).
#[derive(Default)]
pub struct QueueSeen(Mutex<Option<(i64, bool, String)>>);

/// What a queue event is worth telling while the window is away.
#[derive(Debug, PartialEq)]
enum QueueNotice {
    /// it had work and now it's empty
    Finished,
    /// it paused, for a reason not told yet
    Paused(String),
}

/// Compares a queue event with the previous one; nothing on the first event seen.
fn queue_notice(prev: Option<&(i64, bool, String)>, total: i64, paused: bool, reason: &str) -> Option<QueueNotice> {
    let (prev_total, _, prev_reason) = prev?;
    if *prev_total > 0 && total == 0 && !paused {
        Some(QueueNotice::Finished)
    } else if paused && !reason.is_empty() && reason != prev_reason.as_str() {
        Some(QueueNotice::Paused(reason.to_string()))
    } else {
        None
    }
}

pub fn engine_event(app: &AppHandle, event: &serde_json::Value) {
    let kind = event.get("kind").and_then(|k| k.as_str()).unwrap_or("");
    let away = app.get_webview_window("main").is_none() && app.state::<Notify>().0.load(Ordering::Relaxed);
    let say = |title: &str, body: &str| {
        let _ = app.notification().builder().title(title).body(body).show();
    };
    match kind {
        "queue" => {
            let total = event.get("total").and_then(|v| v.as_i64()).unwrap_or(0);
            let paused = event.get("paused").and_then(|v| v.as_bool()).unwrap_or(false);
            let reason = event.get("reason").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let state = app.state::<QueueSeen>();
            let mut seen = state.0.lock().unwrap();
            if away {
                match queue_notice(seen.as_ref(), total, paused, &reason) {
                    Some(QueueNotice::Finished) => say(tr("Bakin terminó"), tr("La cola quedó vacía: todo lo pendiente ya se procesó.")),
                    Some(QueueNotice::Paused(why)) => say(tr("Bakin pausó la cola"), &why),
                    None => {}
                }
            }
            *seen = Some((total, paused, reason));
        }
        "cookies_bad" if away => say(tr("Bakin: cookies vencidas"), tr("Exporta un cookies.txt nuevo para los temas que piden tu cuenta.")),
        _ => {}
    }
}

fn engine_call_detached(app: &AppHandle, method: &'static str) {
    let app = app.clone();
    std::thread::spawn(move || {
        let _ = app.state::<Engine>().call(method, json!({}), Duration::from_secs(15));
    });
}

/// The tray's background items: only usable when a monitor rotates.
pub struct WallpaperItems {
    next: tauri::menu::MenuItem<tauri::Wry>,
    pause: tauri::menu::MenuItem<tauri::Wry>,
}

pub fn sync_wallpaper_tray(app: &AppHandle) {
    let Some(items) = app.try_state::<WallpaperItems>() else { return };
    let cfg = crate::rotation::load();
    let any = cfg.monitors.values().any(|r| r.on);
    let _ = items.next.set_enabled(any);
    let _ = items.pause.set_enabled(any);
    let _ = items.pause.set_text(if cfg.paused { tr("Reanudar la rotación de fondos") } else { tr("Pausar la rotación de fondos") });
}

pub fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItemBuilder::with_id("open", tr("Abrir Bakin")).build(app)?;
    let pause = MenuItemBuilder::with_id("pause", tr("Pausar descargas")).build(app)?;
    let resume = MenuItemBuilder::with_id("resume", tr("Reanudar descargas")).build(app)?;
    let mini = MenuItemBuilder::with_id("mini", tr("Modo mínimo")).build(app)?;
    let mouse = MenuItemBuilder::with_id("mini_mouse", tr("Salir del modo gaming")).enabled(false).build(app)?;
    let wp_next = MenuItemBuilder::with_id("wp_next", tr("Siguiente fondo")).build(app)?;
    let wp_pause = MenuItemBuilder::with_id("wp_pause", tr("Pausar la rotación de fondos")).build(app)?;
    let quit = MenuItemBuilder::with_id("quit", tr("Salir de Bakin")).build(app)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let sep3 = PredefinedMenuItem::separator(app)?;
    let menu = MenuBuilder::new(app).items(&[&open, &mini, &mouse, &sep2, &pause, &resume, &sep3, &wp_next, &wp_pause, &sep, &quit]).build()?;
    app.manage(crate::mini::MouseItem(mouse));
    app.manage(WallpaperItems { next: wp_next, pause: wp_pause });
    sync_wallpaper_tray(app);
    let mut tray = TrayIconBuilder::with_id("bakin")
        .tooltip("Bakin")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => {
                show_main(app);
            }
            "mini" => {
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    let _ = crate::mini::mini_open(app).await;
                });
            }
            "mini_mouse" => crate::mini::set_through(app, false),
            "wp_next" => {
                let app = app.clone();
                std::thread::spawn(move || {
                    let _ = app.state::<crate::rotation::Rotation>().advance(&app, None, true);
                });
            }
            "wp_pause" => {
                let _ = crate::rotation::set_paused(app, !crate::rotation::load().paused);
            }
            "pause" => engine_call_detached(app, "pause_queue"),
            "resume" => engine_call_detached(app, "resume_queue"),
            "quit" => app.exit(0), // the engine saves the queue on the way out
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

/// Closing the window while there is work hides it to the tray instead; the
/// small window remembers where it was moved to.
pub fn on_window_event(window: &tauri::Window, event: &WindowEvent) {
    if let WindowEvent::Resized(_) = event {
        crate::mini::on_resized(window);
    }
    if let WindowEvent::Moved(_) = event {
        crate::mini::on_moved(window);
    }
    if let WindowEvent::CloseRequested { api, .. } = event {
        if window.label() == "main" && (window.state::<KeepAlive>().0.load(Ordering::Relaxed) || crate::rotation::any_on()) {
            api.prevent_close();
            let _ = window.hide();
            schedule_close(window.app_handle());
        }
    }
}

#[tauri::command]
pub fn set_keep_alive(state: tauri::State<'_, KeepAlive>, value: bool) {
    state.0.store(value, Ordering::Relaxed);
}

/// A Windows notification (queue finished, paused until tomorrow, ...).
#[tauri::command]
pub fn notify(app: AppHandle, title: String, body: String) -> Result<(), String> {
    let (title, body) = (clip(&title, 80), clip(&body, 300));
    app.notification().builder().title(title).body(body).show().map_err(|e| e.to_string())
}

/// The first `max` characters (not bytes: a cut never splits one).
fn clip(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

#[tauri::command]
pub fn autostart_get(app: AppHandle) -> bool {
    app.autolaunch().is_enabled().unwrap_or(false)
}

#[tauri::command]
pub fn autostart_set(app: AppHandle, enabled: bool) -> Result<bool, String> {
    let a = app.autolaunch();
    if enabled { a.enable() } else { a.disable() }.map_err(|e| e.to_string())?;
    Ok(a.is_enabled().unwrap_or(false))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seen(total: i64, paused: bool, reason: &str) -> (i64, bool, String) {
        (total, paused, reason.to_string())
    }

    #[test]
    fn the_first_queue_event_is_only_remembered() {
        assert_eq!(queue_notice(None, 0, false, ""), None);
        assert_eq!(queue_notice(None, 0, true, "Sin conexión"), None);
    }

    #[test]
    fn an_emptied_queue_is_told_once() {
        assert_eq!(queue_notice(Some(&seen(3, false, "")), 0, false, ""), Some(QueueNotice::Finished));
        assert_eq!(queue_notice(Some(&seen(0, false, "")), 0, false, ""), None); // it was already empty
        assert_eq!(queue_notice(Some(&seen(3, false, "")), 2, false, ""), None); // still working
        assert_eq!(queue_notice(Some(&seen(3, false, "")), 0, true, ""), None); // paused with nothing left: no reason
    }

    #[test]
    fn a_pause_is_told_once_per_reason() {
        let why = "YouTube pidió esperar";
        assert_eq!(queue_notice(Some(&seen(3, false, "")), 3, true, why), Some(QueueNotice::Paused(why.into())));
        assert_eq!(queue_notice(Some(&seen(3, true, why)), 3, true, why), None);
        assert_eq!(queue_notice(Some(&seen(3, true, why)), 3, true, "Disco lleno"), Some(QueueNotice::Paused("Disco lleno".into())));
        assert_eq!(queue_notice(Some(&seen(3, false, "")), 3, true, ""), None); // paused by the user
    }

    #[test]
    fn the_background_argument() {
        let args = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>().into_iter();
        assert!(has_background_arg(args(&["bakin.exe", "--background"])));
        assert!(!has_background_arg(args(&["bakin.exe", "bakin://download?url=x"])));
        assert!(!has_background_arg(args(&["bakin.exe", "--background=1"])));
    }

    #[test]
    fn notifications_are_cut_by_characters() {
        assert_eq!(clip("canción", 6), "canció");
        assert_eq!(clip("ñ".repeat(100).as_str(), 80).chars().count(), 80);
        assert_eq!(clip("corto", 80), "corto");
    }
}
