//! Rotating backgrounds, per monitor, run by the shell (PLAN 0.13.2): it works
//! with the window closed and the engine asleep. The scheduler (scheduler.rs)
//! calls `tick` every 30 s; a monitor whose time has come gets the next photo
//! of its source.
//!
//! `BakinData/wallpapers.json`: `{paused, monitors: {<key>: Rot}}`. The key is
//! the monitor's size (`1920x1080`, `1080x1920`, a `#2` for a second one of
//! the same size): Windows' device ids change when a monitor is reconnected.
//!
//! Sources are resolved here, without the engine: a folder (any collection's
//! root: Wallhaven's, a preset's, the user's own), or the favourites of every
//! collection, read from their meta files (the engine says where they are
//! when the page saves the rotation, `wallpaper_sources`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Manager};

use crate::wallpaper::{self, Monitor};

/// How many past photos a monitor remembers, so they don't come back soon.
const HISTORY: usize = 50;
/// A folder is listed again after this long (a big one costs a walk).
const LIST_FOR: Duration = Duration::from_secs(600);

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct MetaRef {
    pub root: String,
    pub meta: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Source {
    /// "folder" | "favorites"
    pub kind: String,
    pub label: String,
    #[serde(default)]
    pub folders: Vec<String>,
    #[serde(default)]
    pub metas: Vec<MetaRef>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Rot {
    pub on: bool,
    pub source: Source,
    /// minutes between changes
    pub every_min: u64,
    /// "random" | "ordered"
    pub order: String,
    /// only photos of the monitor's orientation
    pub fit: bool,
    /// light photos by day, dark ones at night (PLAN 0.13.5)
    #[serde(default)]
    pub daynight: bool,
    /// the photo whose colour is closest to the cover playing now
    #[serde(default)]
    pub cover: bool,
    #[serde(default)]
    pub last_at: f64,
    #[serde(default)]
    pub history: Vec<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub paused: bool,
    #[serde(default)]
    pub monitors: BTreeMap<String, Rot>,
    /// the photo each monitor shows now, by its number (rotated or set by hand)
    #[serde(default)]
    pub shown: BTreeMap<String, String>,
    /// where the user framed a photo for a monitor shape: "WxH|path" → [x, y] in 0..1
    #[serde(default)]
    pub focus: BTreeMap<String, [f64; 2]>,
}

#[derive(Default)]
pub struct Rotation {
    /// one change at a time (the scheduler, the tray, the page)
    busy: Mutex<()>,
    lists: Mutex<HashMap<String, (Instant, Vec<String>)>>,
    sizes: Mutex<HashMap<String, (u32, u32)>>,
}

fn config_path() -> PathBuf {
    crate::player::data_dir().join("wallpapers.json")
}

pub fn load() -> Config {
    std::fs::read_to_string(config_path()).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

fn save(cfg: &Config) -> Result<(), String> {
    let p = config_path();
    let _ = std::fs::create_dir_all(p.parent().unwrap_or(Path::new(".")));
    let tmp = p.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(cfg).unwrap_or_default()).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &p).map_err(|e| e.to_string())
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Each monitor's key: its size, numbered when two share it.
pub fn keys(monitors: &[Monitor]) -> Vec<String> {
    let mut seen: HashMap<(i32, i32), usize> = HashMap::new();
    monitors
        .iter()
        .map(|m| {
            let n = seen.entry((m.w, m.h)).or_insert(0);
            *n += 1;
            if *n == 1 { format!("{}x{}", m.w, m.h) } else { format!("{}x{}#{}", m.w, m.h, n) }
        })
        .collect()
}

/// Whether any monitor rotates (Bakin then stays in the tray with the window closed).
pub fn any_on() -> bool {
    let cfg = load();
    !cfg.paused && cfg.monitors.values().any(|r| r.on)
}

/// A photo of `w`×`h` suits a monitor: same orientation (a square one suits both).
pub fn fits(w: u32, h: u32, vertical: bool) -> bool {
    let (w, h) = (w as f64, h as f64);
    if (w - h).abs() <= w.max(h) * 0.05 {
        return true;
    }
    (h > w) == vertical
}

/// A small random number generator: no crate for picking a photo.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
}

/// The next photo among `candidates` (sorted): in order after the last one,
/// or at random without repeating the recent ones until they run out; never
/// the one shown now if there is another. `suits` is asked lazily (it reads
/// the file's header), so a big folder is not read whole.
pub fn pick_next(candidates: &[String], history: &[String], ordered: bool, mut suits: impl FnMut(&str) -> bool, seed: u64) -> Option<String> {
    if candidates.is_empty() {
        return None;
    }
    let current = history.last().map(String::as_str);
    if ordered {
        let start = current.and_then(|c| candidates.iter().position(|x| x == c)).map(|i| i + 1).unwrap_or(0);
        let n = candidates.len();
        return (0..n).map(|k| &candidates[(start + k) % n]).find(|p| Some(p.as_str()) != current && suits(p)).cloned();
    }
    let mut rng = Rng(seed | 1);
    let mut order: Vec<usize> = (0..candidates.len()).collect();
    for i in (1..order.len()).rev() {
        order.swap(i, (rng.next() % (i as u64 + 1)) as usize);
    }
    let recent: HashSet<&str> = history.iter().map(String::as_str).collect();
    let mut asked: HashMap<usize, bool> = HashMap::new();
    let mut ok = |i: usize| *asked.entry(i).or_insert_with(|| suits(&candidates[i]));
    if let Some(&i) = order.iter().find(|&&i| !recent.contains(candidates[i].as_str()) && ok(i)) {
        return Some(candidates[i].clone());
    }
    // all were shown lately: start over, still not the current one
    order.iter().find(|&&i| Some(candidates[i].as_str()) != current && ok(i)).map(|&i| candidates[i].clone())
}

type Rgb = (u8, u8, u8);

fn hex_rgb(h: &str) -> Option<Rgb> {
    let h = h.trim_start_matches('#');
    if h.len() != 6 {
        return None;
    }
    let n = u32::from_str_radix(h, 16).ok()?;
    Some(((n >> 16) as u8, (n >> 8) as u8, n as u8))
}

/// Perceived lightness, 0 to 1.
fn lum((r, g, b): Rgb) -> f64 {
    (0.2126 * r as f64 + 0.7152 * g as f64 + 0.0722 * b as f64) / 255.0
}

fn dist2(a: Rgb, b: Rgb) -> u32 {
    let d = |x: u8, y: u8| (x as i32 - y as i32).pow(2) as u32;
    d(a.0, b.0) + d(a.1, b.1) + d(a.2, b.2)
}

/// The candidates that match the colour wishes (PLAN 0.13.5): light or dark
/// ones, and/or the closest to a colour (the nearest fifth, at least five).
/// Only photos whose colour is known count; empty when none match, and the
/// caller then takes from all, so a rotation never gets stuck.
pub fn prefer(candidates: &[String], color_of: impl Fn(&str) -> Option<Rgb>, light: Option<bool>, near: Option<Rgb>) -> Vec<String> {
    let mut known: Vec<(&String, Rgb)> = candidates.iter().filter_map(|p| color_of(p).map(|c| (p, c))).collect();
    if let Some(light) = light {
        known.retain(|(_, c)| (lum(*c) >= 0.5) == light);
    }
    if let Some(target) = near {
        known.sort_by_key(|(_, c)| dist2(*c, target));
        let keep = (known.len() / 5).max(5);
        known.truncate(keep);
        known.sort_by(|a, b| a.0.cmp(b.0)); // back to the folder's order, for «en orden»
    }
    known.into_iter().map(|(p, _)| p.clone()).collect()
}

/// The dominant colours the engine worked out (bakin/imageinfo.py), by
/// "path|size|mtime": read here so the engine can stay asleep.
fn known_colors() -> HashMap<String, Rgb> {
    let Ok(text) = std::fs::read_to_string(crate::player::data_dir().join("imageinfo.json")) else { return HashMap::new() };
    let Ok(data) = serde_json::from_str::<Value>(&text) else { return HashMap::new() };
    let Some(items) = data.get("items").and_then(Value::as_object) else { return HashMap::new() };
    items.iter().filter_map(|(k, v)| Some((k.clone(), hex_rgb(v.get("c")?.as_str()?)?))).collect()
}

fn color_key(path: &str) -> Option<String> {
    let meta = std::fs::metadata(path).ok()?;
    let mtime = meta.modified().ok()?.duration_since(UNIX_EPOCH).ok()?.as_secs();
    Some(format!("{path}|{}|{mtime}", meta.len()))
}

/// The average colour of the cover playing now (from the cover cache).
fn cover_color(app: &AppHandle) -> Option<Rgb> {
    let st = app.try_state::<crate::player::Player>()?.call(json!({ "cmd": "state" })).ok()?;
    if st.get("playing").and_then(Value::as_bool) != Some(true) {
        return None;
    }
    let path = st.get("track")?.get("path")?.as_str()?.to_string();
    let jpeg = crate::covers::cached(&path)?;
    let px = image::load_from_memory(&jpeg).ok()?.thumbnail(1, 1).to_rgb8();
    let p = px.get_pixel(0, 0);
    Some((p[0], p[1], p[2]))
}

/// Day for the «light by day» wish: 7:00 to 19:00, local time.
fn daytime() -> bool {
    #[cfg(windows)]
    {
        let t = unsafe { windows::Win32::System::SystemInformation::GetLocalTime() };
        (7..19).contains(&t.wHour)
    }
    #[cfg(not(windows))]
    true
}

const SKIP_DIRS: [&str; 3] = [".partial", "$recycle.bin", "system volume information"];

/// Every image under `root` (sorted), skipping hidden and system folders.
pub fn walk(root: &Path) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for e in entries.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().to_lowercase();
            if name.starts_with('.') || SKIP_DIRS.contains(&name.as_str()) {
                continue;
            }
            match e.file_type() {
                Ok(t) if t.is_dir() => stack.push(p),
                Ok(t) if t.is_file() && wallpaper::is_image(&p) => out.push(p.to_string_lossy().into_owned()),
                _ => {}
            }
        }
    }
    out.sort();
    out
}

