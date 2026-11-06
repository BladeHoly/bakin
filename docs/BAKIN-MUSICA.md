# Bakin Música

Turn the music Bakin downloads into a collection that feels like a streaming
service without depending on one: play it, clean it, keep it in sync, take it
to the phone. Planned on 2026-11-01 from what the best players do well.

## Rules

* **Light.** No new runtime: the window's own audio element plays (Opus, AAC,
  MP3, FLAC, WAV decode natively). Nothing runs on a timer: the player hangs
  off the audio element's events, draws nothing while the window is in the
  tray, and calls the engine only when a song changes or counts as played.
  Measured: 0.04 % CPU playing from the tray (muted test copy).
  (0.6.1 tried letting WebView2 rest in the tray; it stopped the music or
  reloaded the page, and 0.6.2 took it out: the background budget is to be
  met by a native player daemon, see docs/ARCHITECTURE-BACKGROUND.md.)
* **Never surprise.** Anything that rewrites files (tags, covers, duplicates)
  is proposed first, applied only on confirmation, and can be undone (old
  files to the Recycle Bin, old tags kept).
* **Private by default.** Lookups that send song data out (lyrics, MusicBrainz)
  are opt-in and send only title / artist / album / duration.

## What we take from whom

| From | Feature | Stage |
|---|---|---|
| Spotify | Play a list as the queue; «Agregar a la cola» plays next; shuffle that keeps the current song; repeat all / one; sleep timer (minutes or end of song); crossfade 0–12 s | **1 (done)** |
| Spotify / YouTube Music | Resume where you left off (queue, song, second, settings) | **1 (done)** |
| Windows (SMTC) | Media keys, volume flyout and lock screen controls with cover | **1 (done)** |
| foobar2000 | Gapless: the next song is already loaded when the current one ends | **1 (done, near-gapless)** |
| PotPlayer | Keyboard first: Space, ← →, Ctrl+← →, M, S, R, Q; playback speed | **1 (done)** |
| MusicBee / foobar2000 | Play counts and last played (half the song or 4 min, like Last.fm) | **1 (counting done)** |
| MusicBee | Views: Artists, Albums, Most played, Recently added, Recently played, Never played | **2 (done)** |
| MusicBee / foobar2000 | Smart playlists by rules (artist, title, album, plays, minutes, bitrate, added / played within, favourite, format) and normal playlists, exported as .m3u8 | **2 (done)** |
| Spotify / YouTube Music | «Mixes» from your own library: daily mix, forgotten favourites, to rediscover, radio of your top artists (same all day, seeded with the date) | **2 (done)** |
| MusicBrainz Picard | Clean titles («Artist - Song (Official Video) [4K]» → artist + title), look songs up on MusicBrainz, propose tags and the official cover, write on confirmation | 3 |
| dupeGuru / foobar2000 | Duplicates by tags + duration (and audio fingerprint later); keep the best copy (lossless > Opus > MP3), the rest to the Recycle Bin | 3 |
| foobar2000 | Volume normalization: EBU R128 / ReplayGain scanned once with ffmpeg in the background, applied while playing (track or album mode) | 4 |
| Spotify / YouTube Music | Synced lyrics (LRCLIB, free, opt-in), scrolling with the song | 4 |
| PotPlayer | A–B repeat, ±semitone pitch (practice) | 4 |
| Spotify | Mirror playlists: keep a folder equal to a YouTube playlist or your Likes (new songs downloaded by follows; removed ones to the Recycle Bin after asking) | 5 |
| Plex / Navidrome | Phone over the home network: Bakin serves the library, the phone plays or downloads in the browser (no cloud, no account; a code shown on the PC to pair) | 5 |

Left out on purpose: visualizers, skins, DSP chains, plugin systems,
streaming from services, social features.

## Stage 1 — the player (done)

* Bar at the bottom of the window (only while there is music): cover, title,
  artist · album, shuffle, previous, play/pause, next, repeat, position,
  queue, options, mute and volume.
* Click a song in the library: the list as it is (filter, search, sort)
  becomes the queue, from that song. Selection: «Reproducir» and «Agregar a
  la cola».
* Queue dialog: what's playing, what comes next (added songs first), play
  any of them now, remove, empty what follows.
* Options: sleep timer, crossfade, speed, shortcuts.
* Playing keeps Bakin in the tray when the window is closed.
* Engine: `music_tracks`, `music_cover`, `music_played`, `music_state_get/set`;
  data in `BakinData\music\plays.json` and `player.json`. An upgraded song
  (MP3 → Opus) keeps its plays, like it keeps favourites and tags.
* Covered by tests (test_player.py) and the UI smoke test (plays, next, and
  moves on by itself at the end of a song).

## Stage 2 — the library as a music app (done)

* «Música» card in the library sidebar: Canciones, Artistas, Álbumes, Más
  escuchadas, Escuchadas hace poco, Agregadas hace poco, Nunca escuchadas;
  then Mixes and Playlists.
* Every view is a library list (`library_items` / `music_tracks` with a
  `music` filter, one query in the engine), so search, selection and playing
  behave the same, and «▶ Reproducir» plays exactly what is on screen.
* Artists and albums are grids of cards with lazily loaded covers; a card
  opens its songs, with «← Artistas» / «← Álbumes» back.
* Playlists: `BakinData\music\playlists.json`. Normal ones keep ids in order
  («Agregar a playlist…», «Quitar de la playlist»); smart ones keep rules,
  previewed live while edited. Export writes .m3u8 where the user chooses.
  Deleting offers «Deshacer».
* Mixes need 5+ songs; with no history yet only «Mix del día» and «Para
  redescubrir» show, the rest appear as plays build up.
* The player restores its queue without loading the song (lighter start, and
  the library folders are only readable once the library is listed).
* Tests: test_music_views.py; the UI smoke test opens Artistas, an artist and
  plays it.
