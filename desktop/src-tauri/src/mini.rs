//! «Modo mínimo»: only the music bar, in a small window of its own. The music
//! lives in the shell (player.rs), so the big window can go: it is hidden and
//! then destroyed like in the tray, and the small one is a remote control.
//!
//! Three layouts (a bar, a card, the song only) and, unless the user turns
//! it off, the window comes back where and how it was: kept by the shell in
//! `BakinData/mini.json`, read before the window is built so it never jumps.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, LogicalSize, Manager, WebviewUrl, WebviewWindowBuilder};

const LABEL: &str = "mini";
const MARGIN: f64 = 24.0;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Layout {
    /// One row: cover, song, buttons; everything in sight (0.24).
    #[default]
    Bar,
    /// The cover as a square, the controls over it on hover; resizable (0.24).
    Card,
    /// Cover and song only: what the gaming mode shows while clicks go through. Not a shape to
    /// pick since 0.24 (an older choice of it opens as the bar).
    Info,
}

/// The card's side: the user resizes it by its corner, within these.
pub const CARD_MIN: f64 = 160.0;
pub const CARD_MAX: f64 = 400.0;
pub const CARD_DEFAULT: f64 = 224.0;

impl Layout {
    fn size(self, card: f64) -> (f64, f64) {
        match self {
            Layout::Bar => (420.0, 64.0),
            Layout::Card => {
                let side = card.clamp(CARD_MIN, CARD_MAX);
                (side, side)
            }
            Layout::Info => (340.0, 56.0),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Prefs {
    /// «Recordar dónde estaba»: on unless the user turns it off.
    remember: bool,
    x: Option<f64>,
    y: Option<f64>,
    layout: Layout,
    /// the card's side, as the user left it
    card: f64,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs { remember: true, x: None, y: None, layout: Layout::Bar, card: CARD_DEFAULT }
    }
}

/// The prefs, the layout shown right now (Info while clicks go through) and
/// a counter that collapses a burst of moves into one write.
#[derive(Default)]
pub struct MiniState {
    prefs: Mutex<Option<Prefs>>,
    through: Mutex<bool>,
    moves: AtomicU64,
    /// a layer the shell asked for (the gaming shortcut): the page takes it when it loads
    layer: Mutex<Option<String>>,
    /// how far the window moved up to make room for a menu (0: it grew down, or none is open)
    popup_up: Mutex<f64>,
    /// a menu is open: the window is taller than its shape, on purpose
    popped: Mutex<bool>,
    resizes: AtomicU64,
}

impl MiniState {
    fn prefs(&self) -> Prefs {
        let mut p = self.prefs.lock().unwrap();
        p.get_or_insert_with(load).clone()
    }
    fn update(&self, f: impl FnOnce(&mut Prefs)) -> Prefs {
        let mut p = self.prefs.lock().unwrap();
        let prefs = p.get_or_insert_with(load);
        f(prefs);
        save(prefs);
        prefs.clone()
    }
}

fn prefs_path() -> PathBuf {
    crate::player::data_dir().join("mini.json")
}

fn load() -> Prefs {
    std::fs::read_to_string(prefs_path()).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
}

fn save(prefs: &Prefs) {
    let path = prefs_path();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let tmp = path.with_extension("tmp");
    if let Ok(text) = serde_json::to_string_pretty(prefs) {
        if std::fs::write(&tmp, text).is_ok() {
            let _ = std::fs::rename(&tmp, &path);
        }
    }
}

/// Where a `w`×`h` window remembered at (`x`, `y`) may go: inside the work
/// area of the monitor that holds its centre, pushed in if it sticks out.
/// None when that monitor is gone (unplugged, another layout of screens).
/// Areas are (x, y, width, height), all logical pixels.
pub fn clamp_to_monitors(x: f64, y: f64, w: f64, h: f64, areas: &[(f64, f64, f64, f64)]) -> Option<(f64, f64)> {
    let (cx, cy) = (x + w / 2.0, y + h / 2.0);
    let &(ax, ay, aw, ah) = areas.iter().find(|&&(ax, ay, aw, ah)| cx >= ax && cx < ax + aw && cy >= ay && cy < ay + ah)?;
    Some((x.min(ax + aw - w).max(ax), y.min(ay + ah - h).max(ay)))
}

/// The default place of a `w`×`h` window: the bottom right of a work area, `MARGIN` in.
fn bottom_right((ax, ay, aw, ah): (f64, f64, f64, f64), w: f64, h: f64) -> (f64, f64) {
    (ax + aw - w - MARGIN, ay + ah - h - MARGIN)
}

/// Whether a window at `pos`, `height` tall, can grow `extra` down and stay on the work area its
/// top-left corner is on (no such area: nothing stops it).
fn grows_down(pos: (f64, f64), height: f64, extra: f64, areas: &[(f64, f64, f64, f64)]) -> bool {
    let bottom = areas
        .iter()
        .find(|&&(ax, ay, aw, ah)| pos.0 >= ax && pos.0 < ax + aw && pos.1 >= ay && pos.1 < ay + ah)
        .map(|&(_, ay, _, ah)| ay + ah)
        .unwrap_or(f64::MAX);
    pos.1 + height + extra <= bottom
}

fn work_areas(app: &AppHandle) -> Vec<(f64, f64, f64, f64)> {
    let Some(w) = app.webview_windows().into_values().next() else { return Vec::new() };
    w.available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|m| {
            let s = m.scale_factor();
            let a = m.work_area();
            (a.position.x as f64 / s, a.position.y as f64 / s, a.size.width as f64 / s, a.size.height as f64 / s)
        })
        .collect()
}

/// Opens the small window and sends the big one to the tray.
/// (Async: building a window from a sync command blocks on Windows.)
#[tauri::command]
pub async fn mini_open(app: AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.show();
        let _ = w.set_focus();
    } else {
        let prefs = app.state::<MiniState>().prefs();
        let layout = shown(&prefs, false);
        let (width, height) = layout.size(prefs.card);
        // the big window's settings as the base: WebView2 refuses a second
        // webview in the same profile with other browser arguments
        let mut cfg = app.config().app.windows.iter().find(|w| w.label == "main").cloned().ok_or("sin configuración de ventana")?;
        cfg.label = LABEL.into();
        cfg.url = WebviewUrl::App("mini.html".into());
        cfg.title = "Bakin".into();
        (cfg.width, cfg.height) = (width, height);
        (cfg.min_width, cfg.min_height, cfg.max_width, cfg.max_height) = (None, None, None, None);
        cfg.resizable = false;
        cfg.maximizable = false;
        cfg.center = false;
        cfg.always_on_top = true;
        cfg.visible = false;
        // see-through: the bar and its menu draw their own rounded boxes, and the room the window
        // makes for the menu shows the desktop, not a dark block (the user, 2026-11-07)
        cfg.transparent = true;
        cfg.background_color = None;
        cfg.shadow = false;
        let areas = work_areas(&app);
        let remembered = match (prefs.remember, prefs.x, prefs.y) {
            (true, Some(x), Some(y)) => clamp_to_monitors(x, y, width, height, &areas),
            _ => None,
        };
        // else bottom right of the screen the big window is on
        let fallback = || {
            let main = app.get_webview_window("main");
            let m = main.as_ref().and_then(|w| w.current_monitor().ok().flatten()).or_else(|| app.primary_monitor().ok().flatten())?;
            let s = m.scale_factor();
            let a = m.work_area();
            let area = (a.position.x as f64 / s, a.position.y as f64 / s, a.size.width as f64 / s, a.size.height as f64 / s);
            Some(bottom_right(area, width, height))
        };
        if let Some((x, y)) = remembered.or_else(fallback) {
            (cfg.x, cfg.y) = (Some(x), Some(y));
        }
        let w = WebviewWindowBuilder::from_config(&app, &cfg).map_err(|e| e.to_string())?.build().map_err(|e| e.to_string())?;
        crate::quiet_window_border(&w);
        crate::webview_memory_low(&w, true);  // a remote control: low memory is fine (0.17.2)
        let _ = w.set_size(LogicalSize::new(width, height));
        *app.state::<MiniState>().through.lock().unwrap() = false;
        constrain(&w, layout, prefs.card);
        let _ = w.show();
    }
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.hide();
        crate::background::schedule_close(&app);
    }
    Ok(())
}