/// The favourite images of the collections whose meta files are given
/// (the `items` of library.Meta: `{rel: {"fav": true, …}}`).
pub fn favorites(metas: &[MetaRef]) -> Vec<String> {
    let mut out = Vec::new();
    for m in metas {
        let Ok(text) = std::fs::read_to_string(&m.meta) else { continue };
        let Ok(data) = serde_json::from_str::<Value>(&text) else { continue };
        let Some(items) = data.get("items").and_then(Value::as_object) else { continue };
        for (rel, v) in items {
            if v.get("fav").and_then(Value::as_bool) == Some(true) {
                let p = Path::new(&m.root).join(rel);
                if wallpaper::is_image(&p) && p.is_file() {
                    out.push(p.to_string_lossy().into_owned());
                }
            }
        }
    }
    out.sort();
    out
}

impl Rotation {
    fn candidates(&self, src: &Source) -> Vec<String> {
        if src.kind == "favorites" {
            return favorites(&src.metas);
        }
        let mut all = Vec::new();
        // the shell lives in the tray for days: lists past their time go, not just get skipped
        self.lists.lock().unwrap().retain(|_, (at, _)| at.elapsed() < LIST_FOR);
        for f in &src.folders {
            let mut lists = self.lists.lock().unwrap();
            match lists.get(f) {
                Some((at, list)) if at.elapsed() < LIST_FOR => all.extend(list.iter().cloned()),
                _ => {
                    let list = walk(Path::new(f));
                    all.extend(list.iter().cloned());
                    lists.insert(f.clone(), (Instant::now(), list));
                }
            }
        }
        all.sort();
        all.dedup();
        all
    }

