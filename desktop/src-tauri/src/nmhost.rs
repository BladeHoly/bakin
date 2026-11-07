//! The browser extension hands Bakin the YouTube cookies (a friend, 2026-11-07: Brave, Chrome and
//! Edge encrypt their cookie files for themselves, so yt-dlp can't read them on Windows, and
//! exporting a cookies.txt by hand is a chore).
//!
//! It goes through the browser's native messaging: the browser starts `Bakin.exe` with the
//! extension's origin as its argument and talks to it over stdin/stdout, and only the extension
//! named in the registered manifest may (`allowed_origins`, a fixed ID from the manifest's `key`).
//! No port, no file left in Downloads, nothing in a URL. This process keeps only the YouTube and
//! Google lines, encrypts them with DPAPI like the engine's vault (same entropy, so the engine can
//! read them) as `cookies-incoming`, answers, and exits; the extension then opens
//! `bakin://cookies` and the window moves them to where the presets read them
//! (`cookies_take_incoming`). The cookies are never logged or shown.

use std::io::{Read, Write};
use std::path::PathBuf;

/// The name the browser knows the host by (the extension's `sendNativeMessage`).
pub const HOST: &str = "dev.bakin.cookies";
/// The extension's fixed ID (from the public `key` in extension/manifest.json).
const CHROMIUM_ORIGIN: &str = "chrome-extension://mekgdfenkaoklmelenfjnjkghahpnbhp/";
/// Firefox's ID for it (`browser_specific_settings.gecko.id`).
const FIREFOX_ID: &str = "bakin@bladeholy";
/// What a cookies.txt can weigh (a YouTube session is ~4–10 KB).
const MAX_MESSAGE: u32 = 1 << 20;

/// Bakin started by a browser to talk to the extension (not by the user).
pub fn is_invocation(args: &[String]) -> bool {
    args.iter().skip(1).any(|a| a.starts_with("chrome-extension://") || a == FIREFOX_ID)
}

/// Runs the exchange and returns the exit code.
pub fn run() -> i32 {
    let reply = match read_message().and_then(|m| take(&m)) {
        Ok(n) => serde_json::json!({ "ok": true, "count": n }),
        Err(e) => serde_json::json!({ "ok": false, "error": e }),
    };
    write_message(&reply).map(|_| 0).unwrap_or(1)
}

fn read_message() -> Result<serde_json::Value, String> {
    let mut stdin = std::io::stdin().lock();
    let mut len = [0u8; 4];
    stdin.read_exact(&mut len).map_err(|_| "sin mensaje".to_string())?;
    let len = u32::from_le_bytes(len);
    if len == 0 || len > MAX_MESSAGE {
        return Err("mensaje demasiado grande".into());
    }
    let mut buf = vec![0u8; len as usize];
    stdin.read_exact(&mut buf).map_err(|_| "mensaje incompleto".to_string())?;
    serde_json::from_slice(&buf).map_err(|_| "mensaje inválido".to_string())
}

fn write_message(v: &serde_json::Value) -> std::io::Result<()> {
    let body = v.to_string();
    let mut out = std::io::stdout().lock();
    out.write_all(&(body.len() as u32).to_le_bytes())?;
    out.write_all(body.as_bytes())?;
    out.flush()
}

/// Keeps the YouTube/Google lines of a cookies.txt and saves them encrypted; the number of cookies.
fn take(msg: &serde_json::Value) -> Result<usize, String> {
    let text = msg.get("cookies").and_then(|c| c.as_str()).ok_or("sin cookies")?;
    let (kept, count, session) = youtube_only(text);
    if !session {
        return Err("No hay una sesión de YouTube en este navegador: inicia sesión en youtube.com y vuelve a probar.".into());
    }
    let enc = protect(kept.as_bytes())?;
    let dir = crate::player::data_dir().join("vault");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let tmp = dir.join(".cookies-incoming.tmp");
    std::fs::write(&tmp, enc).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, dir.join("cookies-incoming.bin")).map_err(|e| e.to_string())?;
    Ok(count)
}

/// The cookies.txt lines for youtube.com and google.com (and their subdomains) only, the header,
/// how many cookies, and whether a signed-in session is among them.
fn youtube_only(text: &str) -> (String, usize, bool) {
    const SESSION: [&str; 6] = ["SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO", "SSID"];
    let mut out = String::from("# Netscape HTTP Cookie File\n");
    let (mut n, mut session) = (0, false);
    for line in text.lines() {
        let bare = line.strip_prefix("#HttpOnly_").unwrap_or(line);
        if bare.starts_with('#') || bare.trim().is_empty() {
            continue;
        }
        let parts: Vec<&str> = bare.split('\t').collect();
        if parts.len() < 7 || parts.iter().any(|p| p.contains('\n') || p.contains('\r')) {
            continue;
        }
        let domain = parts[0].trim_start_matches('.').to_ascii_lowercase();
        let ours = ["youtube.com", "google.com"].iter().any(|d| domain == *d || domain.ends_with(&format!(".{d}")));
        if !ours {
            continue;
        }
        session |= SESSION.contains(&parts[5]);
        n += 1;
        out.push_str(line);
        out.push('\n');
    }
    (out, n, session)
}

