//! Ways to hand Bakin a link without pasting it in the window:
//! * `bakin://download?url=...` links (from the browser extension or a
//!   bookmark). They only open the download dialog: a web page can fire a
//!   `bakin://` link, so nothing is ever queued without the user confirming.
//! * optional clipboard watching: when a copied text is a single http(s)
//!   link the page offers to download it. Nothing else is read or kept.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde_json::json;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_deep_link::DeepLinkExt;

use crate::background::show_main;

#[derive(Default)]
pub struct ClipboardWatch(pub AtomicBool);

/// Links that arrived while the window didn't exist (it was closed in the
/// tray): the page picks them up when it loads.
#[derive(Default)]
pub struct Inbox(pub Mutex<Vec<String>>);

/// Links Bakin was started with (`bakin://...` as the first argument), and
/// those that came while the window was closed.
#[tauri::command]
pub fn pending_links(app: AppHandle) -> Vec<String> {
    let mut out: Vec<String> = app
        .deep_link()
        .get_current()
        .ok()
        .flatten()
        .map(|urls| urls.into_iter().map(|u| u.to_string()).collect())
        .unwrap_or_default();
    out.append(&mut app.state::<Inbox>().0.lock().unwrap());
    out
}

pub fn setup(app: &AppHandle) {
    // (Re)register bakin:// for this executable on every start (HKCU), so the
    // link keeps working if Bakin is moved or reinstalled elsewhere.
    let _ = app.deep_link().register_all();
    let handle = app.clone();
    app.deep_link().on_open_url(move |event| {
        let urls: Vec<String> = event.urls().iter().map(|u| u.to_string()).collect();
        if show_main(&handle) {
            // a new window: its page asks for these when it loads
            handle.state::<Inbox>().0.lock().unwrap().extend(urls);
        } else {
            let _ = handle.emit("bakin-links", json!({ "urls": urls }));
        }
    });
    let handle = app.clone();
    std::thread::spawn(move || clipboard_loop(handle));
}

/// Opens the folder with the browser extension shipped with Bakin, so it can
/// be loaded with "Load unpacked" (Brave/Chrome/Edge) or as a temporary
/// add-on (Firefox).
#[tauri::command]
pub fn open_extension_folder(app: AppHandle) -> Result<String, String> {
    let dir = app.path().resource_dir().map_err(|e| e.to_string())?.join("extension");
    if !dir.join("manifest.json").is_file() {
        return Err("No se encontró la extensión en la instalación de Bakin.".into());
    }
    std::process::Command::new("explorer.exe").arg(&dir).spawn().map_err(|e| e.to_string())?;
    Ok(dir.display().to_string())
}

#[tauri::command]
pub fn set_clipboard_watch(state: tauri::State<'_, ClipboardWatch>, value: bool) {
    state.0.store(value, Ordering::Relaxed);
}

fn clipboard_loop(app: AppHandle) {
    let mut last_seq = clipboard_sequence();
    loop {
        std::thread::sleep(Duration::from_millis(900));
        let seq = clipboard_sequence();
        if seq == last_seq {
            continue;
        }
        last_seq = seq;
        if !app.state::<ClipboardWatch>().0.load(Ordering::Relaxed) {
            continue;
        }
        if let Some(text) = clipboard_text() {
            if let Some(t) = single_link(&text) {
                if app.get_webview_window("main").is_some() {
                    let _ = app.emit("clipboard-link", json!({ "url": t }));
                } else {
                    // no window (closed in the tray): offer it when Bakin is opened
                    let link = inbox_link(t);
                    app.state::<Inbox>().0.lock().unwrap().push(link);
                    if app.state::<crate::background::Notify>().0.load(Ordering::Relaxed) {
                        use tauri_plugin_notification::NotificationExt;
                        let _ = app.notification().builder().title("Copiaste un enlace").body("Abre Bakin para descargarlo.").show();
                    }
                }
            }
        }
    }
}

/// The copied text when it is one http(s) link and nothing else (trimmed).
fn single_link(text: &str) -> Option<&str> {
    let t = text.trim();
    let one_link = (t.starts_with("http://") || t.starts_with("https://")) && t.len() < 2048 && !t.chars().any(char::is_whitespace);
    one_link.then_some(t)
}

/// A copied link kept for when the window opens: it goes through the download dialog.
fn inbox_link(url: &str) -> String {
    format!("bakin://download?url={}", percent_encode(url))
}

/// Encodes a URL to travel inside a bakin:// link's query.
fn percent_encode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

#[cfg(windows)]
fn clipboard_sequence() -> u32 {
    // SAFETY: plain query, no arguments.
    unsafe { windows_sys::Win32::System::DataExchange::GetClipboardSequenceNumber() }
}

#[cfg(not(windows))]
fn clipboard_sequence() -> u32 {
    0
}

#[cfg(windows)]
fn clipboard_text() -> Option<String> {
    use windows_sys::Win32::System::DataExchange::{CloseClipboard, GetClipboardData, OpenClipboard};
    use windows_sys::Win32::System::Memory::{GlobalLock, GlobalUnlock};
    const CF_UNICODETEXT: u32 = 13;
    // SAFETY: the clipboard is opened, read through a locked global handle
    // within bounds (up to the terminating NUL, capped), then unlocked/closed.
    unsafe {
        if OpenClipboard(std::ptr::null_mut()) == 0 {
            return None;
        }
        let mut out = None;
        let h = GetClipboardData(CF_UNICODETEXT);
        if !h.is_null() {
            let p = GlobalLock(h as _) as *const u16;
            if !p.is_null() {
                let mut len = 0usize;
                while len < 4096 && *p.add(len) != 0 {
                    len += 1;
                }
                if len < 4096 {
                    out = Some(String::from_utf16_lossy(std::slice::from_raw_parts(p, len)));
                }
                GlobalUnlock(h as _);
            }
        }
        CloseClipboard();
        out
    }
}

#[cfg(not(windows))]
fn clipboard_text() -> Option<String> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_lone_web_link_counts() {
        assert_eq!(single_link("  https://youtu.be/abc?t=1 \r\n"), Some("https://youtu.be/abc?t=1"));
        assert_eq!(single_link("http://example.com"), Some("http://example.com"));
        assert_eq!(single_link("mira esto https://youtu.be/abc"), None);
        assert_eq!(single_link("https://a.com https://b.com"), None);
        assert_eq!(single_link("ftp://example.com/x"), None);
        assert_eq!(single_link("file:///C:/Bakin/x"), None);
        assert_eq!(single_link("HTTPS://EXAMPLE.COM"), None);
        assert_eq!(single_link(""), None);
        let long = format!("https://example.com/{}", "a".repeat(2048));
        assert_eq!(single_link(&long), None);
    }

    #[test]
    fn links_travel_encoded() {
        assert_eq!(percent_encode("AZaz09-_.~"), "AZaz09-_.~");
        assert_eq!(percent_encode("https://x.com/a?b=1&c=d e"), "https%3A%2F%2Fx.com%2Fa%3Fb%3D1%26c%3Dd%20e");
        assert_eq!(percent_encode("ñ"), "%C3%B1"); // UTF-8 bytes
        assert_eq!(inbox_link("https://youtu.be/x"), "bakin://download?url=https%3A%2F%2Fyoutu.be%2Fx");
    }
}
