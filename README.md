# Bakin

> **En español:** Bakin es una app para Windows 10 y 11 que junta tu música y
> tus fondos de pantalla: baja fondos de Wallhaven, música y video de los sitios
> que soporta yt-dlp, y los reproduce y ordena en tu PC, sin cuentas ni nube.
> Descarga: [Releases](https://github.com/BladeHoly/bakin/releases/latest).

Your music and wallpaper library on your PC (Windows 10 and 11).

* **Wallpapers** from [wallhaven.cc](https://wallhaven.cc) through its public
  API: search by tags, pick for each monitor (vertical ones too), download
  with their tags. SFW by default, «sketchy» only when asked, never NSFW.
* **Music and video** from any site [yt-dlp](https://github.com/yt-dlp/yt-dlp)
  supports, with presets, a queue, a daily limit and download hours; a
  YouTube Music catalogue to browse and pick from.
* **A library** of what Bakin downloads plus your own folders: favourites,
  tags, a Recycle-Bin-safe delete, and conversions (images, audio, video).
* **Bakin Música**: a native player (Rust), playlists, mixes, even volume,
  an equalizer, synced lyrics, a mini player, and «En tu casa» to listen from
  your phone on the home network.

The interface is in Spanish and English.

Download: [latest release](https://github.com/BladeHoly/bakin/releases/latest).

## Layout

* `desktop/` — the Tauri 2 shell (Rust: window, tray, player, updater, home
  server) and the page (`desktop/ui`, built with glass-midas).
* `bakin/` — the Python engine (`python -m bakin.sidecar`): library, queue,
  yt-dlp, Wallhaven, conversions.
* `packaging/` — engine freeze, UI smoke test, release script, third-party
  notices (`notices.py`).
* `docs/` — architecture notes and QA scenarios.
* `.github/workflows/release.yml` — the build that produces every release.

## Develop

```powershell
python -m venv .venv; .venv\Scripts\pip install -e .[test]
.venv\Scripts\python -m pytest
cd desktop; npm install; npx tauri dev
```

## Build

On Windows 10/11 with Python 3.11 or newer, Node.js, stable Rust and Visual
Studio Build Tools («Desktop development with C++»):

```powershell
python -m venv .venv
.venv\Scripts\pip install -e . pyinstaller
.\packaging\build-bootloader.ps1   # PyInstaller's bootloader, built locally
.\packaging\build-engine.ps1       # the engine: desktop\src-tauri\engine-dist\bakin-engine
.venv\Scripts\python packaging\notices.py   # THIRD-PARTY-NOTICES.md
cd desktop; npm ci; npx tauri build
```

The installer ends up in `desktop\src-tauri\target\release\bundle\nsis\`.
To sign the update, `tauri build` needs the updater key in
`TAURI_SIGNING_PRIVATE_KEY`; without it, build with
`npx tauri build --no-bundle` or use `npx tauri dev`.

Published versions are not built on anyone's PC: the public
[`release.yml`](.github/workflows/release.yml) workflow builds them on
GitHub's runners and leaves a draft release that a person reviews and
publishes.

## License

Bakin's source code is under the **MIT** license ([`LICENSE`](LICENSE)).

The engine inside the installer (`bakin-engine.exe`) bundles
[mutagen](https://github.com/quodlibet/mutagen), which is GPL, so that
executable, as distributed, is under the **GPL-2.0 or later**
([`licenses/GPL-2.0.txt`](licenses/GPL-2.0.txt)); its complete source is this
repository. Every dependency's license is in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md), which is also installed
with the app. yt-dlp, ffmpeg and deno don't ship with Bakin: they are
downloaded from their official releases on your PC when needed.

## Open source and code signing

<!-- Pending SignPath Foundation's acceptance: when it arrives, move the
next line out of this comment and delete the «pending» line below it.
Free code signing provided by SignPath.io, certificate by SignPath Foundation
-->
*Code signing: application pending with SignPath Foundation.*

Code signing policy:

* Only versions built by this repository's public
  [`release.yml`](.github/workflows/release.yml) workflow, on GitHub's
  runners, from this repository's code, are signed.
* Roles — **Committer**, **Reviewer** and **Approver**:
  [BladeHoly](https://github.com/BladeHoly). Every signing request is
  approved by a person, and the workflow only leaves a draft release.
* Besides Authenticode, in-app updates carry their own signature (minisign),
  which Bakin verifies before installing.

## Privacy

Bakin has no accounts, no servers of its own and no telemetry. It only
connects when you ask it to or when you turned the option on:

* **Wallhaven** (wallhaven.cc): wallpaper searches and downloads; the API
  key, if you set one, only goes to its API.
* **Music and video sites** yt-dlp supports (YouTube, etc.): only the links
  you paste or pick.
* **YouTube Music catalogue**: when you search or open an artist or album.
* **Spotify**: only the public page of a link you paste, to read the track
  list.
* **MusicBrainz** and Cover Art Archive (if you turn it on): title, artist and
  length, to complete tags and covers.
* **LRCLIB** (if you turn it on): title, artist, album and length, for lyrics.
* **GitHub**: checking for Bakin updates and downloading yt-dlp, ffmpeg and
  deno.
* **«En tu casa»**: the server for listening from other devices only opens on
  your home network, and only when you turn it on.

Nothing else leaves your PC. See also [`SECURITY.md`](SECURITY.md) and the
extension's policy ([`extension/PRIVACY.md`](extension/PRIVACY.md)).