#[cfg(windows)]
fn protect(data: &[u8]) -> Result<Vec<u8>, String> {
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{CryptProtectData, CRYPT_INTEGER_BLOB};
    // the engine's vault entropy (bakin/vault.py), so the engine reads what this writes
    const ENTROPY: &[u8] = b"Bakin vault v1";
    const UI_FORBIDDEN: u32 = 0x1;
    let desc: Vec<u16> = "Bakin".encode_utf16().chain(std::iter::once(0)).collect();
    let input = CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 };
    let entropy = CRYPT_INTEGER_BLOB { cbData: ENTROPY.len() as u32, pbData: ENTROPY.as_ptr() as *mut u8 };
    let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: std::ptr::null_mut() };
    // SAFETY: the blobs point at live buffers for the call; the output is freed with LocalFree
    let ok = unsafe { CryptProtectData(&input, desc.as_ptr(), &entropy, std::ptr::null(), std::ptr::null(), UI_FORBIDDEN, &mut out) };
    if ok == 0 {
        return Err("Windows no pudo cifrar las cookies.".into());
    }
    // SAFETY: CryptProtectData filled `out` with cbData bytes at pbData
    let bytes = unsafe { std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec() };
    unsafe { LocalFree(out.pbData as _) };
    Ok(bytes)
}

#[cfg(not(windows))]
fn protect(_data: &[u8]) -> Result<Vec<u8>, String> {
    Err("solo en Windows".into())
}

/// Registers the host for Chrome, Brave, Edge and Firefox (HKCU), pointing at this executable,
/// on every start like `bakin://`. Test copies (`BAKIN_HOME`) never touch the registry.
pub fn register() {
    if std::env::var_os("BAKIN_HOME").is_some() {
        return;
    }
    let _ = try_register();
}

fn manifests_dir() -> PathBuf {
    crate::player::data_dir().join("native-host")
}

#[cfg(windows)]
fn try_register() -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let dir = manifests_dir();
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let base = serde_json::json!({
        "name": HOST,
        "description": "Bakin: guarda las cookies de YouTube que manda su extensión",
        "path": exe.display().to_string(),
        "type": "stdio",
    });
    let mut chromium = base.clone();
    chromium["allowed_origins"] = serde_json::json!([CHROMIUM_ORIGIN]);
    let mut firefox = base;
    firefox["allowed_extensions"] = serde_json::json!([FIREFOX_ID]);
    let chromium_file = dir.join("chromium.json");
    let firefox_file = dir.join("firefox.json");
    std::fs::write(&chromium_file, serde_json::to_vec_pretty(&chromium).unwrap()).map_err(|e| e.to_string())?;
    std::fs::write(&firefox_file, serde_json::to_vec_pretty(&firefox).unwrap()).map_err(|e| e.to_string())?;
    for (key, file) in [
        (r"Software\Google\Chrome\NativeMessagingHosts", &chromium_file),
        (r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts", &chromium_file),
        (r"Software\Microsoft\Edge\NativeMessagingHosts", &chromium_file),
        (r"Software\Chromium\NativeMessagingHosts", &chromium_file),
        (r"Software\Mozilla\NativeMessagingHosts", &firefox_file),
    ] {
        let k = windows_registry::CURRENT_USER.create(format!(r"{key}\{HOST}")).map_err(|e| e.to_string())?;
        k.set_string("", file.display().to_string()).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(not(windows))]
fn try_register() -> Result<(), String> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_youtube_and_google_lines_are_kept() {
        let text = "# Netscape HTTP Cookie File\n\
            .youtube.com\tTRUE\t/\tTRUE\t1900000000\tSAPISID\tabc\n\
            #HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1900000000\tLOGIN_INFO\tdef\n\
            accounts.google.com\tFALSE\t/\tTRUE\t1900000000\tSID\tghi\n\
            .evil-youtube.com\tTRUE\t/\tFALSE\t0\tSID\tnope\n\
            .bank.example\tTRUE\t/\tTRUE\t0\tsession\tnope\n\
            broken line\n";
        let (kept, n, session) = youtube_only(text);
        assert_eq!(n, 3);
        assert!(session && kept.contains("abc") && kept.contains("#HttpOnly_.youtube.com") && kept.contains("ghi"));
        assert!(!kept.contains("nope"));
        let (_, n, session) = youtube_only(".youtube.com\tTRUE\t/\tFALSE\t0\tPREF\tf1=1\n");
        assert!(n == 1 && !session); // signed out: refused
    }

    #[test]
    fn only_a_browser_starting_the_extension_makes_it_a_host() {
        let args = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert!(is_invocation(&args(&["bakin.exe", "chrome-extension://mekgdfenkaoklmelenfjnjkghahpnbhp/", "--parent-window=0"])));
        assert!(is_invocation(&args(&["bakin.exe", r"C:\x\firefox.json", "bakin@bladeholy"])));
        assert!(!is_invocation(&args(&["bakin.exe", "bakin://download?url=x"])));
        assert!(!is_invocation(&args(&["bakin.exe", "--background"])));
    }
}