/// ⤢: back to the full window.
#[tauri::command]
pub async fn mini_expand(app: AppHandle) {
    crate::background::show_main(&app);
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.destroy();
    }
    set_through(&app, false);
}

/// What the page draws: its layout and whether clicks go through it.
#[tauri::command]
pub fn mini_state(app: AppHandle) -> serde_json::Value {
    let st = app.state::<MiniState>();
    let prefs = st.prefs();
    let through = *st.through.lock().unwrap();
    let layer = st.layer.lock().unwrap().take();
    serde_json::json!({ "layout": shown(&prefs, through), "chosen": prefs.layout, "through": through, "remember": prefs.remember, "layer": layer })
}

/// Whether the small window lets the mouse through now.
pub fn is_through(app: &AppHandle) -> bool {
    app.get_webview_window(LABEL).is_some() && *app.state::<MiniState>().through.lock().unwrap()
}

/// Puts the small window on a layer from the shell (the page remembers it too).
pub fn force_layer(app: &AppHandle, layer: &str) {
    mini_layer(app.clone(), layer.to_string());
    *app.state::<MiniState>().layer.lock().unwrap() = Some(layer.to_string());
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.emit("mini", serde_json::json!({ "layer": layer }));
    }
}

fn shown(prefs: &Prefs, through: bool) -> Layout {
    if through {
        Layout::Info
    } else if prefs.remember && prefs.layout == Layout::Card {
        Layout::Card
    } else {
        Layout::Bar // also an older choice of «the song only», which is the gaming mode's now
    }
}

