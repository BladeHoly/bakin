//! Engine on demand (PLAN 0.12.1). Every 30 s this thread:
//! - lets the engine go when Bakin's window is closed (tray, small window)
//!   and nobody asked it anything for a minute, if the engine agrees it has
//!   nothing to do (`sleep_if_idle`): no job running or waiting;
//! - wakes it when it has something to do while asleep: a timed pause that
//!   ends (daily limit, YouTube pushback, download hours) or a follow coming
//!   due. The engine says when (`wake_at`) as it goes; checked against the
//!   wall clock, so a PC that slept through the time wakes it right after.
//! Anything else that needs it (the window, a link, a tray action) simply
//! calls it, and `Engine::call` starts it again.

use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::json;
use tauri::{AppHandle, Manager};

use crate::engine::Engine;

#[derive(Default)]
pub struct Scheduler {
    wake_at: Mutex<Option<f64>>,
}

impl Scheduler {
    pub fn set(&self, wake_at: Option<f64>) {
        *self.wake_at.lock().unwrap() = wake_at;
    }
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// A minute without calls, with the window closed, before asking it to sleep.
const QUIET: u64 = 60;

/// Whether to ask the engine to sleep.
pub fn may_sleep(window_open: bool, running: bool, quiet_for: u64) -> bool {
    !window_open && running && quiet_for >= QUIET
}

/// Whether it is time to wake the engine.
pub fn due(now: f64, wake_at: Option<f64>) -> bool {
    matches!(wake_at, Some(t) if now >= t)
}

pub fn start(app: AppHandle) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(30));
        crate::rotation::tick(&app); // backgrounds whose time has come (no engine needed)
        let st = app.state::<Scheduler>();
        let engine = app.state::<Engine>();
        let window_open = app.get_webview_window("main").is_some();
        if may_sleep(window_open, engine.running(), engine.quiet_for()) {
            if let Some(wake_at) = engine.sleep_if_idle() {
                st.set(wake_at);
            }
            continue;
        }
        let wake = *st.wake_at.lock().unwrap();
        if !due(now(), wake) {
            continue;
        }
        st.set(None);
        // lift the pause that is over, check the follows that are due (starts it)
        let _ = engine.call("resume_due", json!({}), Duration::from_secs(60));
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sleeps_only_with_the_window_closed_after_a_quiet_minute() {
        assert!(may_sleep(false, true, 60));
        assert!(!may_sleep(true, true, 600)); // the window is open
        assert!(!may_sleep(false, false, 600)); // already asleep
        assert!(!may_sleep(false, true, 59));
    }

    #[test]
    fn wakes_only_once_the_time_has_come() {
        assert!(!due(100.0, None));
        assert!(!due(100.0, Some(160.0)));
        assert!(due(160.0, Some(160.0)));
        assert!(due(1e9, Some(160.0))); // the PC slept through it: wake right away
    }
}
