//! A song's cover for the Windows media controls and the small window,
//! without waking the engine: the engine already caches covers as JPEG in
//! `BakinData/thumbs/<key[:2]>/<key>.jpg`, with key = sha1("path|size|mtime|px")
//! (bakin/library.py `thumb`). Only if none is cached, and the engine is
//! running anyway, is it asked.

use std::path::PathBuf;
use std::time::{Duration, UNIX_EPOCH};

use base64::Engine as _;
use serde_json::json;
use tauri::{AppHandle, Manager};

use crate::engine::Engine;

/// The sizes the engine caches covers at, biggest first (320: the player,
/// 420: photos, 96: list rows).
const SIZES: [u32; 3] = [320, 420, 96];

/// Where the engine keeps the cover of this file at `px`, as library.thumb names it.
pub fn cache_path(data_dir: &std::path::Path, path: &str, size: u64, mtime: u64, px: u32) -> PathBuf {
    let key = sha1_smol::Sha1::from(format!("{path}|{size}|{mtime}|{px}")).digest().to_string();
    data_dir.join("thumbs").join(&key[..2]).join(format!("{key}.jpg"))
}

/// The cached cover of a song file, if the engine made one.
pub fn cached(path: &str) -> Option<Vec<u8>> {
    let meta = std::fs::metadata(path).ok()?;
    // Python's int(st_mtime) truncates the seconds, like as_secs
    let mtime = meta.modified().ok()?.duration_since(UNIX_EPOCH).ok()?.as_secs();
    let dir = crate::player::data_dir();
    SIZES.iter().find_map(|&px| std::fs::read(cache_path(&dir, path, meta.len(), mtime, px)).ok())
}

/// The cover of a song: from the cache, or from the engine if it is running.
pub fn cover(app: &AppHandle, item_id: &str, path: &str) -> Option<Vec<u8>> {
    if let Some(jpeg) = cached(path) {
        return Some(jpeg);
    }
    let r = app.state::<Engine>().call_if_running("music_cover", json!({ "item_id": item_id, "size": 320 }), Duration::from_secs(15))?;
    let uri = r.get("cover")?.as_str()?;
    base64::engine::general_purpose::STANDARD.decode(uri.split_once(',')?.1).ok()
}

/// For the small window: the cover as a data URI ("" when there is none).
#[tauri::command]
pub async fn track_cover(app: AppHandle, item_id: String, path: String) -> String {
    tauri::async_runtime::spawn_blocking(move || cover(&app, &item_id, &path))
        .await
        .ok()
        .flatten()
        .map(|jpeg| format!("data:image/jpeg;base64,{}", base64::engine::general_purpose::STANDARD.encode(jpeg)))
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_cache_name_matches_the_engines() {
        // python: hashlib.sha1(r"C:\Music\a.opus|1234|1700000000|320".encode()).hexdigest()
        let p = cache_path(std::path::Path::new("D"), r"C:\Music\a.opus", 1234, 1_700_000_000, 320);
        let want = std::path::Path::new("D").join("thumbs").join("71").join("7120e7dbe7719536f1b5af4374236ba173b1a9e4.jpg");
        assert_eq!(p, want);
    }
}
