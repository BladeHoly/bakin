# Security

## Reporting a problem

Use GitHub's private vulnerability reporting on this repository («Security» ›
«Report a vulnerability»). Please don't open a public issue for it.

## What Bakin keeps

* **No passwords and no accounts.** Wallhaven works without a key (public,
  SFW and «sketchy» content only, never NSFW). An **API key is optional**
  (to list and follow your own collections): it is kept in the Windows
  Credential Manager (`bakin-wallhaven`), never in a file, a log or the
  window; it is sent only to wallhaven.cc's API (`X-API-Key`), never to its
  image servers, and the log redactor scrubs it. «Quitar la clave» deletes it.
* **Cookies only if you choose them**, for the yt-dlp presets that need them.
  Imported cookies are kept encrypted with Windows DPAPI in Bakin's data
  folder (`vault\`), tied to your Windows user. yt-dlp gets a plain copy only
  while it runs; the copy is overwritten with zeros and deleted afterwards,
  and leftovers are swept at start. Cookies never go into downloads, logs,
  error messages, backups or the window; Bakin only reads their names,
  domains and expiry to warn when they stop working.
* **Backups** (`.zip`) never carry cookies, keys, the vault or logs. Importing
  one only adds: it never deletes, never turns on the home server, online
  lookups or another download folder, and presets it brings arrive with
  cookies off. Before importing, Bakin names the folders, follows and presets
  it would add.

## What leaves your PC

No telemetry and no servers of Bakin's own. Bakin connects only when you ask
or when you turned an option on:

* **Wallhaven** (wallhaven.cc): searches and downloads. Search thumbnails
  (`th.wallhaven.cc`) and YouTube thumbnails (`i.ytimg.com`) load straight
  into the window.
* **Sites yt-dlp supports** (YouTube, etc.): only the links you paste or pick.
  For YouTube, yt-dlp runs the site's own JavaScript challenge in **Deno**
  with no permissions (no files, no network of its own).
* **YouTube Music catalogue**: when you search or open an artist or album
  (no account).
* **Spotify**: only the public embed page of a link you paste, to read the
  track list. Spotify serves that page only to browsers, so Bakin presents a
  browser's User-Agent there; no account and no cookies.
* **MusicBrainz** and Cover Art Archive (if you turn it on): title, artist and
  length. **LRCLIB** (if you turn it on): title, artist, album and length.
* **GitHub**: Bakin's updates and the yt-dlp, ffmpeg and Deno downloads.

## «En tu casa» (the home server)

Off by default. When on, it listens only on private network addresses (never
the internet) and answers only devices that send the pairing code: 12
characters, random, kept encrypted in the vault, never one of your passwords.
«Cambiar el código» locks every phone out.

It speaks plain HTTP, like every Subsonic server: **use it only on a network
you trust.** Anyone on the same Wi-Fi who has the code, or captures a phone's
request, can listen to your library and send links that Bakin downloads.
Every link sent from a phone shows a Windows notification on the PC.

## Tools

yt-dlp, ffmpeg and Deno come from their projects' official GitHub releases,
checked against the SHA-256 sums published with each release before they
run. That protects against broken or swapped downloads, not against a
compromised release upstream: Bakin trusts those projects. If a tool isn't in
Bakin's folder, the one on your `PATH` is used, and Configuración says so.

## Logs

Logs are written to `%LOCALAPPDATA%\BakinData\logs\bakin.log`, outside the
repository, rotated at 2 MB. Every log record, including third-party library
logs and exception tracebacks, and the raw yt-dlp lines shown in the window,
goes through a redactor that removes session cookies (YouTube/Google ones
included), tokens, the pairing code and anything shaped like `Cookie:` or
`Authorization:` headers.

## Things Bakin will not do

Circumvent MFA, CAPTCHAs, bot checks or rate limits; rotate IPs or proxies;
store passwords. When a site says to slow down (429, «not a bot» checks),
Bakin stops and waits instead of retrying.

## Updates

In-app updates are signed (minisign): Bakin checks the signature before
installing, and never installs without «Actualizar ahora» or while a download
is running.
