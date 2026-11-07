//! What Bakin really costs, for Configuración > Consumo: every process it
//! started (the engine, the window's WebView2, tools like yt-dlp while they
//! run) counted the way the Task Manager does (private working set).

use serde_json::{json, Value};
use windows::Win32::Foundation::CloseHandle;
use windows::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
};
use windows::Win32::System::ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS, PROCESS_MEMORY_COUNTERS_EX2};
use windows::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};

/// (pid, parent pid, exe name) of every process on the machine.
fn processes() -> Vec<(u32, u32, String)> {
    let mut out = Vec::new();
    // SAFETY: a snapshot handle read with a correctly sized entry, then closed.
    unsafe {
        let Ok(snap) = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) else { return out };
        let mut e = PROCESSENTRY32W { dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32, ..Default::default() };
        if Process32FirstW(snap, &mut e).is_ok() {
            loop {
                let len = e.szExeFile.iter().position(|&c| c == 0).unwrap_or(e.szExeFile.len());
                out.push((e.th32ProcessID, e.th32ParentProcessID, String::from_utf16_lossy(&e.szExeFile[..len])));
                if Process32NextW(snap, &mut e).is_err() {
                    break;
                }
            }
        }
        let _ = CloseHandle(snap);
    }
    out
}

/// Private working set in bytes (what the Task Manager shows as Memory).
fn private_ws(pid: u32) -> Option<u64> {
    // SAFETY: a query-only handle, a correctly sized counters struct, closed after.
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut c = PROCESS_MEMORY_COUNTERS_EX2::default();
        let ok = GetProcessMemoryInfo(
            h,
            &mut c as *mut PROCESS_MEMORY_COUNTERS_EX2 as *mut PROCESS_MEMORY_COUNTERS,
            std::mem::size_of::<PROCESS_MEMORY_COUNTERS_EX2>() as u32,
        );
        let _ = CloseHandle(h);
        ok.ok().map(|_| c.PrivateWorkingSetSize as u64)
    }
}

/// `me` and every process below it, from (pid, parent pid, exe name) of all of them.
fn descendants(me: u32, all: &[(u32, u32, String)]) -> Vec<(u32, String)> {
    let mut ours = vec![(me, "bakin-desktop.exe".to_string())];
    let mut i = 0;
    while i < ours.len() {
        let parent = ours[i].0;
        for (pid, ppid, name) in all {
            if *ppid == parent && !ours.iter().any(|(p, _)| p == pid) {
                ours.push((*pid, name.clone()));
            }
        }
        i += 1;
    }
    ours
}

/// Which of `usage`'s groups a process counts in; None for the console host (not Bakin's).
fn group_of(pid: u32, me: u32, name: &str) -> Option<usize> {
    let n = name.to_lowercase();
    if n == "conhost.exe" {
        None
    } else if pid == me {
        Some(0)
    } else if n.starts_with("bakin-engine") || n.starts_with("python") {
        Some(1)
    } else if n.starts_with("msedgewebview2") {
        Some(2)
    } else {
        Some(3)
    }
}

/// Bytes as MB with one decimal.
fn mb(b: u64) -> f64 {
    (b as f64 / 1_048_576.0 * 10.0).round() / 10.0
}

#[tauri::command]
pub fn usage() -> Value {
    let me = std::process::id();
    // Bakin and everything below it
    let ours = descendants(me, &processes());
    let mut groups: Vec<(&str, u64)> = vec![("Bakin y el reproductor", 0), ("Motor de descargas", 0), ("Ventana", 0), ("Herramientas", 0)];
    for (pid, name) in &ours {
        let Some(g) = group_of(*pid, me, name) else { continue };
        let Some(bytes) = private_ws(*pid) else { continue };
        groups[g].1 += bytes;
    }
    let total: u64 = groups.iter().map(|g| g.1).sum();
    json!({
        "total_mb": mb(total),
        "groups": groups.iter().map(|(name, b)| json!({ "name": name, "mb": mb(*b) })).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(pid: u32, ppid: u32, name: &str) -> (u32, u32, String) {
        (pid, ppid, name.to_string())
    }

    #[test]
    fn bakin_and_everything_below_it() {
        let all = vec![
            p(1, 0, "explorer.exe"),
            p(10, 1, "bakin-desktop.exe"),
            p(11, 10, "bakin-engine.exe"),
            p(12, 11, "yt-dlp.exe"),
            p(13, 12, "yt-dlp.exe"), // yt-dlp runs as two processes
            p(14, 10, "msedgewebview2.exe"),
            p(20, 1, "chrome.exe"),
            p(21, 20, "msedgewebview2.exe"), // another app's WebView2
        ];
        let ours: Vec<u32> = descendants(10, &all).iter().map(|(pid, _)| *pid).collect();
        assert_eq!(ours, vec![10, 11, 14, 12, 13]);
        assert_eq!(descendants(99, &all).len(), 1); // nothing below: just itself
    }

    #[test]
    fn processes_by_group() {
        assert_eq!(group_of(10, 10, "bakin-desktop.exe"), Some(0));
        assert_eq!(group_of(11, 10, "Bakin-Engine.exe"), Some(1));
        assert_eq!(group_of(11, 10, "python.exe"), Some(1)); // the engine from the .venv
        assert_eq!(group_of(14, 10, "msedgewebview2.exe"), Some(2));
        assert_eq!(group_of(12, 10, "ffmpeg.exe"), Some(3));
        assert_eq!(group_of(15, 10, "conhost.exe"), None);
    }

    #[test]
    fn megabytes_with_one_decimal() {
        assert_eq!(mb(0), 0.0);
        assert_eq!(mb(1_048_576), 1.0);
        assert_eq!(mb(157_286_400 + 10_000), 150.0);
        assert_eq!(mb(157_286_400 + 60_000), 150.1);
        assert_eq!(mb(1_572_864), 1.5);
    }
}
