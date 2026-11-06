//! The Python engine (`python -m bakin.sidecar`) as a child process, started
//! on demand: the first call starts it, and with the window closed and
//! nothing to do the shell lets it go (`sleep`, see background.rs) and wakes
//! it again when something needs it (a call, the scheduler in scheduler.rs).
//!
//! JSON lines both ways: requests carry an id and get one response; the engine
//! also pushes `{"event": …}` lines while a job runs, which are re-emitted to
//! the page as the `engine` event. Request bodies are never logged: they may
//! carry paths and cookie file locations.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};

fn now_secs() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

type Pending = Arc<Mutex<HashMap<u64, Sender<Value>>>>;

/// One running engine process.
struct Running {
    stdin: ChildStdin,
    child: Child,
}

pub struct Engine {
    app: AppHandle,
    inner: Mutex<Option<Running>>,
    pending: Pending,
    next_id: AtomicU64,
    /// Set while the shell lets it go on purpose: its exit is not an error.
    sleeping: Arc<AtomicBool>,
    /// When it was last asked something (unix seconds).
    last_call: AtomicU64,
}

/// How to start the engine.
struct Launch {
    program: PathBuf,
    args: Vec<&'static str>,
    cwd: PathBuf,
}

const PYTHON_ARGS: [&str; 4] = ["-X", "utf8", "-m", "bakin.sidecar"];

/// Where the engine is, in order:
/// 1. BAKIN_PYTHON (a Python with the `bakin` package), for testing;
/// 2. debug builds: the project's .venv (live code; Tauri also copies the
///    frozen engine next to debug builds, and that copy is usually stale);
/// 3. the frozen engine shipped with the app (resources/engine);
/// 4. release builds run from the repo: the .venv as a last resort.
fn find_engine(app: &AppHandle) -> Option<Launch> {
    if let Ok(p) = std::env::var("BAKIN_PYTHON") {
        let program = PathBuf::from(p);
        let cwd = program.parent()?.to_path_buf();
        return Some(Launch { program, args: PYTHON_ARGS.to_vec(), cwd });
    }
    if cfg!(debug_assertions) {
        if let Some(dev) = find_dev_venv() {
            return Some(dev);
        }
    }
    if let Ok(res) = app.path().resource_dir() {
        let exe = res.join("engine").join("bakin-engine.exe");
        if exe.is_file() {
            let cwd = exe.parent()?.to_path_buf();
            return Some(Launch { program: exe, args: vec![], cwd });
        }
    }
    find_dev_venv()
}

/// The project's .venv, found by walking up from this executable.
fn find_dev_venv() -> Option<Launch> {
    let exe = std::env::current_exe().ok()?;
    let mut dir: Option<&Path> = exe.parent();
    while let Some(d) = dir {
        let py = d.join(".venv").join("Scripts").join("python.exe");
        if py.is_file() && d.join("bakin").join("sidecar.py").is_file() {
            return Some(Launch { program: py, args: PYTHON_ARGS.to_vec(), cwd: d.to_path_buf() });
        }
        dir = d.parent();
    }
    None
}

impl Engine {
    /// Bakin starts with the engine running (the window opens right away).
    pub fn spawn(app: AppHandle) -> Engine {
        let engine = Engine {
            app,
            inner: Mutex::new(None),
            pending: Arc::new(Mutex::new(HashMap::new())),
            next_id: AtomicU64::new(1),
            sleeping: Arc::new(AtomicBool::new(false)),
            last_call: AtomicU64::new(now_secs()),
        };
        let _ = engine.ensure();
        engine
    }

    /// Whether the process is there right now.
    pub fn running(&self) -> bool {
        let mut inner = self.inner.lock().unwrap();
        match inner.as_mut() {
            Some(r) => matches!(r.child.try_wait(), Ok(None)),
            None => false,
        }
    }

    /// Starts the engine if it is not running (it was asleep, or it crashed).
    pub fn ensure(&self) -> Result<(), String> {
        let mut inner = self.inner.lock().unwrap();
        if let Some(r) = inner.as_mut() {
            if matches!(r.child.try_wait(), Ok(None)) {
                return Ok(());
            }
        }
        *inner = None;
        let running = self.start()?;
        *inner = Some(running);
        self.sleeping.store(false, Ordering::SeqCst);
        Ok(())
    }

