//! «Poner de fondo»: a photo as the desktop background of one monitor, or of
//! all of them (IDesktopWallpaper, PLAN 0.13.1).
//!
//! Monitors are numbered with the main one first (Windows puts it at 0,0),
//! then the rest left to right, as the user sees them; their ids change when one is reconnected, so they are looked up again
//! on every call, never cached.
//!
//! Tests never touch the real desktop: with BAKIN_DRY_WALLPAPER=1 nothing is
//! set and each request is appended to `BakinData/wallpaper-dry.json`;
//! BAKIN_DRY_MONITORS ("1920x1080,1080x1920") stands in for the monitors.

use serde::Serialize;
use serde_json::{json, Value};

/// One monitor, as the page shows it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Monitor {
    /// 1-based: the main monitor, then left to right.
    pub number: usize,
    #[serde(skip)]
    pub id: String,
    pub w: i32,
    pub h: i32,
    pub vertical: bool,
}

/// Monitors from (device id, left, top, right, bottom), without the
/// disconnected ones (empty rectangle): the main one first, then left to right.
pub fn describe(rects: Vec<(String, i32, i32, i32, i32)>) -> Vec<Monitor> {
    let mut live: Vec<_> = rects.into_iter().filter(|r| r.3 > r.1 && r.4 > r.2).collect();
    live.sort_by_key(|r| (!(r.1 <= 0 && r.2 <= 0 && r.3 > 0 && r.4 > 0), r.1, r.2));
    live.into_iter()
        .enumerate()
        .map(|(i, (id, l, t, r, b))| Monitor { number: i + 1, id, w: r - l, h: b - t, vertical: b - t > r - l })
        .collect()
}

pub const IMAGES: [&str; 5] = ["jpg", "jpeg", "png", "bmp", "webp"];

/// Whether Windows can use this file as a background (by its extension).
pub fn is_image(path: &std::path::Path) -> bool {
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
    IMAGES.contains(&ext.as_str())
}

pub fn dry() -> bool {
    std::env::var("BAKIN_DRY_WALLPAPER").is_ok_and(|v| v == "1")
}

fn dry_monitors() -> Option<Vec<Monitor>> {
    let spec = std::env::var("BAKIN_DRY_MONITORS").ok()?;
    let mut x = 0;
    let rects = spec
        .split(',')
        .enumerate()
        .filter_map(|(i, m)| {
            let (w, h) = m.trim().split_once('x')?;
            let (w, h) = (w.parse::<i32>().ok()?, h.parse::<i32>().ok()?);
            let r = (format!("dry-{i}"), x, 0, x + w, h);
            x += w;
            Some(r)
        })
        .collect();
    Some(describe(rects))
}

#[cfg(windows)]
mod com {
    use windows::core::HSTRING;
    use windows::Win32::System::Com::{CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_ALL, COINIT_APARTMENTTHREADED};
    use windows::Win32::UI::Shell::{DesktopWallpaper, IDesktopWallpaper, DWPOS_FILL};

    /// Runs `f` with the desktop wallpaper object, on a thread of its own (COM, STA).
    fn with<T: Send + 'static>(f: impl FnOnce(&IDesktopWallpaper) -> windows::core::Result<T> + Send + 'static) -> Result<T, String> {
        std::thread::spawn(move || unsafe {
            let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
            let out = CoCreateInstance::<_, IDesktopWallpaper>(&DesktopWallpaper, None, CLSCTX_ALL).and_then(|dw| f(&dw));
            CoUninitialize();
            out
        })
        .join()
        .map_err(|_| "no se pudo hablar con Windows".to_string())?
        .map_err(|e| format!("Windows no aceptó el fondo: {}", e.message()))
    }

    pub fn rects() -> Result<Vec<(String, i32, i32, i32, i32)>, String> {
        with(|dw| unsafe {
            let mut out = Vec::new();
            for i in 0..dw.GetMonitorDevicePathCount()? {
                let p = dw.GetMonitorDevicePathAt(i)?;
                let id = p.to_string().unwrap_or_default();
                CoTaskMemFree(Some(p.0 as _));
                // a disconnected monitor has no rectangle: skipped by describe()
                let r = dw.GetMonitorRECT(&HSTRING::from(id.as_str())).unwrap_or_default();
                out.push((id, r.left, r.top, r.right, r.bottom));
            }
            Ok(out)
        })
    }

    /// (monitor id, file) pairs: each monitor its own framed copy.
    pub fn set(pairs: Vec<(String, String)>) -> Result<(), String> {
        with(move |dw| unsafe {
            for (id, file) in &pairs {
                dw.SetWallpaper(&HSTRING::from(id.as_str()), &HSTRING::from(file.as_str()))?;
            }
            // one position for every monitor (Windows keeps only one): fill, never stretch
            dw.SetPosition(DWPOS_FILL)
        })
    }
}