/// Only the card resizes (by its corner, mini.js), within its limits; the bar and the gaming
/// strip keep their size.
fn constrain(w: &tauri::WebviewWindow, layout: Layout, card: f64) {
    let (width, height) = layout.size(card);
    if layout == Layout::Card {
        let _ = w.set_resizable(true);
        let _ = w.set_min_size(Some(LogicalSize::new(CARD_MIN, CARD_MIN)));
        let _ = w.set_max_size(Some(LogicalSize::new(CARD_MAX, CARD_MAX)));
    } else {
        let _ = w.set_resizable(false);
        let _ = w.set_min_size(Some(LogicalSize::new(width, height)));
        let _ = w.set_max_size(Some(LogicalSize::new(width, height)));
    }
}

/// The side a resized card settles on: the larger of the two (the corner may move one way
/// more than the other), within its limits.
pub fn square_side(width: f64, height: f64) -> f64 {
    width.max(height).clamp(CARD_MIN, CARD_MAX)
}

/// The card was resized: keep it square and remember its side (half a second after it stops).
pub fn on_resized(window: &tauri::Window) {
    if window.label() != LABEL {
        return;
    }
    let app = window.app_handle().clone();
    let st = app.state::<MiniState>();
    let prefs = st.prefs();
    if *st.popped.lock().unwrap() || shown(&prefs, *st.through.lock().unwrap()) != Layout::Card {
        return;
    }
    let (Ok(size), Ok(scale)) = (window.inner_size(), window.scale_factor()) else { return };
    let (w, h) = (size.width as f64 / scale, size.height as f64 / scale);
    let side = square_side(w, h);
    if (w - side).abs() > 1.0 || (h - side).abs() > 1.0 {
        let _ = window.set_size(LogicalSize::new(side, side));
    }
    let generation = st.resizes.fetch_add(1, Ordering::SeqCst) + 1;
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(500));
        let st = app.state::<MiniState>();
        if st.resizes.load(Ordering::SeqCst) == generation {
            st.update(|p| p.card = side);
        }
    });
}

fn apply(app: &AppHandle) {
    let st = app.state::<MiniState>();
    let prefs = st.prefs();
    let through = *st.through.lock().unwrap();
    let layout = shown(&prefs, through);
    if let Some(w) = app.get_webview_window(LABEL) {
        let (width, height) = layout.size(prefs.card);
        constrain(&w, layout, prefs.card);
        let _ = w.set_size(LogicalSize::new(width, height));
        let _ = w.emit("mini", serde_json::json!({ "layout": layout, "through": through }));
    }
}

/// The shape: a bar or a card (the song only is the gaming mode's, not a choice).
#[tauri::command]
pub fn mini_layout(app: AppHandle, layout: Layout) {
    let layout = if layout == Layout::Card { Layout::Card } else { Layout::Bar };
    app.state::<MiniState>().update(|p| p.layout = layout);
    apply(&app);
}

/// Configuración › General › «Recordar dónde estaba el modo mínimo».
#[tauri::command]
pub fn mini_prefs_set(app: AppHandle, remember: bool) -> serde_json::Value {
    let prefs = app.state::<MiniState>().update(|p| {
        p.remember = remember;
        if !remember {
            (p.x, p.y, p.layout) = (None, None, Layout::Bar);
        }
    });
    serde_json::json!({ "remember": prefs.remember })
}

