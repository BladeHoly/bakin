# Bakin in the background: one small controller, the rest on demand

## Where we are (0.6.2, measured on the user's PC, Task Manager numbers)

| Process | Memory | Needed in the tray? |
|---|---|---|
| WebView2 (browser, GPU, page…) | ~450 MB | No: nobody is looking at it |
| bakin-engine (Python) | ~55–64 MB | Only while downloading / checking follows |
| Bakin (Rust shell) | ~7.5 MB | Yes: tray, scheduler |

Music playing from the tray costs ~520 MB, almost all of it for a window
nobody sees. 0.6.1 tried to make WebView2 rest (not visible, low-memory
mode): the music stopped after a while or the page reloaded and lost its
state, so 0.6.2 took it out. A hidden WebView cannot be trusted to keep
state; the state has to live outside it.

## The design

```
Bakin (Rust shell)  — always on, the controller
 ├─ tray, window, single instance, bakin:// links
 ├─ scheduler: next download hour, follow checks, timed pauses, sleep timer
 ├─ player (in-process, native)        ← new: plays even with no window
 │    decode: Symphonia (MP3, AAC, FLAC, Vorbis, WAV) + libopus (Opus)
 │    output: WASAPI; gapless by design (one continuous stream)
 │    Windows media keys and overlay: SystemMediaTransportControls
 ├─ bakin-engine (Python)  — started when there is work, stopped when idle
 └─ window (WebView2)       — exists only while it is open
```

* **The player lives in the shell process**, not in the page: closing the
  window never touches the music, and the page is only a remote control.
  Same commands as today (play, pause, seek, next, previous, shuffle, repeat,
  crossfade, speed, sleep timer); the page draws what the player reports.
* **The window is destroyed in the tray** (after a short grace period, so
  closing and reopening quickly is instant). Reopening creates it again and
  it asks the player and the engine for their state: nothing to lose, because
  nothing lives only in the page. This removes the whole class of R1 bugs.
* **The engine starts on demand**: when the window is open, when the
  download queue has work or its hour comes, when a follow is due. Before
  stopping it tells the shell when it next needs to wake up. Play counts and
  the player state are written by the shell (the engine reads them).
* **One process to look at**: shell + player are one process ("Bakin"); the
  engine is its child, grouped under it; WebView2 only exists while the
  window is open. Settings shows the honest total of everything Bakin runs.

## Expected budget

| Situation | Processes | Estimate |
|---|---|---|
| Tray, idle | Bakin | ~10 MB |
| Tray, music | Bakin (+ player buffers) | ~15–25 MB |
| Tray, music + downloads | Bakin + engine (+ yt-dlp while it runs) | ~80–100 MB |
| Window open | + WebView2 | as today (~150–450 MB) |

Within the 150 MB ceiling and close to the 75 MB aim even while downloading.

## Steps

1. **Done (0.7)**: native player in the shell (`src/player.rs`): Symphonia
   + libopus (built from source with CMake; it needs a short build path),
   cpal/WASAPI output, rubato FFT resampling with exact frame accounting
   (a 1 s 44.1 kHz song is exactly 48000 frames at 48 kHz: no gap, no
   overlap), queue / shuffle / repeat / crossfade / sleep timer in the
   player, its own state file plus a small position file every 5 s, media
   keys and the Windows overlay through SystemMediaTransportControls
   (`src/smtc.rs`). No output device (headphones off): it says so, waits and
   starts when one appears; a lost device pauses and resumes. `BAKIN_MUTE`
   gives test copies a silent real-time output. Measured: Opus decodes at
   ~230x real time, 3 MB with the output open. Speed control was dropped
   (changing it without changing the pitch is not worth its cost).
2. **Done (0.7)**: 20 s after the window goes to the tray it is destroyed
   (WebView2 exits with it); opening Bakin builds it again from the config
   and the page asks the player and the engine for their state. The media
   keys live on a hidden window of their own (a thread with a message
   loop); the shell sends the notifications the page would have sent
   (queue finished or paused, cookies) and keeps `bakin://` and copied
   links for the next page. Closing the last window doesn't exit while
   there is music or downloads (KeepAlive). Measured (debug build, the
   user's 3500-file library): window open with music 252 MB; 35 s in the
   tray with music **58.7 MB** (shell with the player 11.6 MB, engine
   46.5 MB). The UI test checks WebView2 is gone, the music goes on through
   the player's position file, reopening shows the same song playing, and
   a 150 MB budget (41 MB in the test).
3. **Done (0.12.1).** Engine on demand with a wake-up time held by the
   shell (`scheduler.rs`): every 30 s, with no main window and a minute
   without calls, the shell asks `sleep_if_idle`; the engine says no while
   anything runs or waits, else saves the queue and answers `wake_at`
   (end of a timed pause — download hours, daily limit, YouTube — or the
   next follow), and the shell closes its stdin. Any call starts it again
   (`Engine::call` → `ensure`); `show_main` starts it while the page loads;
   at `wake_at` the scheduler calls `resume_due`. Plays counted while it
   sleeps go to `music/plays-pending.jsonl` (merged by `music.plays()`);
   covers for the media overlay and the small window come from the engine's
   thumbnail cache (`covers.rs`). Tests: pytest `sleep_if_idle`,
   `resume_due`, pending plays; cargo `scheduler`, `covers`; UI smoke C43.
4. Budgets in `release.ps1`: launch, play from the tray for a minute,
   measure every Bakin process; a release over budget does not go out.