pub fn monitors() -> Result<Vec<Monitor>, String> {
    if dry() {
        if let Some(m) = dry_monitors() {
            return Ok(m);
        }
    }
    #[cfg(windows)]
    return com::rects().map(describe);
    #[cfg(not(windows))]
    Err("Solo en Windows.".into())
}

/// The monitors, and whether this is a test copy that never changes the desktop.
#[tauri::command]
pub async fn wallpaper_monitors() -> Result<Value, String> {
    let (m, lock) = tauri::async_runtime::spawn_blocking(|| monitors().map(|m| (m, lockscreen_supported()))).await.map_err(|e| e.to_string())??;
    Ok(json!({ "monitors": m, "dry": dry(), "lock": lock }))
}

/// Puts `path` as the background of monitor `number` (1-based), or of all.
#[tauri::command]
pub async fn wallpaper_set(app: tauri::AppHandle, path: String, number: Option<usize>) -> Result<(), String> {
    let file = std::path::Path::new(&path);
    if !is_image(file) || !file.is_file() {
        return Err("Eso no es una imagen que Windows pueda poner de fondo.".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let all = monitors()?;
        let chosen: Vec<&Monitor> = match number {
            Some(n) => all.iter().filter(|m| m.number == n).collect(),
            None => all.iter().collect(),
        };
        if chosen.is_empty() {
            return Err("Ese monitor ya no está conectado.".to_string());
        }
        apply(&chosen, &path)?;
        crate::rotation::note_shown(&app, &chosen.iter().map(|m| m.number).collect::<Vec<_>>(), &path);
        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Puts `path` on these monitors (or, in a test copy, writes down that it would).
/// Each gets its own copy framed to its size (`framed`); the original if that fails.
pub fn apply(chosen: &[&Monitor], path: &str) -> Result<(), String> {
    let focus = crate::rotation::load().focus;
    let shown: Vec<(&Monitor, String)> = chosen
        .iter()
        .map(|m| {
            let f = focus.get(&focus_key(path, m.w, m.h)).map(|f| (f[0], f[1])).unwrap_or((0.5, 0.5));
            (*m, framed(path, m.w, m.h, f).unwrap_or_else(|| path.to_string()))
        })
        .collect();
    if dry() {
        let log = crate::player::data_dir().join("wallpaper-dry.json");
        let mut done: Vec<Value> = std::fs::read_to_string(&log).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
        done.extend(shown.iter().map(|(m, file)| json!({ "monitor": m.number, "w": m.w, "h": m.h, "path": path, "shown": file })));
        return std::fs::write(&log, serde_json::to_string_pretty(&done).unwrap_or_default()).map_err(|e| e.to_string());
    }
    #[cfg(windows)]
    return com::set(shown.into_iter().map(|(m, file)| (m.id.clone(), file)).collect());
    #[cfg(not(windows))]
    Err("Solo en Windows.".into())
}

/// The part of a `sw`×`sh` photo that fills a `mw`×`mh` monitor: the
/// biggest centred rectangle of the monitor's shape.
#[cfg(test)]
pub fn crop_rect(sw: u32, sh: u32, mw: u32, mh: u32) -> (u32, u32, u32, u32) {
    crop_rect_at(sw, sh, mw, mh, (0.5, 0.5))
}

/// The same rectangle, placed where the user framed it: `focus` 0..1 along
/// the side that is cut (0 = left / top, 0.5 = centre, 1 = right / bottom).
pub fn crop_rect_at(sw: u32, sh: u32, mw: u32, mh: u32, focus: (f64, f64)) -> (u32, u32, u32, u32) {
    let (sw64, sh64, mw64, mh64) = (sw as u64, sh as u64, mw.max(1) as u64, mh.max(1) as u64);
    let at = |room: u32, f: f64| ((room as f64) * f.clamp(0.0, 1.0)).floor() as u32;
    if sw64 * mh64 > sh64 * mw64 {
        // wider than the monitor: full height, cut the sides
        let w = ((sh64 * mw64 + mh64 / 2) / mh64).clamp(1, sw64) as u32;
        (at(sw - w, focus.0), 0, w, sh)
    } else {
        let h = ((sw64 * mh64 + mw64 / 2) / mw64).clamp(1, sh64) as u32;
        (0, at(sh - h, focus.1), sw, h)
    }
}

/// Where a photo's framing for a monitor shape is kept (wallpapers.json `focus`).
pub fn focus_key(path: &str, mw: i32, mh: i32) -> String {
    format!("{mw}x{mh}|{path}")
}

/// How many framed copies are kept (the newest; the rest are deleted).
const FRAMED_KEEP: usize = 40;

/// A copy of the photo framed to one monitor (PLAN 0.13.7): the centred part
/// of the monitor's shape, made smaller to its exact size with a good filter
/// (never bigger), saved as PNG — Windows re-compresses JPEG backgrounds, not
/// PNG ones. Kept in `BakinData/wallpapers/cache`; made once per photo and size.
pub fn framed(path: &str, mw: i32, mh: i32, focus: (f64, f64)) -> Option<String> {
    let (mw, mh) = (u32::try_from(mw).ok()?, u32::try_from(mh).ok()?);
    let meta = std::fs::metadata(path).ok()?;
    let mtime = meta.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
    let at = if focus == (0.5, 0.5) { String::new() } else { format!("|{:.3},{:.3}", focus.0, focus.1) };
    let key = sha1_smol::Sha1::from(format!("{path}|{}|{mtime}|{mw}x{mh}{at}", meta.len())).digest().to_string();
    let dir = crate::player::data_dir().join("wallpapers").join("cache");
    let out = dir.join(format!("{key}.png"));
    if out.is_file() {
        return Some(out.to_string_lossy().into_owned());
    }
    let img = image::ImageReader::open(path).ok()?.with_guessed_format().ok()?.decode().ok()?;
    let part = frame_at(&img, mw, mh, focus);
    std::fs::create_dir_all(&dir).ok()?;
    let tmp = dir.join(format!("{key}.tmp"));
    part.save_with_format(&tmp, image::ImageFormat::Png).ok()?;
    std::fs::rename(&tmp, &out).ok()?;
    prune(&dir, FRAMED_KEEP);
    Some(out.to_string_lossy().into_owned())
}

/// The photo cut to the monitor's shape and, if bigger, made its exact size.
#[cfg(test)]
pub fn frame(img: &image::DynamicImage, mw: u32, mh: u32) -> image::DynamicImage {
    frame_at(img, mw, mh, (0.5, 0.5))
}

pub fn frame_at(img: &image::DynamicImage, mw: u32, mh: u32, focus: (f64, f64)) -> image::DynamicImage {
    let (x, y, w, h) = crop_rect_at(img.width(), img.height(), mw, mh, focus);
    let part = img.crop_imm(x, y, w, h);
    if w > mw { part.resize_exact(mw, mh, image::imageops::FilterType::Lanczos3) } else { part }
}

/// Keeps the `keep` newest PNGs of the cache.
fn prune(dir: &std::path::Path, keep: usize) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    let mut files: Vec<_> = entries
        .flatten()
        .filter(|e| e.path().extension().is_some_and(|x| x == "png"))
        .filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path())))
        .collect();
    files.sort_by(|a, b| b.0.cmp(&a.0));
    for (_, p) in files.into_iter().skip(keep) {
        let _ = std::fs::remove_file(p);
    }
}