#[tauri::command]
pub fn mini_prefs_get(app: AppHandle) -> serde_json::Value {
    serde_json::json!({ "remember": app.state::<MiniState>().prefs().remember })
}

/// Room for a menu or the volume (0.13 review: vertical, not over the song): the window grows
/// by `extra` px, down if the screen has room, else up so it stays on screen; 0 puts it back.
/// Returns where it grew: "down" or "up".
#[tauri::command]
pub fn mini_popup(app: AppHandle, extra: f64) -> String {
    let Some(w) = app.get_webview_window(LABEL) else { return "down".into() };
    let st = app.state::<MiniState>();
    let prefs = st.prefs();
    let layout = shown(&prefs, *st.through.lock().unwrap());
    let (width, height) = layout.size(prefs.card);
    let scale = w.scale_factor().unwrap_or(1.0);
    let pos = w.outer_position().map(|p| (p.x as f64 / scale, p.y as f64 / scale)).unwrap_or((0.0, 0.0));
    let mut up = st.popup_up.lock().unwrap();
    if extra <= 0.0 {
        *st.popped.lock().unwrap() = false;
        constrain(&w, layout, prefs.card);
        let _ = w.set_size(LogicalSize::new(width, height));
        if *up > 0.0 {
            let _ = w.set_position(tauri::LogicalPosition::new(pos.0, pos.1 + *up));
        }
        *up = 0.0;
        return "down".into();
    }
    let down = grows_down(pos, height, extra, &work_areas(&app));
    *st.popped.lock().unwrap() = true;
    let _ = w.set_resizable(false);
    let _ = w.set_min_size(None::<LogicalSize<f64>>);
    let _ = w.set_max_size(None::<LogicalSize<f64>>);
    let _ = w.set_size(LogicalSize::new(width, height + extra));
    if down {
        *up = 0.0;
        "down".into()
    } else {
        let _ = w.set_position(tauri::LogicalPosition::new(pos.0, pos.1 - extra));
        *up = extra;
        "up".into()
    }
}

/// The small window moved: remember where (half a second after it stops).
pub fn on_moved(window: &tauri::Window) {
    if window.label() != LABEL {
        return;
    }
    let app = window.app_handle().clone();
    let st = app.state::<MiniState>();
    if !st.prefs().remember {
        return;
    }
    let (Ok(pos), Ok(scale)) = (window.outer_position(), window.scale_factor()) else { return };
    let (x, y) = (pos.x as f64 / scale, pos.y as f64 / scale);
    let generation = st.moves.fetch_add(1, Ordering::SeqCst) + 1;
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(500));
        let st = app.state::<MiniState>();
        if st.moves.load(Ordering::SeqCst) == generation {
            st.update(|p| (p.x, p.y) = (Some(x), Some(y)));
        }
    });
}

/// The layer: "top" (above every window), "normal" or "bottom" (on the
/// desktop, under everything, like a widget).
#[tauri::command]
pub fn mini_layer(app: AppHandle, layer: String) {
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.set_always_on_top(layer == "top");
        let _ = w.set_always_on_bottom(layer == "bottom");
    }
}

/// The tray's «Modo mínimo: usar el mouse», kept to enable it only while
/// the small window lets clicks through.
pub struct MouseItem(pub tauri::menu::MenuItem<tauri::Wry>);

/// Clicks through the small window (a picture only, like a HUD) or not.
/// While they do, it shows the song only (its buttons couldn't be used);
/// the way back is the tray menu, or opening the full window.
pub fn set_through(app: &AppHandle, on: bool) {
    let Some(w) = app.get_webview_window(LABEL) else {
        if let Some(item) = app.try_state::<MouseItem>() {
            let _ = item.0.set_enabled(false);
        }
        return;
    };
    *app.state::<MiniState>().through.lock().unwrap() = on;
    let _ = w.set_ignore_cursor_events(on);
    apply(app);
    if let Some(item) = app.try_state::<MouseItem>() {
        let _ = item.0.set_enabled(on);
    }
}

