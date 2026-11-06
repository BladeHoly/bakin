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

#[tauri::command]
pub fn usage() -> Value {
    let me = std::process::id();
    let all = processes();
    // Bakin and everything below it
    let mut ours = vec![(me, "bakin-desktop.exe".to_string())];
    let mut i = 0;
    while i < ours.len() {
        let parent = ours[i].0;
        for (pid, ppid, name) in &all {
            if *ppid == parent && !ours.iter().any(|(p, _)| p == pid) {
                ours.push((*pid, name.clone()));
            }
        }
        i += 1;
    }
    let mut groups: Vec<(&str, u64)> = vec![("Bakin y el reproductor", 0), ("Motor de descargas", 0), ("Ventana", 0), ("Herramientas", 0)];
    for (pid, name) in &ours {
        let n = name.to_lowercase();
        if n == "conhost.exe" {
            continue;
        }
        let Some(bytes) = private_ws(*pid) else { continue };
        let g = if *pid == me {
            0
        } else if n.starts_with("bakin-engine") || n.starts_with("python") {
            1
        } else if n.starts_with("msedgewebview2") {
            2
        } else {
            3
        };
        groups[g].1 += bytes;
    }
    let mb = |b: u64| (b as f64 / 1_048_576.0 * 10.0).round() / 10.0;
    let total: u64 = groups.iter().map(|g| g.1).sum();
    json!({
        "total_mb": mb(total),
        "groups": groups.iter().map(|(name, b)| json!({ "name": name, "mb": mb(*b) })).collect::<Vec<_>>(),
    })
}
