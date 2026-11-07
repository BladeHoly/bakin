//! The keyboard's media keys and Windows' media overlay (volume flyout, lock
//! screen) for the native player: SystemMediaTransportControls, tied to a
//! small hidden window of their own on a thread with a message loop, so they
//! work while Bakin's window doesn't exist (it is closed in the tray).
//! Button presses become player commands; the player's state (song, cover,
//! playing or paused) is shown there.

use std::sync::Mutex;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager};
use windows::core::HSTRING;
use windows::Foundation::TypedEventHandler;
use windows::Media::{
    MediaPlaybackStatus, MediaPlaybackType, SystemMediaTransportControls, SystemMediaTransportControlsButton,
    SystemMediaTransportControlsButtonPressedEventArgs,
};
use windows::Storage::Streams::{DataWriter, InMemoryRandomAccessStream, RandomAccessStreamReference};
use windows::Win32::Foundation::HWND;
use windows::Win32::System::WinRT::ISystemMediaTransportControlsInterop;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DispatchMessageW, GetMessageW, TranslateMessage, MSG, WINDOW_EX_STYLE, WS_POPUP,
};

use crate::player::Player;

/// Stopped with no song, else playing or paused as the player says (paused when it doesn't).
fn status_of(state: &Value) -> MediaPlaybackStatus {
    match (state.get("track").is_some_and(|t| !t.is_null()), state.get("playing").and_then(|p| p.as_bool())) {
        (false, _) => MediaPlaybackStatus::Stopped,
        (true, Some(true)) => MediaPlaybackStatus::Playing,
        _ => MediaPlaybackStatus::Paused,
    }
}

fn track_text(track: &Value, key: &str) -> String {
    track.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

/// The song's title, or its file name when it has none.
fn title_of(track: &Value) -> String {
    let title = track_text(track, "title");
    if title.is_empty() { track_text(track, "name") } else { title }
}

pub struct Smtc {
    controls: SystemMediaTransportControls,
    shown: Mutex<String>, // id of the song on display
}

// The controls are an agile WinRT object: usable from any thread.
unsafe impl Send for Smtc {}
unsafe impl Sync for Smtc {}

impl Smtc {
    pub fn create(app: &AppHandle) -> Option<Smtc> {
        // a hidden window and its message loop, on their own thread for good
        let (tx, rx) = std::sync::mpsc::channel::<Option<SystemMediaTransportControls>>();
        std::thread::Builder::new()
            .name("bakin-media-keys".into())
            .spawn(move || unsafe {
                let hwnd = CreateWindowExW(
                    WINDOW_EX_STYLE(0x80), // WS_EX_TOOLWINDOW: never in the taskbar
                    windows::core::w!("STATIC"),
                    windows::core::w!("Bakin"),
                    WS_POPUP,
                    0, 0, 0, 0,
                    None, None, None, None,
                );
                let controls = hwnd.ok().and_then(|h: HWND| {
                    let interop = windows::core::factory::<SystemMediaTransportControls, ISystemMediaTransportControlsInterop>().ok()?;
                    interop.GetForWindow::<SystemMediaTransportControls>(h).ok()
                });
                let ok = controls.is_some();
                let _ = tx.send(controls);
                if !ok {
                    return;
                }
                let mut msg = MSG::default();
                while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                    let _ = TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
            })
            .ok()?;
        let controls = rx.recv().ok()??;
        let _ = controls.SetIsEnabled(true);
        let _ = controls.SetIsPlayEnabled(true);
        let _ = controls.SetIsPauseEnabled(true);
        let _ = controls.SetIsNextEnabled(true);
        let _ = controls.SetIsPreviousEnabled(true);
        let app = app.clone();
        let handler = TypedEventHandler::<SystemMediaTransportControls, SystemMediaTransportControlsButtonPressedEventArgs>::new(
            move |_, args| {
                if let Some(args) = args.as_ref() {
                    let cmd = match args.Button()? {
                        SystemMediaTransportControlsButton::Play => "play",
                        SystemMediaTransportControlsButton::Pause => "pause",
                        SystemMediaTransportControlsButton::Next => "next",
                        SystemMediaTransportControlsButton::Previous => "prev",
                        _ => return Ok(()),
                    };
                    let app = app.clone();
                    std::thread::spawn(move || {
                        let _ = app.state::<Player>().call(json!({ "cmd": cmd }));
                    });
                }
                Ok(())
            },
        );
        controls.ButtonPressed(&handler).ok()?;
        Some(Smtc { controls, shown: Mutex::new(String::new()) })
    }

    /// Shows the player's state (a "state" event).
    pub fn update(&self, app: &AppHandle, state: &Value) {
        let _ = self.controls.SetPlaybackStatus(status_of(state));
        let Some(track) = state.get("track").filter(|t| !t.is_null()) else { return };
        let id = track.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        {
            let mut shown = self.shown.lock().unwrap();
            if *shown == id {
                return;
            }
            *shown = id.clone();
        }
        let text = |k: &str| track_text(track, k);
        let title = title_of(track);
        if let Ok(updater) = self.controls.DisplayUpdater() {
            let _ = updater.SetType(MediaPlaybackType::Music);
            if let Ok(props) = updater.MusicProperties() {
                let _ = props.SetTitle(&HSTRING::from(title));
                let _ = props.SetArtist(&HSTRING::from(text("artist")));
                let _ = props.SetAlbumTitle(&HSTRING::from(text("album")));
            }
            let _ = updater.Update();
        }
        // the cover the library cached (or the engine, if it is awake), off this thread
        let app = app.clone();
        let path = text("path");
        std::thread::spawn(move || {
            let Some(bytes) = crate::covers::cover(&app, &id, &path) else { return };
            let Some(smtc) = app.try_state::<Smtc>() else { return };
            if *smtc.shown.lock().unwrap() != id {
                return; // another song by now
            }
            let _ = smtc.set_cover(&bytes);
        });
    }

    fn set_cover(&self, jpeg: &[u8]) -> windows::core::Result<()> {
        let stream = InMemoryRandomAccessStream::new()?;
        let writer = DataWriter::CreateDataWriter(&stream.GetOutputStreamAt(0)?)?;
        writer.WriteBytes(jpeg)?;
        writer.StoreAsync()?.join()?;
        writer.FlushAsync()?.join()?;
        writer.DetachStream()?;
        let updater = self.controls.DisplayUpdater()?;
        updater.SetThumbnail(&RandomAccessStreamReference::CreateFromStream(&stream)?)?;
        updater.Update()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn what_windows_shows() {
        assert_eq!(status_of(&json!({})), MediaPlaybackStatus::Stopped);
        assert_eq!(status_of(&json!({"track": null, "playing": true})), MediaPlaybackStatus::Stopped);
        assert_eq!(status_of(&json!({"track": {"id": "a"}, "playing": true})), MediaPlaybackStatus::Playing);
        assert_eq!(status_of(&json!({"track": {"id": "a"}, "playing": false})), MediaPlaybackStatus::Paused);
        assert_eq!(status_of(&json!({"track": {"id": "a"}})), MediaPlaybackStatus::Paused);
    }

    #[test]
    fn a_song_without_a_title_shows_its_name() {
        assert_eq!(title_of(&json!({"title": "Tema", "name": "tema.opus"})), "Tema");
        assert_eq!(title_of(&json!({"title": "", "name": "tema.opus"})), "tema.opus");
        assert_eq!(title_of(&json!({"title": 3})), "");
        assert_eq!(track_text(&json!({"artist": "X"}), "album"), "");
    }
}
