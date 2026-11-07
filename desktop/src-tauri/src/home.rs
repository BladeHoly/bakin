//! «En tu casa» (PLAN 0.21): the home server (lan.rs) wired to Bakin. The
//! library, stars, plays, covers and lyrics come from the engine (`lan_*`
//! methods); the pairing code lives in the engine's vault. Off by default;
//! on, it starts with Bakin and stays while Bakin is in the tray.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};

use crate::engine::Engine;
use crate::lan::{LanServer, Library, Playlist, Snapshot, Song};

#[derive(Default)]
pub struct LanState(Mutex<Option<LanServer>>);

const DEFAULT_PORT: u16 = 4533;

/// The engine, as the server's library.
struct EngineLibrary(AppHandle);

impl EngineLibrary {
    fn call(&self, method: &str, params: Value, secs: u64) -> Result<Value, String> {
        self.0.state::<Engine>().call(method, params, Duration::from_secs(secs))
    }
}

fn text(v: &Value, key: &str) -> String {
    v.get(key).and_then(Value::as_str).unwrap_or_default().to_string()
}

fn number(v: &Value, key: &str) -> Option<u64> {
    v.get(key).and_then(|x| x.as_u64().or_else(|| x.as_f64().map(|f| f as u64)))
}

/// The engine's `lan_library` answer as the server's library; songs without an id are left out.
fn snapshot_from(data: &Value) -> Snapshot {
    let songs = data["songs"]
        .as_array()
        .map(|a| a.as_slice())
        .unwrap_or_default()
        .iter()
        .map(|s| Song {
            id: text(s, "id"),
            path: text(s, "path").into(),
            title: text(s, "title"),
            artist: text(s, "artist"),
            album: text(s, "album"),
            duration: number(s, "duration").unwrap_or(0) as u32,
            track: number(s, "track").map(|n| n as u32),
            year: number(s, "year").map(|n| n as u32),
            size: number(s, "size").unwrap_or(0),
            suffix: text(s, "suffix"),
            bitrate: number(s, "bitrate").map(|n| n as u32),
            starred: s["starred"].as_bool().unwrap_or(false),
            plays: number(s, "plays").unwrap_or(0) as u32,
            added: s["added"].as_f64().unwrap_or(0.0),
        })
        .filter(|s| !s.id.is_empty())
        .collect();
    let playlists = data["playlists"]
        .as_array()
        .map(|a| a.as_slice())
        .unwrap_or_default()
        .iter()
        .map(|p| Playlist {
            id: text(p, "id"),
            name: text(p, "name"),
            song_ids: p["song_ids"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default(),
        })
        .collect();
    Snapshot { songs, playlists }
}

/// Only a file of Bakin's preview cache is served as a cover, never a path from anywhere else.
fn is_cached_cover(path: &std::path::Path, thumbs: &std::path::Path) -> bool {
    !path.as_os_str().is_empty() && path.starts_with(thumbs)
}

/// The download flow's link for a URL sent from the phone.
fn download_link(url: &str) -> String {
    format!("bakin://download?url={}", form_urlencoded::byte_serialize(url.as_bytes()).collect::<String>())
}

/// The address the phone opens: the first one on the network (not this PC's loopback).
fn lan_address(addrs: &[String]) -> Option<String> {
    addrs.iter().find(|a| !a.starts_with("127.")).cloned()
}

impl Library for EngineLibrary {
    fn snapshot(&self) -> Result<Snapshot, String> {
        let data = self.call("lan_library", json!({}), 120)?;
        Ok(snapshot_from(&data))
    }

    fn star(&self, song_id: &str, on: bool) -> Result<(), String> {
        self.call("lan_star", json!({ "id": song_id, "on": on }), 15).map(|_| ())
    }

    fn scrobble(&self, song_id: &str) -> Result<(), String> {
        self.call("lan_scrobble", json!({ "id": song_id }), 15).map(|_| ())
    }

    fn cover(&self, song_id: &str, size: u32) -> Option<Vec<u8>> {
        let size = if size == 0 { 640 } else { size };
        let r = self.call("lan_cover", json!({ "id": song_id, "size": size }), 30).ok()?;
        let path = text(&r, "path");
        let path = std::path::Path::new(&path);
        if !is_cached_cover(path, &crate::player::data_dir().join("thumbs")) {
            return None;
        }
        std::fs::read(path).ok()
    }

    fn lyrics(&self, song_id: &str) -> Option<String> {
        let r = self.call("lan_lyrics", json!({ "id": song_id }), 15).ok()?;
        Some(text(&r, "text")).filter(|t| !t.is_empty())
    }

    /// The phone's link opens the download flow on the PC, like the browser extension: nothing is
    /// queued until the user confirms there.
    fn link(&self, url: &str) -> Result<(), String> {
        let deep = download_link(url);
        if self.0.get_webview_window("main").is_some() {
            self.0.emit("bakin-links", json!({ "urls": [deep] })).map_err(|e| e.to_string())
        } else {
            // the window is closed (tray): the page picks it up when it opens
            self.0.state::<crate::links::Inbox>().0.lock().unwrap().push(deep);
            Ok(())
        }
    }
}

fn settings(app: &AppHandle) -> Result<Value, String> {
    // an empty change returns the settable settings, lan_* among them
    let r = app.state::<Engine>().call("settings_set", json!({ "changes": {} }), Duration::from_secs(15))?;
    Ok(r["settings"].clone())
}

fn code(app: &AppHandle, new: bool) -> Result<String, String> {
    let r = app.state::<Engine>().call("lan_code", json!({ "new": new }), Duration::from_secs(15))?;
    Some(text(&r, "code")).filter(|c| !c.is_empty()).ok_or_else(|| "no pairing code".to_string())
}

fn start(app: &AppHandle) -> Result<(), String> {
    let port = settings(app)?["lan_port"].as_u64().map(|p| p as u16).unwrap_or(DEFAULT_PORT);
    let server = LanServer::start(port, code(app, false)?, Arc::new(EngineLibrary(app.clone())))
        .map_err(|e| format!("No se pudo abrir el puerto {port} ({e}). ¿Otro programa lo usa?"))?;
    *app.state::<LanState>().0.lock().unwrap() = Some(server);
    Ok(())
}

fn stop(app: &AppHandle) {
    if let Some(server) = app.state::<LanState>().0.lock().unwrap().take() {
        server.stop();
    }
}

/// What Configuración › En tu casa shows: whether it's on, where, the code and a QR of the page.
fn status(app: &AppHandle, error: Option<String>) -> Value {
    let addrs: Vec<String> = app
        .state::<LanState>()
        .0
        .lock()
        .unwrap()
        .as_ref()
        .map(|s| s.addresses().iter().map(|a| a.to_string()).collect())
        .unwrap_or_default();
    let running = !addrs.is_empty();
    let lan = lan_address(&addrs);
    let url = lan.as_ref().map(|a| format!("http://{a}")).unwrap_or_default();
    let qr = (!url.is_empty())
        .then(|| qrcode::QrCode::new(format!("{url}/app/").as_bytes()).ok())
        .flatten()
        .map(|c| c.render::<qrcode::render::svg::Color>().min_dimensions(116, 116).quiet_zone(false).build())
        .unwrap_or_default();
    let code = if running { code(app, false).unwrap_or_default() } else { String::new() };
    json!({ "running": running, "addresses": addrs, "private": lan.is_some(), "url": url, "code": code,
            "qr_svg": qr, "error": error })
}

#[tauri::command]
pub async fn lan_status(app: AppHandle) -> Value {
    tauri::async_runtime::spawn_blocking(move || status(&app, None)).await.unwrap_or_else(|e| json!({ "error": e.to_string() }))
}

#[tauri::command]
pub async fn lan_set(app: AppHandle, on: bool) -> Value {
    tauri::async_runtime::spawn_blocking(move || {
        stop(&app);
        let err = if on { start(&app).err() } else { None };
        let saved = err.is_none() && on;
        let _ = app.state::<Engine>().call("settings_set", json!({ "changes": { "lan_enabled": saved } }), Duration::from_secs(15));
        status(&app, err)
    })
    .await
    .unwrap_or_else(|e| json!({ "error": e.to_string() }))
}

/// A new pairing code: every phone has to connect again (that's how one is removed).
#[tauri::command]
pub async fn lan_new_code(app: AppHandle) -> Value {
    tauri::async_runtime::spawn_blocking(move || {
        let running = app.state::<LanState>().0.lock().unwrap().is_some();
        let err = code(&app, true).err().or_else(|| {
            if running {
                stop(&app);
                start(&app).err()
            } else {
                None
            }
        });
        status(&app, err)
    })
    .await
    .unwrap_or_else(|e| json!({ "error": e.to_string() }))
}

/// At start: back on if it was on (read from the settings file, so the engine only starts when
/// sharing is on).
pub fn autostart(app: &AppHandle) {
    let file = crate::player::data_dir().join("ui-settings.json");
    let on = std::fs::read_to_string(file)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v["lan_enabled"].as_bool())
        .unwrap_or(false);
    if on {
        let app = app.clone();
        std::thread::spawn(move || {
            if start(&app).is_err() {
                let _ = app.emit("lan", json!({ "error": "No se pudo compartir en la red de casa al arrancar." }));
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn qr_renders_svg() {
        let svg = qrcode::QrCode::new(b"http://192.168.1.20:4533/app/")
            .unwrap()
            .render::<qrcode::render::svg::Color>()
            .min_dimensions(116, 116)
            .quiet_zone(false)
            .build();
        assert!(svg.starts_with("<?xml") && svg.contains("<svg"));
    }

    #[test]
    fn the_engines_library_becomes_the_servers() {
        let data = json!({
            "songs": [
                {"id": "a1", "path": "C:/M/a.opus", "title": "Uno", "artist": "X", "album": "D", "duration": 201.7,
                 "track": 3, "year": 2024, "size": 4_000_000, "suffix": "opus", "bitrate": 160, "starred": true,
                 "plays": 7, "added": 1_700_000_000.5},
                {"id": "", "title": "sin id"},
                {"id": "b2"}
            ],
            "playlists": [{"id": "p", "name": "Lista", "song_ids": ["a1", 5, "b2"]}, {"id": "q"}]
        });
        let snap = snapshot_from(&data);
        assert_eq!(snap.songs.len(), 2);
        let a = &snap.songs[0];
        assert_eq!((a.id.as_str(), a.title.as_str(), a.duration, a.track, a.year), ("a1", "Uno", 201, Some(3), Some(2024)));
        assert_eq!((a.size, a.bitrate, a.starred, a.plays, a.added), (4_000_000, Some(160), true, 7, 1_700_000_000.5));
        assert_eq!(a.path, Path::new("C:/M/a.opus"));
        let b = &snap.songs[1];
        assert_eq!((b.title.as_str(), b.duration, b.track, b.starred), ("", 0, None, false));
        assert_eq!(snap.playlists[0].song_ids, vec!["a1", "b2"]); // ids that aren't text are dropped
        assert!(snap.playlists[1].song_ids.is_empty() && snap.playlists[1].name.is_empty());
        assert!(snapshot_from(&json!({})).songs.is_empty());
    }

    #[test]
    fn covers_only_from_the_preview_cache() {
        let thumbs = Path::new(r"D:\BakinData\thumbs");
        assert!(is_cached_cover(&thumbs.join("ab").join("cd.jpg"), thumbs));
        assert!(!is_cached_cover(Path::new(r"D:\Fotos\me.jpg"), thumbs));
        assert!(!is_cached_cover(Path::new(r"D:\BakinData\thumbs-old\a.jpg"), thumbs));
        assert!(!is_cached_cover(Path::new(""), thumbs));
    }

    #[test]
    fn a_link_from_the_phone_opens_the_download_dialog() {
        assert_eq!(download_link("https://youtu.be/x?t=1&a=b c"), "bakin://download?url=https%3A%2F%2Fyoutu.be%2Fx%3Ft%3D1%26a%3Db+c");
    }

    #[test]
    fn the_phone_gets_the_network_address() {
        let addrs = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(lan_address(&addrs(&["127.0.0.1:4533", "192.168.1.20:4533"])), Some("192.168.1.20:4533".into()));
        assert_eq!(lan_address(&addrs(&["127.0.0.1:4533"])), None);
        assert_eq!(lan_address(&[]), None);
    }
}