/// The lock screen (PLAN 0.13.6): Windows only lets apps change it on some
/// editions and setups (it may need a packaged app). Bakin asks once, when the
/// user presses «Probar»; if Windows says no, the page says it is not
/// available here. A test copy only asks whether it is supported.
#[tauri::command]
pub async fn lockscreen_set(path: String) -> Result<Value, String> {
    let file = std::path::Path::new(&path);
    if !is_image(file) || !file.is_file() {
        return Err("Eso no es una imagen.".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        #[cfg(windows)]
        {
            use windows::core::HSTRING;
            use windows::Storage::StorageFile;
            use windows::System::UserProfile::UserProfilePersonalizationSettings;
            let supported = UserProfilePersonalizationSettings::IsSupported().unwrap_or(false);
            if !supported || dry() {
                return Ok(json!({ "supported": supported, "set": false }));
            }
            let set = (|| -> windows::core::Result<bool> {
                let f = StorageFile::GetFileFromPathAsync(&HSTRING::from(path.as_str()))?.join()?;
                UserProfilePersonalizationSettings::Current()?.TrySetLockScreenImageAsync(&f)?.join()
            })()
            .unwrap_or(false);
            Ok(json!({ "supported": true, "set": set }))
        }
        #[cfg(not(windows))]
        Ok(json!({ "supported": false, "set": false }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Read-only: whether this Windows lets an app change the lock screen.
pub fn lockscreen_supported() -> bool {
    #[cfg(windows)]
    return windows::System::UserProfile::UserProfilePersonalizationSettings::IsSupported().unwrap_or(false);
    #[cfg(not(windows))]
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_main_monitor_comes_first_then_left_to_right_without_the_disconnected_ones() {
        // like the user's PC: a vertical one on the left of the main one
        let m = describe(vec![
            ("right".into(), 1920, 0, 3840, 1080),
            ("vertical".into(), -1080, -420, 0, 1500),
            ("gone".into(), 0, 0, 0, 0),
            ("main".into(), 0, 0, 1920, 1080),
        ]);
        let got: Vec<_> = m.iter().map(|m| (m.number, m.id.as_str(), m.vertical)).collect();
        assert_eq!(got, vec![(1, "main", false), (2, "vertical", true), (3, "right", false)]);
        assert_eq!((m[1].w, m[1].h), (1080, 1920));
    }

    #[test]
    fn the_crop_has_the_monitors_shape_and_stays_centred() {
        // a landscape photo on the vertical monitor: full height, the middle strip
        assert_eq!(crop_rect(3840, 2160, 1080, 1920), (1312, 0, 1215, 2160));
        // the same photo on a 16:9 monitor: all of it
        assert_eq!(crop_rect(3840, 2160, 1920, 1080), (0, 0, 3840, 2160));
        // a tall photo on a landscape monitor: full width, the middle band
        assert_eq!(crop_rect(1000, 3000, 1920, 1080), (0, 1218, 1000, 563));
        // ultrawide monitor
        let (x, _, w, h) = crop_rect(1920, 1080, 3440, 1440);
        assert_eq!((w, h, x), (1920, 804, 0));
    }

    #[test]
    fn the_crop_goes_where_it_was_framed() {
        // a landscape photo on the vertical monitor: the strip at the left, the right, the centre
        assert_eq!(crop_rect_at(3840, 2160, 1080, 1920, (0.0, 0.5)), (0, 0, 1215, 2160));
        assert_eq!(crop_rect_at(3840, 2160, 1080, 1920, (1.0, 0.5)), (2625, 0, 1215, 2160));
        assert_eq!(crop_rect_at(3840, 2160, 1080, 1920, (0.5, 0.5)), crop_rect(3840, 2160, 1080, 1920));
        // out of range is kept inside the photo
        assert_eq!(crop_rect_at(1000, 3000, 1920, 1080, (0.5, 7.0)).1, 3000 - 563);
    }

    #[test]
    fn framing_makes_it_the_monitors_size_but_never_bigger() {
        let big = image::DynamicImage::new_rgb8(3840, 2160);
        let v = frame(&big, 1080, 1920);
        assert_eq!((v.width(), v.height()), (1080, 1920));
        let small = image::DynamicImage::new_rgb8(64, 80);
        let s = frame(&small, 1920, 1080);
        assert_eq!((s.width(), s.height()), (64, 36)); // cut, not enlarged: Windows fills the rest
    }

    /// Read-only, by hand: `cargo test lockscreen_here -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn lockscreen_here() {
        println!("lock screen supported: {}", lockscreen_supported());
    }

    /// Read-only, by hand: `cargo test real_monitors -- --ignored --nocapture`.
    #[test]
    #[ignore]
    #[cfg(windows)]
    fn real_monitors() {
        let m = describe(com::rects().unwrap());
        println!("{:?}", m.iter().map(|m| (m.number, m.w, m.h, m.vertical)).collect::<Vec<_>>());
        assert!(!m.is_empty());
    }
}