    fn start(&self) -> Result<Running, String> {
        let Some(launch) = find_engine(&self.app) else {
            return Err("No se encontró el motor de Bakin. Reinstala la aplicación.".into());
        };
        let mut cmd = Command::new(launch.program);
        cmd.args(launch.args).current_dir(launch.cwd).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd.spawn().map_err(|e| format!("No se pudo iniciar el motor: {e}"))?;
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");
        let stdin = child.stdin.take().expect("piped stdin");

        // Responses and events.
        let pend = self.pending.clone();
        let app = self.app.clone();
        let sleeping = self.sleeping.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
                if let Some(id) = msg.get("id").and_then(Value::as_u64) {
                    if let Some(tx) = pend.lock().unwrap().remove(&id) {
                        let _ = tx.send(msg);
                    }
                } else if let Some(event) = msg.get("event") {
                    crate::background::engine_event(&app, event);
                    let _ = app.emit("engine", event.clone());
                } else if msg.get("ready").is_some() {
                    let _ = app.emit("engine-ready", msg.clone());
                }
            }
            // The engine exited: fail whatever is still waiting.
            for (_, tx) in pend.lock().unwrap().drain() {
                let _ = tx.send(json!({"error": "El motor se detuvo."}));
            }
            // let go on purpose (asleep): not something to tell the page
            if !sleeping.load(Ordering::SeqCst) {
                let _ = app.emit("engine-exit", ());
            }
        });
        // Keep stderr drained so the engine never blocks on a full pipe.
        // (Its real log is redacted and written to %LOCALAPPDATA%\BakinData\logs.)
        std::thread::spawn(move || for _ in BufReader::new(stderr).lines().map_while(Result::ok) {});
        Ok(Running { stdin, child })
    }

    /// Send one request and wait for its response (starting the engine first if needed).
    pub fn call(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        self.ensure()?;
        self.last_call.store(now_secs(), Ordering::Relaxed);
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = channel();
        self.pending.lock().unwrap().insert(id, tx);
        let line = json!({"id": id, "method": method, "params": params}).to_string();
        {
            let mut guard = self.inner.lock().unwrap();
            let r = guard.as_mut().ok_or("El motor no está disponible.")?;
            if r.stdin.write_all(line.as_bytes()).and_then(|_| r.stdin.write_all(b"\n")).and_then(|_| r.stdin.flush()).is_err() {
                self.pending.lock().unwrap().remove(&id);
                return Err("El motor no responde.".into());
            }
        }
        let msg = rx.recv_timeout(timeout).map_err(|_| {
            self.pending.lock().unwrap().remove(&id);
            "El motor tardó demasiado en responder.".to_string()
        })?;
        match msg.get("error") {
            Some(err) => Err(err.as_str().unwrap_or("error").to_string()),
            None => Ok(msg.get("result").cloned().unwrap_or(Value::Null)),
        }
    }

    /// Only if it is running: never starts the engine (the shell's cheap
    /// questions, e.g. a cover it does not have cached).
    pub fn call_if_running(&self, method: &str, params: Value, timeout: Duration) -> Option<Value> {
        if !self.running() {
            return None;
        }
        self.call(method, params, timeout).ok()
    }

    /// Ask the engine whether it may sleep; if so, let it go. Returns when to
    /// wake it (`Some(None)`: only when something needs it), or None if it
    /// stays (it is working, or it was not running).
    pub fn sleep_if_idle(&self) -> Option<Option<f64>> {
        if !self.running() {
            return None;
        }
        let last = self.last_call.load(Ordering::Relaxed);
        let r = self.call("sleep_if_idle", json!({}), Duration::from_secs(15)).ok();
        self.last_call.store(last, Ordering::Relaxed);
        let r = r?;
        if r.get("idle").and_then(Value::as_bool) != Some(true) {
            return None;
        }
        let wake_at = r.get("wake_at").and_then(Value::as_f64);
        self.let_go();
        Some(wake_at)
    }

    /// Close stdin: the engine saves and exits by itself (sidecar EOF).
    fn let_go(&self) {
        self.sleeping.store(true, Ordering::SeqCst);
        if let Some(mut r) = self.inner.lock().unwrap().take() {
            drop(r.stdin);
            std::thread::spawn(move || {
                for _ in 0..50 {
                    if let Ok(Some(_)) = r.child.try_wait() {
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                let _ = r.child.kill(); // it should have gone by itself in 5 s
            });
        }
    }

    /// Close stdin (the engine cancels any job and exits), then make sure.
    pub fn shutdown(&self) {
        self.sleeping.store(true, Ordering::SeqCst);
        if let Some(mut r) = self.inner.lock().unwrap().take() {
            drop(r.stdin);
            for _ in 0..30 {
                if let Ok(Some(_)) = r.child.try_wait() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = r.child.kill();
        }
    }

    /// Seconds since anything asked the engine something.
    pub fn quiet_for(&self) -> u64 {
        now_secs().saturating_sub(self.last_call.load(Ordering::Relaxed))
    }
}