    fn size(&self, path: &str) -> Option<(u32, u32)> {
        if let Some(s) = self.sizes.lock().unwrap().get(path) {
            return Some(*s);
        }
        let d = imagesize::size(path).ok()?; // the header only
        let s = (d.width as u32, d.height as u32);
        let mut sizes = self.sizes.lock().unwrap();
        if sizes.len() >= 4096 {
            sizes.clear(); // a header read is cheap: no need to keep every photo ever shown
        }
        sizes.insert(path.to_string(), s);
        Some(s)
    }

    /// Changes the monitors whose time has come (or `only` / every rotating one
    /// when `force`). How many changed.
    pub fn advance(&self, app: &AppHandle, only: Option<&str>, force: bool) -> Result<usize, String> {
        let _one = self.busy.lock().unwrap();
        let mut cfg = load();
        if cfg.paused && !force {
            return Ok(0);
        }
        // the colour wishes need the colours, the hour and the cover: only if someone asks
        let wishes = cfg.monitors.values().any(|r| r.on && (r.daynight || r.cover));
        let colors = if wishes { known_colors() } else { HashMap::new() };
        let cover = if cfg.monitors.values().any(|r| r.on && r.cover) { cover_color(app) } else { None };
        let day = daytime();
        let monitors = wallpaper::monitors()?;
        let keys = keys(&monitors);
        let t = now();
        let mut changed = 0;
        for (m, key) in monitors.iter().zip(&keys) {
            if only.is_some_and(|k| k != key) {
                continue;
            }
            let Some(rot) = cfg.monitors.get_mut(key) else { continue };
            if !rot.on || (!force && t < rot.last_at + rot.every_min.max(1) as f64 * 60.0 - 1.0) {
                continue;
            }
            let mut list = self.candidates(&rot.source);
            if rot.daynight || (rot.cover && cover.is_some()) {
                let light = rot.daynight.then_some(day);
                let near = if rot.cover { cover } else { None };
                let wished = prefer(&list, |p| color_key(p).and_then(|k| colors.get(&k).copied()), light, near);
                if !wished.is_empty() {
                    list = wished;
                }
            }
            let fit = rot.fit;
            let seed = (t * 1000.0) as u64 ^ (m.number as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
            let pick = pick_next(&list, &rot.history, rot.order == "ordered", |p| !fit || self.size(p).is_none_or(|(w, h)| fits(w, h, m.vertical)), seed);
            rot.last_at = t; // also when there is nothing to show: try again next time, not every 30 s
            let Some(path) = pick else { continue };
            wallpaper::apply(&[m], &path)?;
            cfg.shown.insert(m.number.to_string(), path.clone());
            let rot = cfg.monitors.get_mut(key).expect("still there");
            rot.history.push(path);
            let n = rot.history.len();
            if n > HISTORY {
                rot.history.drain(..n - HISTORY);
            }
            changed += 1;
        }
        save(&cfg)?;
        Ok(changed)
    }
}

/// «Poner de fondo» by hand: remember what each of these monitors shows.
pub fn note_shown(app: &AppHandle, numbers: &[usize], path: &str) {
    let Some(rotation) = app.try_state::<Rotation>() else { return };
    let _one = rotation.busy.lock().unwrap();
    let mut cfg = load();
    for n in numbers {
        cfg.shown.insert(n.to_string(), path.to_string());
    }
    let _ = save(&cfg);
}

/// «Ajustar encuadre»: how a photo is framed for each monitor shape it was framed for.
#[tauri::command]
pub fn wallpaper_focus_get(path: String) -> BTreeMap<String, [f64; 2]> {
    let suffix = format!("|{path}");
    load().focus.into_iter().filter(|(k, _)| k.ends_with(&suffix)).map(|(k, v)| (k[..k.len() - suffix.len()].to_string(), v)).collect()
}

/// Keeps where the user framed `path` for a `w`×`h` monitor (the centre: forget it).
#[tauri::command]
pub fn wallpaper_focus_set(app: AppHandle, path: String, w: i32, h: i32, x: f64, y: f64) -> Result<(), String> {
    let rotation = app.state::<Rotation>();
    let _one = rotation.busy.lock().unwrap();
    let mut cfg = load();
    let key = wallpaper::focus_key(&path, w, h);
    let (x, y) = (x.clamp(0.0, 1.0), y.clamp(0.0, 1.0));
    if (x - 0.5).abs() < 0.005 && (y - 0.5).abs() < 0.005 {
        cfg.focus.remove(&key);
    } else {
        cfg.focus.insert(key, [x, y]);
    }
    save(&cfg)
}

/// What each monitor shows now (by number), for the badge on its photo.
#[tauri::command]
pub fn wallpaper_shown() -> BTreeMap<String, String> {
    load().shown
}

/// The scheduler's call, every 30 s.
pub fn tick(app: &AppHandle) {
    if let Some(r) = app.try_state::<Rotation>() {
        let _ = r.advance(app, None, false);
    }
}

/// For Configuración › Fondos: each monitor with its rotation.
#[tauri::command]
pub async fn wallpaper_rotation_get() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let monitors = wallpaper::monitors()?;
        let cfg = load();
        let rows: Vec<Value> = monitors
            .iter()
            .zip(keys(&monitors))
            .map(|(m, key)| {
                let rot = cfg.monitors.get(&key);
                json!({ "number": m.number, "w": m.w, "h": m.h, "vertical": m.vertical, "key": key, "rot": rot,
                        "current": rot.and_then(|r| r.history.last()) })
            })
            .collect();
        Ok(json!({ "paused": cfg.paused, "monitors": rows, "dry": wallpaper::dry() }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Saves one monitor's rotation; turning it on (or changing where it takes
/// photos from) shows the first one right away.
#[tauri::command]
pub async fn wallpaper_rotation_set(app: AppHandle, key: String, rot: Rot) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let rotation = app.state::<Rotation>();
        let now_on;
        {
            let _one = rotation.busy.lock().unwrap();
            let mut cfg = load();
            let old = cfg.monitors.get(&key).cloned();
            let mut rot = Rot { every_min: rot.every_min.clamp(1, 7 * 24 * 60), ..rot };
            let restart = rot.on && old.as_ref().is_none_or(|o| !o.on || o.source != rot.source);
            if let Some(o) = &old {
                rot.history = o.history.clone();
                rot.last_at = o.last_at;
            }
            now_on = restart;
            cfg.monitors.insert(key.clone(), rot);
            save(&cfg)?;
        }
        let changed = if now_on { rotation.advance(&app, Some(&key), true)? } else { 0 };
        crate::background::sync_wallpaper_tray(&app);
        Ok(json!({ "ok": true, "changed": changed }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// «Siguiente fondo»: one monitor (its key) or every rotating one, now.
#[tauri::command]
pub async fn wallpaper_next(app: AppHandle, key: Option<String>) -> Result<usize, String> {
    tauri::async_runtime::spawn_blocking(move || app.state::<Rotation>().advance(&app, key.as_deref(), true))
        .await
        .map_err(|e| e.to_string())?
}

/// Pause or resume every rotation (the tray has the same switch).
#[tauri::command]
pub fn wallpaper_rotation_pause(app: AppHandle, paused: bool) -> Result<(), String> {
    set_paused(&app, paused)
}

pub fn set_paused(app: &AppHandle, paused: bool) -> Result<(), String> {
    let rotation = app.state::<Rotation>();
    let _one = rotation.busy.lock().unwrap();
    let mut cfg = load();
    cfg.paused = paused;
    save(&cfg)?;
    crate::background::sync_wallpaper_tray(app);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn list(n: usize) -> Vec<String> {
        (0..n).map(|i| format!("f{i:02}.jpg")).collect()
    }

    #[test]
    fn in_order_it_goes_on_after_the_last_one_and_wraps() {
        let c = list(3);
        assert_eq!(pick_next(&c, &[], true, |_| true, 1).as_deref(), Some("f00.jpg"));
        assert_eq!(pick_next(&c, &["f01.jpg".into()], true, |_| true, 1).as_deref(), Some("f02.jpg"));
        assert_eq!(pick_next(&c, &["f02.jpg".into()], true, |_| true, 1).as_deref(), Some("f00.jpg"));
        // skipping the ones that don't suit the monitor
        assert_eq!(pick_next(&c, &["f00.jpg".into()], true, |p| p != "f01.jpg", 1).as_deref(), Some("f02.jpg"));
    }

    #[test]
    fn at_random_it_does_not_repeat_until_they_run_out() {
        let c = list(10);
        let mut history: Vec<String> = Vec::new();
        for seed in 1..=10u64 {
            let p = pick_next(&c, &history, false, |_| true, seed * 7919).unwrap();
            assert!(!history.contains(&p), "{p} repeated");
            history.push(p);
        }
        // all shown: it starts over, but never with the one on screen
        for seed in 1..50u64 {
            assert_ne!(pick_next(&c, &history, false, |_| true, seed).as_deref(), history.last().map(String::as_str));
        }
    }

    #[test]
    fn nothing_to_show_gives_none() {
        assert_eq!(pick_next(&[], &[], false, |_| true, 3), None);
        assert_eq!(pick_next(&list(4), &[], false, |_| false, 3), None);
        // only the current one suits: stays
        assert_eq!(pick_next(&list(1), &["f00.jpg".into()], true, |_| true, 3), None);
    }

    #[test]
    fn pick_prefers_colour() {
        let c: Vec<String> = ["dark1", "dark2", "light1", "red1", "unknown"].iter().map(|s| s.to_string()).collect();
        let col = |p: &str| match p {
            "dark1" => Some((0x00, 0x00, 0x00)),
            "dark2" => Some((0x42, 0x41, 0x53)),
            "light1" => Some((0xff, 0xff, 0xff)),
            "red1" => Some((0xcc, 0x00, 0x00)),
            _ => None,
        };
        assert_eq!(prefer(&c, col, Some(true), None), vec!["light1"]);
        assert_eq!(prefer(&c, col, Some(false), None), vec!["dark1", "dark2", "red1"]);
        // near a red cover, red first (the nearest five here: all known ones)
        let near: Vec<String> = (0..20).map(|i| format!("p{i:02}")).collect();
        let col2 = |p: &str| -> Option<Rgb> { let i: u8 = p[1..].parse().ok()?; Some((i * 12, 0, 255 - i * 12)) };
        let got = prefer(&near, col2, None, Some((255, 0, 0)));
        assert_eq!(got.len(), 5);
        assert!(got.iter().all(|p| p.as_str() >= "p15"), "{got:?}");
        assert!(prefer(&c, |_| None, Some(true), None).is_empty()); // nothing known: the caller takes from all
    }

    #[test]
    fn orientation_and_keys() {
        assert!(fits(1920, 1080, false) && !fits(1920, 1080, true));
        assert!(fits(1080, 1920, true) && fits(1000, 1020, true) && fits(1000, 1020, false));
        let m = |n, w, h| Monitor { number: n, id: String::new(), w, h, vertical: h > w };
        assert_eq!(keys(&[m(1, 1920, 1080), m(2, 1080, 1920), m(3, 1920, 1080)]), vec!["1920x1080", "1080x1920", "1920x1080#2"]);
    }

    #[test]
    fn favourites_come_from_the_meta_files() {
        let dir = std::env::temp_dir().join(format!("bakin-rot-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        for f in ["a.jpg", "sub/b.png", "c.jpg", "d.txt"] {
            std::fs::write(dir.join(f), b"x").unwrap();
        }
        std::fs::create_dir_all(dir.join(".partial")).unwrap();
        std::fs::write(dir.join(".partial/e.jpg"), b"x").unwrap();
        let meta = dir.join("meta.json");
        std::fs::write(&meta, r#"{"items": {"a.jpg": {"fav": true}, "sub/b.png": {"fav": true}, "c.jpg": {"tags": ["x"]}, "gone.jpg": {"fav": true}}}"#).unwrap();
        let favs = favorites(&[MetaRef { root: dir.to_string_lossy().into(), meta: meta.to_string_lossy().into() }]);
        assert_eq!(favs.len(), 2);
        let all = walk(&dir);
        assert_eq!(all.len(), 3, "{all:?}"); // a, sub/b, c: not the .txt nor .partial
        let _ = std::fs::remove_dir_all(&dir);
    }
}