#[tauri::command]
pub fn mini_through(app: AppHandle, on: bool) {
    set_through(&app, on);
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCREENS: [(f64, f64, f64, f64); 2] = [(0.0, 0.0, 1920.0, 1040.0), (1920.0, -400.0, 1080.0, 1880.0)];

    #[test]
    fn a_remembered_place_on_a_screen_stays() {
        assert_eq!(clamp_to_monitors(100.0, 200.0, 500.0, 72.0, &SCREENS), Some((100.0, 200.0)));
        assert_eq!(clamp_to_monitors(2000.0, -300.0, 248.0, 384.0, &SCREENS), Some((2000.0, -300.0)));
    }

    #[test]
    fn half_out_of_the_screen_is_pushed_back_in() {
        assert_eq!(clamp_to_monitors(1700.0, 990.0, 340.0, 80.0, &SCREENS), Some((1580.0, 960.0)));
    }

    #[test]
    fn a_monitor_that_is_gone_means_the_default_place() {
        assert_eq!(clamp_to_monitors(5000.0, 100.0, 500.0, 72.0, &SCREENS), None);
        assert_eq!(clamp_to_monitors(100.0, 100.0, 500.0, 72.0, &[]), None);
    }

    #[test]
    fn the_default_place_is_bottom_right() {
        assert_eq!(bottom_right(SCREENS[0], 500.0, 72.0), (1396.0, 944.0));
        assert_eq!(bottom_right(SCREENS[1], 224.0, 224.0), (2752.0, 1232.0));
    }

    #[test]
    fn a_menu_grows_down_unless_the_screen_ends() {
        assert!(grows_down((100.0, 100.0), 72.0, 300.0, &SCREENS));
        assert!(grows_down((100.0, 668.0), 72.0, 300.0, &SCREENS)); // exactly to the bottom
        assert!(!grows_down((100.0, 900.0), 72.0, 300.0, &SCREENS));
        assert!(grows_down((2000.0, 900.0), 72.0, 300.0, &SCREENS)); // the tall screen on the right
        assert!(grows_down((5000.0, 5000.0), 72.0, 300.0, &SCREENS)); // on no known screen
    }

    #[test]
    fn what_is_shown() {
        let mut p = Prefs { layout: Layout::Card, ..Prefs::default() };
        assert_eq!(shown(&p, false), Layout::Card);
        assert_eq!(shown(&p, true), Layout::Info); // clicks go through: the song only
        p.remember = false;
        assert_eq!(shown(&p, false), Layout::Bar);
        assert_eq!(Layout::Info.size(CARD_DEFAULT), (340.0, 56.0));
        // «the song only» chosen in an older version opens as the bar (0.24)
        let old = Prefs { layout: Layout::Info, ..Prefs::default() };
        assert_eq!(shown(&old, false), Layout::Bar);
    }

    #[test]
    fn prefs_round_trip() {
        let p = Prefs { remember: true, x: Some(-1200.5), y: Some(30.0), layout: Layout::Info, card: 300.0 };
        let text = serde_json::to_string(&p).unwrap();
        assert!(text.contains(r#""layout":"info""#));
        let back: Prefs = serde_json::from_str(&text).unwrap();
        assert_eq!((back.x, back.y, back.layout), (Some(-1200.5), Some(30.0), Layout::Info));
        assert!(serde_json::from_str::<Prefs>(r#"{"layout": "huge"}"#).is_err());
    }

    #[test]
    fn the_card_stays_square_within_its_limits() {
        assert_eq!(Layout::Card.size(300.0), (300.0, 300.0));
        assert_eq!(Layout::Card.size(90.0), (CARD_MIN, CARD_MIN));
        assert_eq!(Layout::Card.size(900.0), (CARD_MAX, CARD_MAX));
        assert_eq!(Layout::Bar.size(300.0), (420.0, 64.0));
        assert_eq!(square_side(260.0, 230.0), 260.0);
        assert_eq!(square_side(100.0, 120.0), CARD_MIN);
        assert_eq!(square_side(500.0, 300.0), CARD_MAX);
    }

    #[test]
    fn old_or_partial_prefs_files_still_load() {
        let p: Prefs = serde_json::from_str(r#"{"x": 10}"#).unwrap();
        assert!(p.remember && p.layout == Layout::Bar && p.x == Some(10.0) && p.y.is_none());
        let p: Prefs = serde_json::from_str(r#"{"layout": "card", "remember": false}"#).unwrap();
        assert!(!p.remember && p.layout == Layout::Card);
        assert_eq!(p.card, CARD_DEFAULT); // files from before 0.24 have no card size
    }
}
