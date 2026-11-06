//! A small OpenSubsonic server for the home network (PLAN 0.21.1): any
//! Subsonic app on the phone (Symfonium, Tempo, DSub, Ultrasonic…) can
//! browse, stream and star the music library with user `bakin` and the
//! pairing code as password.
//!
//! Safety first: it listens only on this PC's private IPv4 addresses
//! (RFC 1918) and loopback, never on 0.0.0.0, and refuses any peer that is
//! not private either. Failed logins are rate-limited per address. The
//! password, tokens and salts are never logged.
//!
//! The data comes from a `Library` (the engine, through the integrator):
//! songs and playlists in one snapshot, cached for a short while; albums
//! and artists are derived from the songs. No transcoding: files are served
//! as they are, with `Range` support.

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::extract::{ConnectInfo, Request, State};
use axum::http::{header, HeaderMap, HeaderValue, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Router;
use base64::Engine as _;
use serde_json::{json, Map, Value};

// ---------------------------------------------------------------- interface

#[derive(Clone, Debug)]
pub struct Song {
    pub id: String,
    pub path: PathBuf,
    pub title: String,
    pub artist: String,
    pub album: String,
    /// Seconds.
    pub duration: u32,
    pub track: Option<u32>,
    pub year: Option<u32>,
    pub size: u64,
    pub suffix: String,
    /// kbit/s.
    pub bitrate: Option<u32>,
    pub starred: bool,
    pub plays: u32,
    /// When it was added (unix seconds).
    pub added: f64,
}

#[derive(Clone, Debug)]
pub struct Playlist {
    pub id: String,
    pub name: String,
    pub song_ids: Vec<String>,
}

#[derive(Clone, Debug, Default)]
pub struct Snapshot {
    pub songs: Vec<Song>,
    pub playlists: Vec<Playlist>,
}

/// What the server needs from Bakin. Every call may block (it may ask the
/// engine), so the server always runs them on the blocking pool.
pub trait Library: Send + Sync + 'static {
    /// The whole library; cached for `CACHE_TTL`.
    fn snapshot(&self) -> Result<Snapshot, String>;
    fn star(&self, song_id: &str, on: bool) -> Result<(), String>;
    /// A finished play.
    fn scrobble(&self, song_id: &str) -> Result<(), String>;
    /// A JPEG of the song's cover; `size` is the largest side in pixels,
    /// 0 when the client did not ask for one (the largest available).
    fn cover(&self, song_id: &str, size: u32) -> Option<Vec<u8>>;
    /// The song's lyrics as LRC text (synced or plain).
    fn lyrics(&self, song_id: &str) -> Option<String>;
    /// «Mandar a Bakin» from the phone (PLAN 0.21.3): a link for the PC's download hub, where
    /// the user still confirms it.
    fn link(&self, _url: &str) -> Result<(), String> {
        Err("not supported".into())
    }
}

// The phone page (PLAN 0.21.2): one HTML file plus glass-midas' styles and the icon.
const PWA_INDEX: &str = include_str!("../pwa/index.html");
const PWA_MANIFEST: &str = include_str!("../pwa/manifest.webmanifest");
const PWA_CSS: &str = include_str!("../../ui/vendor/glass-midas/components/styles.css");
const PWA_ICON: &[u8] = include_bytes!("../icons/128x128@2x.png");

/// The page's files; they hold no data, so they're served before the login (the page asks for
/// the code itself).
fn pwa_file(path: &str) -> Option<Response> {
    let (kind, body): (&str, Body) = match path {
        "/app/" | "/app/index.html" => ("text/html; charset=utf-8", PWA_INDEX.into()),
        "/app/manifest.webmanifest" => ("application/manifest+json", PWA_MANIFEST.into()),
        "/app/gm.css" => ("text/css; charset=utf-8", PWA_CSS.into()),
        "/app/icon.png" => ("image/png", Body::from(PWA_ICON)),
        "/" | "/app" => return Some((StatusCode::FOUND, [(header::LOCATION, "/app/")]).into_response()),
        _ => return None,
    };
    Some(([(header::CONTENT_TYPE, kind), (header::CACHE_CONTROL, "no-cache")], body).into_response())
}

/// A running server. Dropping it (or `stop`) closes the port.
pub struct LanServer {
    rt: Option<tokio::runtime::Runtime>,
    addrs: Vec<SocketAddr>,
}

impl LanServer {
    /// Listens on `port` on each private IPv4 address of this PC and on
    /// 127.0.0.1 (only loopback when there is no private address, e.g. on a
    /// public wifi). Port 0 picks a free port per address (tests).
    pub fn start(port: u16, password: String, lib: Arc<dyn Library>) -> Result<LanServer, String> {
        // test copies (BAKIN_HOME + BAKIN_LAN_LOOPBACK) stay on this PC: no firewall prompt
        let test_copy = std::env::var_os("BAKIN_HOME").is_some() && std::env::var_os("BAKIN_LAN_LOOPBACK").is_some();
        let mut ips = if test_copy { Vec::new() } else { private_ipv4s() };
        ips.push(Ipv4Addr::LOCALHOST);
        let mut listeners = Vec::new();
        for ip in ips {
            debug_assert!(is_private(IpAddr::V4(ip)));
            let l = std::net::TcpListener::bind((ip, port)).map_err(|e| format!("{ip}:{port}: {e}"))?;
            l.set_nonblocking(true).map_err(|e| e.to_string())?;
            listeners.push(l);
        }
        let addrs = listeners.iter().filter_map(|l| l.local_addr().ok()).collect();
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .thread_name("bakin-lan")
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        let app = router(Arc::new(Shared::new(password, lib)));
        for l in listeners {
            let app = app.clone();
            rt.spawn(async move {
                if let Ok(l) = tokio::net::TcpListener::from_std(l) {
                    let _ = axum::serve(l, app.into_make_service_with_connect_info::<SocketAddr>()).await;
                }
            });
        }
        Ok(LanServer { rt: Some(rt), addrs })
    }

    pub fn addresses(&self) -> Vec<SocketAddr> {
        self.addrs.clone()
    }

    /// Closes the listeners and drops every open connection.
    pub fn stop(self) {}
}

impl Drop for LanServer {
    fn drop(&mut self) {
        // shutdown_background: safe from async code too (a plain drop of a
        // runtime panics there); the tasks, and with them the sockets, go
        // away right after.
        if let Some(rt) = self.rt.take() {
            rt.shutdown_background();
        }
    }
}

/// This PC's RFC 1918 addresses (10/8, 172.16/12, 192.168/16), sorted.
pub fn private_ipv4s() -> Vec<Ipv4Addr> {
    let mut out: Vec<Ipv4Addr> = if_addrs::get_if_addrs()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|i| match i.ip() {
            IpAddr::V4(v4) if v4.is_private() => Some(v4),
            _ => None,
        })
        .collect();
    out.sort();
    out.dedup();
    out
}

/// RFC 1918 or loopback (IPv4, or IPv6 loopback / IPv4-mapped).
pub fn is_private(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => v4.is_private() || v4.is_loopback(),
        IpAddr::V6(v6) => v6.is_loopback() || v6.to_ipv4_mapped().is_some_and(|v4| v4.is_private() || v4.is_loopback()),
    }
}

// ------------------------------------------------------------------ state

const USER: &str = "bakin";
const API_VERSION: &str = "1.16.1";
const CACHE_TTL: Duration = Duration::from_secs(30);
const MAX_FAILURES: u32 = 10;
const FAIL_WINDOW: Duration = Duration::from_secs(60);
const UNKNOWN_ARTIST: &str = "Artista desconocido";
const UNKNOWN_ALBUM: &str = "Sin álbum";
const IGNORED_ARTICLES: &str = "The El La Los Las Le Les";

struct Failures {
    since: Instant,
    count: u32,
}

struct Shared {
    lib: Arc<dyn Library>,
    password: String,
    /// An async lock so concurrent requests wait for one snapshot.
    index: tokio::sync::Mutex<Option<(Instant, Arc<Index>)>>,
    failures: Mutex<HashMap<IpAddr, Failures>>,
}

impl Shared {
    fn new(password: String, lib: Arc<dyn Library>) -> Shared {
        Shared { lib, password, index: tokio::sync::Mutex::new(None), failures: Mutex::new(HashMap::new()) }
    }

    async fn index(&self) -> Result<Arc<Index>, String> {
        let mut slot = self.index.lock().await;
        if let Some((at, idx)) = slot.as_ref() {
            if at.elapsed() < CACHE_TTL {
                return Ok(idx.clone());
            }
        }
        let lib = self.lib.clone();
        let snap = tokio::task::spawn_blocking(move || lib.snapshot()).await.map_err(|e| e.to_string())??;
        let idx = Arc::new(Index::build(snap));
        *slot = Some((Instant::now(), idx.clone()));
        Ok(idx)
    }

    /// After a change (star, scrobble) the next request reads it again.
    async fn forget(&self) {
        *self.index.lock().await = None;
    }

    fn blocked(&self, ip: IpAddr) -> bool {
        let map = self.failures.lock().unwrap();
        map.get(&ip).is_some_and(|f| f.count >= MAX_FAILURES && f.since.elapsed() < FAIL_WINDOW)
    }

    fn failed(&self, ip: IpAddr) {
        let mut map = self.failures.lock().unwrap();
        map.retain(|_, f| f.since.elapsed() < FAIL_WINDOW);
        let f = map.entry(ip).or_insert(Failures { since: Instant::now(), count: 0 });
        f.count += 1;
        if f.count == MAX_FAILURES {
            // blocked for a full minute from the last allowed try
            f.since = Instant::now();
        }
    }
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Option<T> {
    tokio::task::spawn_blocking(f).await.ok()
}

// ------------------------------------------------------------------ index

struct Album {
    id: String,
    name: String,
    artist: String,
    artist_id: String,
    songs: Vec<usize>,
    year: Option<u32>,
    duration: u64,
    /// The newest song's `added`.
    created: f64,
    plays: u64,
    /// Any song starred (Bakin stars songs, not albums).
    starred: bool,
}

struct Artist {
    id: String,
    name: String,
    albums: Vec<usize>,
}

/// The snapshot with albums and artists derived and lookups by id.
struct Index {
    songs: Vec<Song>,
    song_album: Vec<usize>,
    by_song: HashMap<String, usize>,
    albums: Vec<Album>,
    by_album: HashMap<String, usize>,
    artists: Vec<Artist>,
    by_artist: HashMap<String, usize>,
    playlists: Vec<Playlist>,
}

fn fold(s: &str) -> String {
    s.to_lowercase()
}

fn short_hash(s: &str) -> String {
    sha1_smol::Sha1::from(s).digest().to_string()[..12].to_string()
}

/// The first credited artist: «A, B», «A feat. B», «A & B» → «A».
fn main_artist(artist: &str) -> &str {
    // ASCII lowercase keeps byte offsets, so positions carry over
    let lower = artist.to_ascii_lowercase();
    let end = [", ", " feat. ", " feat ", " ft. ", " & ", "; "]
        .iter()
        .filter_map(|sep| lower.find(sep))
        .min()
        .unwrap_or(artist.len());
    let main = artist[..end].trim();
    if main.is_empty() { UNKNOWN_ARTIST } else { main }
}

fn artist_id(main: &str) -> String {
    format!("ar-{}", short_hash(&fold(main)))
}

fn album_id(album: &str, main: &str) -> String {
    format!("al-{}", short_hash(&format!("{}\0{}", fold(album), fold(main))))
}

fn song_id(lib_id: &str) -> String {
    format!("so-{}", base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(lib_id))
}

/// Back to the library id; a raw library id passes through.
fn song_lib_id(id: &str) -> String {
    id.strip_prefix("so-")
        .and_then(|b| base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(b).ok())
        .and_then(|v| String::from_utf8(v).ok())
        .unwrap_or_else(|| id.to_string())
}

impl Index {
    fn build(snap: Snapshot) -> Index {
        let mut idx = Index {
            songs: Vec::new(),
            song_album: Vec::new(),
            by_song: HashMap::new(),
            albums: Vec::new(),
            by_album: HashMap::new(),
            artists: Vec::new(),
            by_artist: HashMap::new(),
            playlists: snap.playlists,
        };
        for song in snap.songs {
            let i = idx.songs.len();
            let main = main_artist(&song.artist).to_string();
            let arid = artist_id(&main);
            let ar = match idx.by_artist.get(&arid) {
                Some(&ar) => ar,
                None => {
                    idx.artists.push(Artist { id: arid.clone(), name: main.clone(), albums: Vec::new() });
                    idx.by_artist.insert(arid.clone(), idx.artists.len() - 1);
                    idx.artists.len() - 1
                }
            };
            let name = if song.album.trim().is_empty() { UNKNOWN_ALBUM.to_string() } else { song.album.clone() };
            let alid = album_id(&name, &main);
            let al = match idx.by_album.get(&alid) {
                Some(&al) => al,
                None => {
                    idx.albums.push(Album {
                        id: alid.clone(),
                        name,
                        artist: idx.artists[ar].name.clone(),
                        artist_id: arid,
                        songs: Vec::new(),
                        year: None,
                        duration: 0,
                        created: 0.0,
                        plays: 0,
                        starred: false,
                    });
                    let al = idx.albums.len() - 1;
                    idx.by_album.insert(alid, al);
                    idx.artists[ar].albums.push(al);
                    al
                }
            };
            let a = &mut idx.albums[al];
            a.songs.push(i);
            a.year = a.year.or(song.year);
            a.duration += song.duration as u64;
            a.created = a.created.max(song.added);
            a.plays += song.plays as u64;
            a.starred |= song.starred;
            idx.by_song.insert(song.id.clone(), i);
            idx.song_album.push(al);
            idx.songs.push(song);
        }
        let songs = &idx.songs;
        for a in &mut idx.albums {
            a.songs.sort_by_key(|&i| (songs[i].track.unwrap_or(u32::MAX), fold(&songs[i].title)));
        }
        let albums = &idx.albums;
        for ar in &mut idx.artists {
            ar.albums.sort_by_key(|&i| (albums[i].year.unwrap_or(0), fold(&albums[i].name)));
        }
        idx
    }

    fn song(&self, id: &str) -> Option<usize> {
        self.by_song.get(&song_lib_id(id)).copied()
    }

    fn album(&self, id: &str) -> Option<&Album> {
        self.by_album.get(id).map(|&i| &self.albums[i])
    }

    fn artist(&self, id: &str) -> Option<&Artist> {
        self.by_artist.get(id).map(|&i| &self.artists[i])
    }

    fn playlist(&self, id: &str) -> Option<&Playlist> {
        let id = id.strip_prefix("pl-").unwrap_or(id);
        self.playlists.iter().find(|p| p.id == id)
    }

    fn playlist_songs(&self, p: &Playlist) -> Vec<usize> {
        p.song_ids.iter().filter_map(|id| self.by_song.get(id).copied()).collect()
    }

    /// A song that stands for any id (song, album, artist, playlist): for covers.
    fn representative(&self, id: &str) -> Option<usize> {
        if id.starts_with("al-") {
            return self.album(id)?.songs.first().copied();
        }
        if id.starts_with("ar-") {
            let ar = self.artist(id)?;
            return ar.albums.iter().find_map(|&a| self.albums[a].songs.first().copied());
        }
        if id.starts_with("pl-") {
            return self.playlist_songs(self.playlist(id)?).first().copied();
        }
        self.song(id)
    }

    fn child(&self, i: usize) -> Value {
        let s = &self.songs[i];
        let al = &self.albums[self.song_album[i]];
        let file = s.path.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default();
        let mut v = json!({
            "id": song_id(&s.id),
            "parent": al.id,
            "isDir": false,
            "title": s.title,
            "album": s.album,
            "artist": s.artist,
            "coverArt": al.id,
            "size": s.size,
            "contentType": content_type(&s.suffix),
            "suffix": s.suffix,
            "duration": s.duration,
            "path": format!("{}/{}/{}", al.artist, al.name, file),
            "playCount": s.plays,
            "created": iso(s.added),
            "albumId": al.id,
            "artistId": al.artist_id,
            "type": "music",
            "mediaType": "song",
        });
        if let Some(t) = s.track {
            v["track"] = t.into();
        }
        if let Some(y) = s.year {
            v["year"] = y.into();
        }
        if let Some(b) = s.bitrate {
            v["bitRate"] = b.into();
        }
        if s.starred {
            // Bakin keeps no star time; the date added stands in
            v["starred"] = iso(s.added).into();
        }
        v
    }

    fn album_json(&self, a: &Album) -> Value {
        let mut v = json!({
            "id": a.id,
            "name": a.name,
            "artist": a.artist,
            "artistId": a.artist_id,
            "coverArt": a.id,
            "songCount": a.songs.len(),
            "duration": a.duration,
            "playCount": a.plays,
            "created": iso(a.created),
        });
        if let Some(y) = a.year {
            v["year"] = y.into();
        }
        if a.starred {
            v["starred"] = iso(a.created).into();
        }
        v
    }

    fn artist_json(&self, ar: &Artist) -> Value {
        json!({ "id": ar.id, "name": ar.name, "albumCount": ar.albums.len(), "coverArt": ar.id })
    }

    /// Artists grouped by initial, as getIndexes / getArtists want them.
    fn indexes(&self) -> Vec<Value> {
        let mut sorted: Vec<&Artist> = self.artists.iter().collect();
        sorted.sort_by_key(|a| fold(&a.name));
        let mut groups: Vec<(String, Vec<Value>)> = Vec::new();
        for ar in sorted {
            let letter = ar
                .name
                .chars()
                .next()
                .filter(|c| c.is_alphabetic())
                .map(|c| c.to_uppercase().collect::<String>())
                .unwrap_or_else(|| "#".into());
            match groups.last_mut() {
                Some((l, list)) if *l == letter => list.push(self.artist_json(ar)),
                _ => groups.push((letter, vec![self.artist_json(ar)])),
            }
        }
        groups.into_iter().map(|(name, artist)| json!({ "name": name, "artist": artist })).collect()
    }
}

// ---------------------------------------------------------------- helpers

fn content_type(suffix: &str) -> &'static str {
    match suffix.to_ascii_lowercase().as_str() {
        "opus" | "ogg" | "oga" => "audio/ogg",
        "mp3" => "audio/mpeg",
        "m4a" | "aac" | "mp4" => "audio/mp4",
        "flac" => "audio/flac",
        "wav" => "audio/wav",
        "webm" => "audio/webm",
        _ => "application/octet-stream",
    }
}

/// Unix seconds → `2026-11-06T12:34:56Z`.
fn iso(unix: f64) -> String {
    let secs = unix.max(0.0) as i64;
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // civil from days (Howard Hinnant)
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

/// A small xorshift for random lists (no need for a crate).
fn shuffled<T>(mut v: Vec<T>) -> Vec<T> {
    static SEED: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(1);
    let mut x = nanos ^ SEED.fetch_add(0x9E37_79B9_7F4A_7C15, Ordering::Relaxed) | 1;
    for i in (1..v.len()).rev() {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        v.swap(i, (x % (i as u64 + 1)) as usize);
    }
    v
}

fn page<T: Clone>(v: &[T], offset: usize, size: usize) -> Vec<T> {
    v.iter().skip(offset).take(size).cloned().collect()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(s.get(i..i + 2)?, 16).ok()).collect()
}

/// Equal without an early exit (no timing hint about how much matched).
fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

// ----------------------------------------------------------------- params

struct Params(Vec<(String, String)>);

impl Params {
    fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
    fn all(&self, name: &str) -> Vec<&str> {
        self.0.iter().filter(|(k, _)| k == name).map(|(_, v)| v.as_str()).collect()
    }
    fn num(&self, name: &str, default: usize, max: usize) -> usize {
        self.get(name).and_then(|v| v.parse().ok()).unwrap_or(default).min(max)
    }
}

/// Query string plus, for a form POST, the body (OpenSubsonic formPost).
async fn read_params(req: Request) -> Params {
    let mut out: Vec<(String, String)> =
        form_urlencoded::parse(req.uri().query().unwrap_or("").as_bytes()).into_owned().collect();
    let is_form = req
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/x-www-form-urlencoded"));
    if req.method() == Method::POST && is_form {
        if let Ok(body) = axum::body::to_bytes(req.into_body(), 1 << 20).await {
            out.extend(form_urlencoded::parse(&body).into_owned());
        }
    }
    Params(out)
}

/// Subsonic auth: user `bakin` and either `t` = md5(password + `s`) with
/// `s`, or `p` (plain or `enc:` + hex).
fn check_auth(p: &Params, password: &str) -> bool {
    use md5::Digest;
    if password.is_empty() || p.get("u") != Some(USER) {
        return false;
    }
    if let (Some(t), Some(s)) = (p.get("t"), p.get("s")) {
        let want = hex(&md5::Md5::digest(format!("{password}{s}").as_bytes()));
        return same(want.as_bytes(), t.to_ascii_lowercase().as_bytes());
    }
    if let Some(given) = p.get("p") {
        let given = match given.strip_prefix("enc:") {
            Some(h) => match unhex(h) {
                Some(b) => b,
                None => return false,
            },
            None => given.as_bytes().to_vec(),
        };
        return same(&given, password.as_bytes());
    }
    false
}

// --------------------------------------------------------------- responses

enum Out {
    Data(Value),
    Fail(u32, String),
    Raw(Response),
}

fn not_found() -> Out {
    Out::Fail(70, "The requested data was not found.".into())
}

fn envelope(out_fields: Value, failed: Option<(u32, String)>) -> Value {
    let mut body = Map::new();
    body.insert("status".into(), (if failed.is_some() { "failed" } else { "ok" }).into());
    body.insert("version".into(), API_VERSION.into());
    body.insert("type".into(), "bakin".into());
    body.insert("serverVersion".into(), env!("CARGO_PKG_VERSION").into());
    body.insert("openSubsonic".into(), true.into());
    if let Value::Object(m) = out_fields {
        body.extend(m);
    }
    if let Some((code, message)) = failed {
        body.insert("error".into(), json!({ "code": code, "message": message }));
    }
    json!({ "subsonic-response": body })
}

fn render(out: Out, as_json: bool) -> Response {
    let value = match out {
        Out::Raw(r) => return r,
        Out::Data(v) => envelope(v, None),
        Out::Fail(code, msg) => envelope(Value::Null, Some((code, msg))),
    };
    if as_json {
        ([(header::CONTENT_TYPE, "application/json")], value.to_string()).into_response()
    } else {
        ([(header::CONTENT_TYPE, "text/xml; charset=utf-8")], to_xml(&value)).into_response()
    }
}

fn xml_escape(s: &str, out: &mut String) {
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            // characters XML 1.0 cannot carry at all
            c if (c as u32) < 0x20 && !matches!(c, '\t' | '\n' | '\r') => {}
            c => out.push(c),
        }
    }
}

fn scalar(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Bool(b) => Some(b.to_string()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// The JSON form as Subsonic XML: scalar fields → attributes, objects →
/// child elements, arrays → one element per item named after the field,
/// a `value` field → the element's text (lyrics lines).
fn xml_element(name: &str, v: &Value, extra_attrs: &str, out: &mut String) {
    out.push('<');
    out.push_str(name);
    out.push_str(extra_attrs);
    let mut text = None;
    let mut children = Vec::new();
    match v {
        Value::Object(m) => {
            for (k, field) in m {
                match (k.as_str(), scalar(field)) {
                    ("value", Some(s)) => text = Some(s),
                    (_, Some(s)) => {
                        out.push(' ');
                        out.push_str(k);
                        out.push_str("=\"");
                        xml_escape(&s, out);
                        out.push('"');
                    }
                    _ => children.push((k, field)),
                }
            }
        }
        other => text = scalar(other),
    }
    if text.is_none() && children.is_empty() {
        out.push_str("/>");
        return;
    }
    out.push('>');
    if let Some(t) = text {
        xml_escape(&t, out);
    }
    for (k, field) in children {
        match field {
            Value::Array(items) => items.iter().for_each(|item| xml_element(k, item, "", out)),
            Value::Null => {}
            other => xml_element(k, other, "", out),
        }
    }
    out.push_str("</");
    out.push_str(name);
    out.push('>');
}

fn to_xml(envelope: &Value) -> String {
    let mut out = String::from("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
    xml_element("subsonic-response", &envelope["subsonic-response"], " xmlns=\"http://subsonic.org/restapi\"", &mut out);
    out
}

// ------------------------------------------------------------------ server

fn router(shared: Arc<Shared>) -> Router {
    Router::new().fallback(handle).with_state(shared)
}

async fn handle(State(sh): State<Arc<Shared>>, ConnectInfo(peer): ConnectInfo<SocketAddr>, req: Request) -> Response {
    let ip = peer.ip();
    if !is_private(ip) {
        return (StatusCode::FORBIDDEN, "Bakin only answers on the home network.").into_response();
    }
    let path = req.uri().path().to_string();
    if let Some(page) = pwa_file(&path) {
        return page;
    }
    let Some(name) = path.strip_prefix("/rest/") else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let name = name.strip_suffix(".view").unwrap_or(name).to_string();
    let headers = req.headers().clone();
    let params = read_params(req).await;
    let as_json = params.get("f") == Some("json");
    if sh.blocked(ip) {
        return render(Out::Fail(40, "Too many failed logins; try again in a minute.".into()), as_json);
    }
    if !check_auth(&params, &sh.password) {
        sh.failed(ip);
        return render(Out::Fail(40, "Wrong username or password.".into()), as_json);
    }
    let out = match endpoint(&sh, &name, &params, &headers).await {
        Ok(out) => out,
        Err(e) => Out::Fail(0, e),
    };
    render(out, as_json)
}

async fn endpoint(sh: &Shared, name: &str, p: &Params, headers: &HeaderMap) -> Result<Out, String> {
    let id = p.get("id").unwrap_or("");
    Ok(match name {
        "ping" => Out::Data(json!({})),
        "getLicense" => Out::Data(json!({ "license": { "valid": true } })),
        "getOpenSubsonicExtensions" => Out::Data(json!({ "openSubsonicExtensions": [
            { "name": "songLyrics", "versions": [1] },
            { "name": "formPost", "versions": [1] },
        ] })),
        "getMusicFolders" => Out::Data(json!({ "musicFolders": { "musicFolder": [{ "id": 1, "name": "Bakin" }] } })),
        "getUser" => Out::Data(json!({ "user": {
            "username": USER, "email": "", "scrobblingEnabled": true, "adminRole": false,
            "settingsRole": false, "downloadRole": true, "uploadRole": false, "playlistRole": false,
            "coverArtRole": true, "commentRole": false, "podcastRole": false, "streamRole": true,
            "jukeboxRole": false, "shareRole": false, "videoConversionRole": false, "folder": [1],
        } })),
        "getGenres" => Out::Data(json!({ "genres": {} })),
        "getScanStatus" => {
            let idx = sh.index().await?;
            Out::Data(json!({ "scanStatus": { "scanning": false, "count": idx.songs.len() } }))
        }
        "getIndexes" => {
            let idx = sh.index().await?;
            Out::Data(json!({ "indexes": {
                "lastModified": 0, "ignoredArticles": IGNORED_ARTICLES, "index": idx.indexes(),
            } }))
        }
        "getArtists" => {
            let idx = sh.index().await?;
            Out::Data(json!({ "artists": { "ignoredArticles": IGNORED_ARTICLES, "index": idx.indexes() } }))
        }
        "getMusicDirectory" => {
            let idx = sh.index().await?;
            if let Some(ar) = idx.artist(id) {
                let child: Vec<Value> = ar
                    .albums
                    .iter()
                    .map(|&a| {
                        let al = &idx.albums[a];
                        json!({ "id": al.id, "parent": ar.id, "isDir": true, "title": al.name,
                                "album": al.name, "artist": al.artist, "coverArt": al.id })
                    })
                    .collect();
                Out::Data(json!({ "directory": { "id": ar.id, "name": ar.name, "child": child } }))
            } else if let Some(al) = idx.album(id) {
                let child: Vec<Value> = al.songs.iter().map(|&i| idx.child(i)).collect();
                Out::Data(json!({ "directory": { "id": al.id, "parent": al.artist_id, "name": al.name, "child": child } }))
            } else {
                not_found()
            }
        }
        "getArtist" => {
            let idx = sh.index().await?;
            match idx.artist(id) {
                Some(ar) => {
                    let mut v = idx.artist_json(ar);
                    v["album"] = ar.albums.iter().map(|&a| idx.album_json(&idx.albums[a])).collect();
                    Out::Data(json!({ "artist": v }))
                }
                None => not_found(),
            }
        }
        "getAlbum" => {
            let idx = sh.index().await?;
            match idx.album(id) {
                Some(al) => {
                    let mut v = idx.album_json(al);
                    v["song"] = al.songs.iter().map(|&i| idx.child(i)).collect();
                    Out::Data(json!({ "album": v }))
                }
                None => not_found(),
            }
        }
        "getSong" => {
            let idx = sh.index().await?;
            match idx.song(id) {
                Some(i) => Out::Data(json!({ "song": idx.child(i) })),
                None => not_found(),
            }
        }
        "getAlbumList2" => {
            let idx = sh.index().await?;
            let size = p.num("size", 10, 500);
            let offset = p.num("offset", 0, usize::MAX);
            let mut list: Vec<usize> = (0..idx.albums.len()).collect();
            let al = |i: usize| &idx.albums[i];
            match p.get("type").unwrap_or("") {
                "random" => list = shuffled(list),
                "newest" => list.sort_by(|&a, &b| al(b).created.total_cmp(&al(a).created)),
                "frequent" => {
                    list.retain(|&a| al(a).plays > 0);
                    list.sort_by_key(|&a| std::cmp::Reverse(al(a).plays));
                }
                // Bakin keeps play counts but not when: played albums, newest first
                "recent" => {
                    list.retain(|&a| al(a).plays > 0);
                    list.sort_by(|&a, &b| al(b).created.total_cmp(&al(a).created));
                }
                "alphabeticalByArtist" => list.sort_by_key(|&a| (fold(&al(a).artist), fold(&al(a).name))),
                "starred" => {
                    list.retain(|&a| al(a).starred);
                    list.sort_by_key(|&a| fold(&al(a).name));
                }
                "byYear" => {
                    let from: u32 = p.get("fromYear").and_then(|v| v.parse().ok()).unwrap_or(0);
                    let to: u32 = p.get("toYear").and_then(|v| v.parse().ok()).unwrap_or(u32::MAX);
                    let (lo, hi) = (from.min(to), from.max(to));
                    list.retain(|&a| al(a).year.is_some_and(|y| y >= lo && y <= hi));
                    list.sort_by_key(|&a| al(a).year);
                    if from > to {
                        list.reverse();
                    }
                }
                "byGenre" => list.clear(),
                _ => list.sort_by_key(|&a| fold(&al(a).name)),
            }
            let album: Vec<Value> = page(&list, offset, size).into_iter().map(|a| idx.album_json(al(a))).collect();
            Out::Data(json!({ "albumList2": { "album": album } }))
        }
        "getRandomSongs" => {
            let idx = sh.index().await?;
            let size = p.num("size", 10, 500);
            let from: u32 = p.get("fromYear").and_then(|v| v.parse().ok()).unwrap_or(0);
            let to: u32 = p.get("toYear").and_then(|v| v.parse().ok()).unwrap_or(u32::MAX);
            let has_years = p.get("fromYear").is_some() || p.get("toYear").is_some();
            let list: Vec<usize> = (0..idx.songs.len())
                .filter(|&i| !has_years || idx.songs[i].year.is_some_and(|y| y >= from && y <= to))
                .collect();
            let song: Vec<Value> = shuffled(list).into_iter().take(size).map(|i| idx.child(i)).collect();
            Out::Data(json!({ "randomSongs": { "song": song } }))
        }
        "search3" => {
            let idx = sh.index().await?;
            // clients sync with an empty query (or `""`): that means everything
            let q = fold(p.get("query").unwrap_or("").trim().trim_matches('"').trim_matches('*').trim());
            let terms: Vec<&str> = q.split_whitespace().collect();
            let hit = |hay: String| terms.iter().all(|t| hay.contains(t));
            let mut artists: Vec<&Artist> = idx.artists.iter().filter(|a| hit(fold(&a.name))).collect();
            artists.sort_by_key(|a| fold(&a.name));
            let mut albums: Vec<&Album> =
                idx.albums.iter().filter(|a| hit(fold(&format!("{} {}", a.name, a.artist)))).collect();
            albums.sort_by_key(|a| fold(&a.name));
            let songs: Vec<usize> = (0..idx.songs.len())
                .filter(|&i| {
                    let s = &idx.songs[i];
                    hit(fold(&format!("{} {} {}", s.title, s.artist, s.album)))
                })
                .collect();
            let artist: Vec<Value> = page(&artists, p.num("artistOffset", 0, usize::MAX), p.num("artistCount", 20, 10_000))
                .into_iter()
                .map(|a| idx.artist_json(a))
                .collect();
            let album: Vec<Value> = page(&albums, p.num("albumOffset", 0, usize::MAX), p.num("albumCount", 20, 10_000))
                .into_iter()
                .map(|a| idx.album_json(a))
                .collect();
            let song: Vec<Value> = page(&songs, p.num("songOffset", 0, usize::MAX), p.num("songCount", 20, 10_000))
                .into_iter()
                .map(|i| idx.child(i))
                .collect();
            Out::Data(json!({ "searchResult3": { "artist": artist, "album": album, "song": song } }))
        }
        "getStarred2" => {
            let idx = sh.index().await?;
            let song: Vec<Value> = (0..idx.songs.len()).filter(|&i| idx.songs[i].starred).map(|i| idx.child(i)).collect();
            let album: Vec<Value> = idx.albums.iter().filter(|a| a.starred).map(|a| idx.album_json(a)).collect();
            Out::Data(json!({ "starred2": { "artist": [], "album": album, "song": song } }))
        }
        "getPlaylists" => {
            let idx = sh.index().await?;
            let list: Vec<Value> = idx.playlists.iter().map(|pl| playlist_json(&idx, pl)).collect();
            Out::Data(json!({ "playlists": { "playlist": list } }))
        }
        "getPlaylist" => {
            let idx = sh.index().await?;
            match idx.playlist(id) {
                Some(pl) => {
                    let mut v = playlist_json(&idx, pl);
                    v["entry"] = idx.playlist_songs(pl).into_iter().map(|i| idx.child(i)).collect();
                    Out::Data(json!({ "playlist": v }))
                }
                None => not_found(),
            }
        }
        "star" | "unstar" => {
            // Bakin stars songs only: albumId / artistId are accepted and ignored
            let idx = sh.index().await?;
            let mut ids = Vec::new();
            for raw in p.all("id") {
                match idx.song(raw) {
                    Some(i) => ids.push(idx.songs[i].id.clone()),
                    None => return Ok(not_found()),
                }
            }
            let on = name == "star";
            let lib = sh.lib.clone();
            let res = blocking(move || ids.iter().try_for_each(|id| lib.star(id, on))).await;
            sh.forget().await;
            match res {
                Some(Ok(())) => Out::Data(json!({})),
                Some(Err(e)) => Out::Fail(0, e),
                None => Out::Fail(0, "star failed".into()),
            }
        }
        "scrobble" => {
            // submission=false is a "now playing" note: nothing to keep
            if p.get("submission").unwrap_or("true") == "true" {
                let idx = sh.index().await?;
                let ids: Vec<String> =
                    p.all("id").into_iter().filter_map(|raw| idx.song(raw)).map(|i| idx.songs[i].id.clone()).collect();
                let lib = sh.lib.clone();
                let res = blocking(move || ids.iter().try_for_each(|id| lib.scrobble(id))).await;
                sh.forget().await;
                if let Some(Err(e)) = res {
                    return Ok(Out::Fail(0, e));
                }
            }
            Out::Data(json!({}))
        }
        "getCoverArt" => {
            let idx = sh.index().await?;
            let Some(i) = idx.representative(id) else { return Ok(not_found()) };
            let lib_id = idx.songs[i].id.clone();
            let size = p.get("size").and_then(|v| v.parse().ok()).unwrap_or(0u32).min(2048);
            let lib = sh.lib.clone();
            match blocking(move || lib.cover(&lib_id, size)).await.flatten() {
                Some(jpeg) => Out::Raw(
                    ([(header::CONTENT_TYPE, "image/jpeg"), (header::CACHE_CONTROL, "max-age=86400")], jpeg).into_response(),
                ),
                None => not_found(),
            }
        }
        "stream" | "download" => {
            let idx = sh.index().await?;
            let Some(i) = idx.song(id) else { return Ok(not_found()) };
            let s = &idx.songs[i];
            let download = (name == "download").then(|| s.path.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default());
            serve_file(s.path.clone(), &s.suffix, headers, download).await?
        }
        "getLyricsBySongId" => {
            let idx = sh.index().await?;
            let Some(i) = idx.song(id) else { return Ok(not_found()) };
            let s = &idx.songs[i];
            let (lib_id, artist, title) = (s.id.clone(), s.artist.clone(), s.title.clone());
            let lib = sh.lib.clone();
            let structured: Vec<Value> = match blocking(move || lib.lyrics(&lib_id)).await.flatten() {
                Some(text) => {
                    let (synced, lines) = parse_lrc(&text);
                    let line: Vec<Value> = lines
                        .into_iter()
                        .map(|(start, value)| match start {
                            Some(ms) => json!({ "start": ms, "value": value }),
                            None => json!({ "value": value }),
                        })
                        .collect();
                    vec![json!({ "displayArtist": artist, "displayTitle": title, "lang": "xxx",
                                 "synced": synced, "offset": 0, "line": line })]
                }
                None => Vec::new(),
            };
            Out::Data(json!({ "lyricsList": { "structuredLyrics": structured } }))
        }
        "bakinLink" => {
            let url = p.get("url").unwrap_or("").trim().to_string();
            let ok = (url.starts_with("https://") || url.starts_with("http://")) && url.len() <= 2048
                && !url.chars().any(|c| c.is_whitespace() || c.is_control());
            if !ok {
                return Ok(Out::Fail(10, "That isn't a link.".into()));
            }
            let lib = sh.lib.clone();
            match blocking(move || lib.link(&url)).await {
                Some(Ok(())) => Out::Data(json!({})),
                Some(Err(e)) => Out::Fail(0, e),
                None => Out::Fail(0, "link failed".into()),
            }
        }
        _ => Out::Fail(0, "not implemented".into()),
    })
}

fn playlist_json(idx: &Index, pl: &Playlist) -> Value {
    let songs = idx.playlist_songs(pl);
    let duration: u64 = songs.iter().map(|&i| idx.songs[i].duration as u64).sum();
    let created = songs.iter().map(|&i| idx.songs[i].added).fold(0.0, f64::max);
    let mut v = json!({
        "id": format!("pl-{}", pl.id), "name": pl.name, "owner": USER, "public": false,
        "songCount": songs.len(), "duration": duration, "created": iso(created), "changed": iso(created),
    });
    if !songs.is_empty() {
        v["coverArt"] = format!("pl-{}", pl.id).into();
    }
    v
}

/// `Range: bytes=a-b` (one range) → Ok(Some((start, end_inclusive))); no
/// header → Ok(None); unsatisfiable → Err.
fn parse_range(h: Option<&HeaderValue>, len: u64) -> Result<Option<(u64, u64)>, ()> {
    let Some(h) = h else { return Ok(None) };
    let spec = h.to_str().map_err(|_| ())?.trim().strip_prefix("bytes=").ok_or(())?;
    if spec.contains(',') {
        // several ranges: serve the whole file, which the spec allows
        return Ok(None);
    }
    let (a, b) = spec.split_once('-').ok_or(())?;
    let (a, b) = (a.trim(), b.trim());
    let (start, end) = if a.is_empty() {
        let n: u64 = b.parse().map_err(|_| ())?;
        if n == 0 {
            return Err(());
        }
        (len.saturating_sub(n), len.checked_sub(1).ok_or(())?)
    } else {
        let start: u64 = a.parse().map_err(|_| ())?;
        let end = if b.is_empty() { len.saturating_sub(1) } else { b.parse::<u64>().map_err(|_| ())?.min(len.saturating_sub(1)) };
        (start, end)
    };
    if start >= len || start > end {
        return Err(());
    }
    Ok(Some((start, end)))
}

async fn serve_file(path: PathBuf, suffix: &str, headers: &HeaderMap, download: Option<String>) -> Result<Out, String> {
    use tokio::io::{AsyncReadExt, AsyncSeekExt};
    let Ok(mut file) = tokio::fs::File::open(&path).await else { return Ok(not_found()) };
    let len = file.metadata().await.map_err(|e| e.to_string())?.len();
    let mut res = Response::builder()
        .header(header::CONTENT_TYPE, content_type(suffix))
        .header(header::ACCEPT_RANGES, "bytes");
    if let Some(name) = download {
        let enc: String = form_urlencoded::byte_serialize(name.as_bytes()).collect::<String>().replace('+', "%20");
        res = res.header(header::CONTENT_DISPOSITION, format!("attachment; filename*=UTF-8''{enc}"));
    }
    let (status, start, count) = match parse_range(headers.get(header::RANGE), len) {
        Ok(None) => (StatusCode::OK, 0, len),
        Ok(Some((a, b))) => {
            res = res.header(header::CONTENT_RANGE, format!("bytes {a}-{b}/{len}"));
            (StatusCode::PARTIAL_CONTENT, a, b - a + 1)
        }
        Err(()) => {
            let r = Response::builder()
                .status(StatusCode::RANGE_NOT_SATISFIABLE)
                .header(header::CONTENT_RANGE, format!("bytes */{len}"))
                .body(Body::empty())
                .map_err(|e| e.to_string())?;
            return Ok(Out::Raw(r));
        }
    };
    if start > 0 {
        file.seek(std::io::SeekFrom::Start(start)).await.map_err(|e| e.to_string())?;
    }
    let body = Body::from_stream(tokio_util::io::ReaderStream::new(file.take(count)));
    let r = res.status(status).header(header::CONTENT_LENGTH, count).body(body).map_err(|e| e.to_string())?;
    Ok(Out::Raw(r))
}

/// LRC → (synced, lines with start ms). Metadata tags are skipped and
/// `[offset:±ms]` is applied to the times; a line with several time tags
/// repeats. Without any time tag the text comes back as plain lines.
fn parse_lrc(text: &str) -> (bool, Vec<(Option<u64>, String)>) {
    fn time(tag: &str) -> Option<i64> {
        let (m, rest) = tag.split_once(':')?;
        let m: i64 = m.trim().parse().ok()?;
        let (s, frac) = match rest.split_once(['.', ':']) {
            Some((s, f)) => (s, f),
            None => (rest, ""),
        };
        let s: i64 = s.trim().parse().ok()?;
        let frac_ms = if frac.is_empty() {
            0
        } else {
            let digits: String = frac.chars().take(3).collect();
            let v: i64 = digits.parse().ok()?;
            v * 10_i64.pow(3 - digits.len() as u32)
        };
        Some(m * 60_000 + s * 1000 + frac_ms)
    }
    let mut offset = 0i64;
    let mut timed: Vec<(i64, String)> = Vec::new();
    let mut plain = Vec::new();
    for line in text.lines() {
        let mut rest = line.trim();
        let mut times = Vec::new();
        let mut tagged = false;
        while let Some(after) = rest.strip_prefix('[') {
            let Some(close) = after.find(']') else { break };
            let tag = &after[..close];
            tagged = true;
            if let Some(t) = time(tag) {
                times.push(t);
            } else if let Some(o) = tag.strip_prefix("offset:") {
                offset = o.trim().parse().unwrap_or(0);
            }
            rest = after[close + 1..].trim_start();
        }
        if !times.is_empty() {
            timed.extend(times.into_iter().map(|t| (t, rest.to_string())));
        } else if !tagged {
            plain.push(rest.to_string());
        }
    }
    if timed.is_empty() {
        while plain.last().is_some_and(|l: &String| l.is_empty()) {
            plain.pop();
        }
        return (false, plain.into_iter().map(|l| (None, l)).collect());
    }
    timed.sort_by_key(|(t, _)| *t);
    // a positive LRC offset shows the lines sooner
    (true, timed.into_iter().map(|(t, l)| (Some((t - offset).max(0) as u64), l)).collect())
}

// ------------------------------------------------------------------- tests

#[cfg(test)]
mod tests {
    use super::*;
    use axum::extract::connect_info::MockConnectInfo;
    use std::io::{Read, Write};
    use tower::ServiceExt;

    struct FakeLibrary {
        songs: Vec<Song>,
        starred: Mutex<Vec<(String, bool)>>,
        plays: Mutex<Vec<String>>,
        links: Mutex<Vec<String>>,
    }

    impl Library for FakeLibrary {
        fn snapshot(&self) -> Result<Snapshot, String> {
            let stars = self.starred.lock().unwrap();
            let songs = self
                .songs
                .iter()
                .cloned()
                .map(|mut s| {
                    if let Some((_, on)) = stars.iter().rev().find(|(id, _)| *id == s.id) {
                        s.starred = *on;
                    }
                    s
                })
                .collect();
            Ok(Snapshot {
                songs,
                playlists: vec![Playlist { id: "mix".into(), name: "Mix".into(), song_ids: vec![self.songs[2].id.clone(), self.songs[0].id.clone()] }],
            })
        }
        fn star(&self, song_id: &str, on: bool) -> Result<(), String> {
            self.starred.lock().unwrap().push((song_id.to_string(), on));
            Ok(())
        }
        fn scrobble(&self, song_id: &str) -> Result<(), String> {
            self.plays.lock().unwrap().push(song_id.to_string());
            Ok(())
        }
        fn cover(&self, _song_id: &str, _size: u32) -> Option<Vec<u8>> {
            Some(vec![0xFF, 0xD8, 0xFF, 0xD9])
        }
        fn lyrics(&self, _song_id: &str) -> Option<String> {
            Some("[ar:Someone]\n[00:01.50]First\n[00:03.00][00:10.00]Chorus\n".into())
        }
        fn link(&self, url: &str) -> Result<(), String> {
            self.links.lock().unwrap().push(url.to_string());
            Ok(())
        }
    }

    /// Three songs in temp files (bytes 0..100 each); removed on drop.
    struct Fixture {
        lib: Arc<FakeLibrary>,
        dir: PathBuf,
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn fixture() -> Fixture {
        static N: AtomicU64 = AtomicU64::new(0);
        let dir = std::env::temp_dir().join(format!("bakin-lan-test-{}-{}", std::process::id(), N.fetch_add(1, Ordering::Relaxed)));
        std::fs::create_dir_all(&dir).unwrap();
        let data: Vec<u8> = (0..100u8).collect();
        let mk = |file: &str, title: &str, artist: &str, album: &str, track: u32, added: f64| {
            let path = dir.join(file);
            std::fs::write(&path, &data).unwrap();
            Song {
                id: format!("music|{}", path.display()),
                path,
                title: title.into(),
                artist: artist.into(),
                album: album.into(),
                duration: 180,
                track: Some(track),
                year: Some(2020),
                size: 100,
                suffix: file.rsplit('.').next().unwrap().into(),
                bitrate: Some(160),
                starred: false,
                plays: track,
                added,
            }
        };
        let songs = vec![
            mk("a.opus", "Alpha", "Artist One", "First Album", 1, 1_700_000_000.0),
            mk("b.mp3", "Beta", "Artist One", "First Album", 2, 1_700_000_100.0),
            mk("c.m4a", "Gamma & <Co>", "Artist Two, Guest", "Second", 1, 1_800_000_000.0),
        ];
        Fixture { lib: Arc::new(FakeLibrary { songs, starred: Mutex::new(Vec::new()), plays: Mutex::new(Vec::new()), links: Mutex::new(Vec::new()) }), dir }
    }

    const PASS: &str = "4821-7730";

    fn auth() -> String {
        let salt = "c19b2d";
        use md5::Digest;
        let t = hex(&md5::Md5::digest(format!("{PASS}{salt}").as_bytes()));
        format!("u=bakin&t={t}&s={salt}")
    }

    fn app(fx: &Fixture, peer: &str) -> Router {
        let shared = Arc::new(Shared::new(PASS.into(), fx.lib.clone()));
        router(shared).layer(MockConnectInfo(peer.parse::<SocketAddr>().unwrap()))
    }

    async fn call(app: &Router, uri: &str, range: Option<&str>) -> (StatusCode, HeaderMap, Vec<u8>) {
        let mut req = axum::http::Request::builder().uri(uri);
        if let Some(r) = range {
            req = req.header(header::RANGE, r);
        }
        let res = app.clone().oneshot(req.body(Body::empty()).unwrap()).await.unwrap();
        let (parts, body) = res.into_parts();
        (parts.status, parts.headers, axum::body::to_bytes(body, usize::MAX).await.unwrap().to_vec())
    }

    async fn call_json(app: &Router, uri: &str) -> Value {
        let (status, _, body) = call(app, uri, None).await;
        assert_eq!(status, StatusCode::OK);
        serde_json::from_slice::<Value>(&body).unwrap()["subsonic-response"].clone()
    }

    #[test]
    fn auth_token() {
        use md5::Digest;
        let params = |q: &str| Params(form_urlencoded::parse(q.as_bytes()).into_owned().collect());
        let t = hex(&md5::Md5::digest(b"secretabc"));
        assert!(check_auth(&params(&format!("u=bakin&t={t}&s=abc")), "secret"));
        assert!(check_auth(&params(&format!("u=bakin&t={}&s=abc", t.to_uppercase())), "secret"));
        assert!(!check_auth(&params(&format!("u=bakin&t={t}&s=abd")), "secret"));
        assert!(!check_auth(&params(&format!("u=bakin&t={t}&s=abc")), "secreT"));
        assert!(!check_auth(&params(&format!("u=admin&t={t}&s=abc")), "secret"));
        assert!(check_auth(&params("u=bakin&p=secret"), "secret"));
        assert!(!check_auth(&params("u=bakin&p=secrets"), "secret"));
        assert!(check_auth(&params(&format!("u=bakin&p=enc:{}", hex(b"secret"))), "secret"));
        assert!(!check_auth(&params("u=bakin&p=enc:zz"), "secret"));
        assert!(!check_auth(&params("u=bakin"), "secret"));
        assert!(!check_auth(&params("u=bakin&p="), ""), "an empty password never opens");
    }

    #[test]
    fn private_ip_only() {
        let table = [
            ("10.0.0.5", true),
            ("172.16.0.1", true),
            ("172.31.255.255", true),
            ("172.32.0.1", false),
            ("192.168.1.20", true),
            ("127.0.0.1", true),
            ("8.8.8.8", false),
            ("100.64.0.1", false),
            ("169.254.1.1", false),
            ("0.0.0.0", false),
            ("::1", true),
            ("::ffff:192.168.0.2", true),
            ("::ffff:1.1.1.1", false),
            ("2001:db8::1", false),
        ];
        for (ip, want) in table {
            assert_eq!(is_private(ip.parse().unwrap()), want, "{ip}");
        }
        assert!(private_ipv4s().iter().all(|ip| ip.is_private()));
        let fx = fixture();
        let server = LanServer::start(0, PASS.into(), fx.lib.clone()).unwrap();
        let addrs = server.addresses();
        assert!(addrs.iter().any(|a| a.ip() == IpAddr::V4(Ipv4Addr::LOCALHOST)));
        assert!(addrs.iter().all(|a| is_private(a.ip()) && !a.ip().is_unspecified()), "{addrs:?}");
        server.stop();
    }

    #[test]
    fn ids_round_trip() {
        let raw = "music|C:\\Música/x y+z?&=.opus";
        assert_eq!(song_lib_id(&song_id(raw)), raw);
        assert!(song_id(raw)[3..].chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
        assert_eq!(main_artist("Artist Two, Guest"), "Artist Two");
        assert_eq!(main_artist("A feat. B"), "A");
        assert_eq!(main_artist(""), UNKNOWN_ARTIST);
        assert_eq!(iso(0.0), "1970-01-01T00:00:00Z");
        assert_eq!(iso(1_700_000_000.0), "2023-11-14T22:13:20Z");
    }

    #[test]
    fn lrc_parsing() {
        let (synced, lines) = parse_lrc("[ti:x]\n[offset:500]\n[00:01.50]One\n[00:00.20][01:00]Two\n");
        assert!(synced);
        assert_eq!(lines, vec![(Some(0), "Two".into()), (Some(1000), "One".into()), (Some(59_500), "Two".into())]);
        let (synced, lines) = parse_lrc("plain one\nplain two\n");
        assert!(!synced);
        assert_eq!(lines.len(), 2);
    }

    #[tokio::test]
    async fn endpoints() {
        let fx = fixture();
        let app = app(&fx, "192.168.1.30:50000");
        let a = auth();

        // the phone page: no login needed for its files, / goes to it
        let (status, headers, body) = call(&app, "/app/", None).await;
        assert_eq!(status, StatusCode::OK);
        assert!(headers[header::CONTENT_TYPE].to_str().unwrap().starts_with("text/html"));
        assert!(String::from_utf8_lossy(&body).contains("/rest/"));
        assert_eq!(call(&app, "/", None).await.0, StatusCode::FOUND);
        assert_eq!(call(&app, "/app/gm.css", None).await.0, StatusCode::OK);
        assert_eq!(call(&app, "/app/../Cargo.toml", None).await.0, StatusCode::NOT_FOUND);

        // «Mandar a Bakin»: a link, never anything else
        let r = call_json(&app, &format!("/rest/bakinLink?{a}&f=json&url=https%3A%2F%2Fyoutu.be%2Fabc")).await;
        assert_eq!(r["status"], "ok");
        let r = call_json(&app, &format!("/rest/bakinLink?{a}&f=json&url=javascript%3Aalert(1)")).await;
        assert_eq!(r["status"], "failed");
        assert_eq!(*fx.lib.links.lock().unwrap(), vec!["https://youtu.be/abc".to_string()]);
        let r = call_json(&app, "/rest/bakinLink?u=bakin&p=wrong&f=json&url=https%3A%2F%2Fx.y").await;
        assert_eq!(r["error"]["code"], 40);

        // ping, JSON and XML
        let r = call_json(&app, &format!("/rest/ping?{a}&f=json")).await;
        assert_eq!(r["status"], "ok");
        assert_eq!(r["type"], "bakin");
        assert_eq!(r["openSubsonic"], true);
        assert_eq!(r["version"], API_VERSION);
        let (status, headers, body) = call(&app, &format!("/rest/ping.view?{a}"), None).await;
        assert_eq!(status, StatusCode::OK);
        assert!(headers[header::CONTENT_TYPE].to_str().unwrap().starts_with("text/xml"));
        let xml = String::from_utf8(body).unwrap();
        assert!(xml.contains("<subsonic-response xmlns=\"http://subsonic.org/restapi\""), "{xml}");
        assert!(xml.contains("status=\"ok\"") && xml.ends_with("/>"), "{xml}");

        // wrong password → error 40
        let r = call_json(&app, "/rest/ping?u=bakin&p=nope&f=json").await;
        assert_eq!(r["status"], "failed");
        assert_eq!(r["error"]["code"], 40);

        // search3 with an empty query returns everything
        let r = call_json(&app, &format!("/rest/search3?{a}&f=json&query=%22%22")).await;
        let res = &r["searchResult3"];
        assert_eq!(res["song"].as_array().unwrap().len(), 3);
        assert_eq!(res["album"].as_array().unwrap().len(), 2);
        assert_eq!(res["artist"].as_array().unwrap().len(), 2);
        let r = call_json(&app, &format!("/rest/search3?{a}&f=json&query=gam")).await;
        assert_eq!(r["searchResult3"]["song"].as_array().unwrap().len(), 1);

        // getAlbum: the songs in track order, no absolute path
        let first = album_id("First Album", "Artist One");
        let r = call_json(&app, &format!("/rest/getAlbum?{a}&f=json&id={first}")).await;
        let songs = r["album"]["song"].as_array().unwrap();
        assert_eq!(r["album"]["songCount"], 2);
        assert_eq!(songs[0]["title"], "Alpha");
        assert_eq!(songs[1]["title"], "Beta");
        assert_eq!(songs[0]["path"], "Artist One/First Album/a.opus");
        assert_eq!(songs[0]["contentType"], "audio/ogg");
        assert_eq!(songs[0]["albumId"], first.as_str());
        let alpha = songs[0]["id"].as_str().unwrap().to_string();

        // XML escaping and arrays as repeated elements
        let (_, _, body) = call(&app, &format!("/rest/getAlbum?{a}&id={}", album_id("Second", "Artist Two")), None).await;
        let xml = String::from_utf8(body).unwrap();
        assert!(xml.contains("title=\"Gamma &amp; &lt;Co&gt;\""), "{xml}");
        assert_eq!(xml.matches("<song ").count(), 1);

        // stream with a range
        let (status, headers, body) = call(&app, &format!("/rest/stream?{a}&id={alpha}"), Some("bytes=10-19")).await;
        assert_eq!(status, StatusCode::PARTIAL_CONTENT);
        assert_eq!(body, (10..20u8).collect::<Vec<u8>>());
        assert_eq!(headers[header::CONTENT_RANGE], "bytes 10-19/100");
        assert_eq!(headers[header::CONTENT_TYPE], "audio/ogg");
        let (status, _, body) = call(&app, &format!("/rest/download?{a}&id={alpha}"), None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body.len(), 100);
        let (status, _, _) = call(&app, &format!("/rest/stream?{a}&id={alpha}"), Some("bytes=500-")).await;
        assert_eq!(status, StatusCode::RANGE_NOT_SATISFIABLE);

        // star → the library records it and getStarred2 shows it
        let r = call_json(&app, &format!("/rest/star?{a}&f=json&id={alpha}")).await;
        assert_eq!(r["status"], "ok");
        assert_eq!(*fx.lib.starred.lock().unwrap(), vec![(fx.lib.songs[0].id.clone(), true)]);
        let r = call_json(&app, &format!("/rest/getStarred2?{a}&f=json")).await;
        assert_eq!(r["starred2"]["song"][0]["id"], alpha.as_str());

        // scrobble: only submissions count
        call_json(&app, &format!("/rest/scrobble?{a}&f=json&id={alpha}&submission=false")).await;
        call_json(&app, &format!("/rest/scrobble?{a}&f=json&id={alpha}")).await;
        assert_eq!(fx.lib.plays.lock().unwrap().len(), 1);

        // playlists, cover, lyrics, album lists
        let r = call_json(&app, &format!("/rest/getPlaylist?{a}&f=json&id=pl-mix")).await;
        assert_eq!(r["playlist"]["entry"][0]["title"], "Gamma & <Co>");
        let (status, headers, _) = call(&app, &format!("/rest/getCoverArt?{a}&id={first}&size=300"), None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(headers[header::CONTENT_TYPE], "image/jpeg");
        let r = call_json(&app, &format!("/rest/getLyricsBySongId?{a}&f=json&id={alpha}")).await;
        let lyr = &r["lyricsList"]["structuredLyrics"][0];
        assert_eq!(lyr["synced"], true);
        assert_eq!(lyr["line"][0]["start"], 1500);
        assert_eq!(lyr["line"].as_array().unwrap().len(), 3);
        let r = call_json(&app, &format!("/rest/getAlbumList2?{a}&f=json&type=newest&size=1")).await;
        assert_eq!(r["albumList2"]["album"][0]["name"], "Second");

        // unknown endpoint: error 0, still HTTP 200
        let r = call_json(&app, &format!("/rest/createShare?{a}&f=json")).await;
        assert_eq!(r["error"]["code"], 0);

        // a form POST works like a query
        let req = axum::http::Request::builder()
            .method(Method::POST)
            .uri("/rest/ping.view")
            .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
            .body(Body::from(format!("{a}&f=json")))
            .unwrap();
        let res = app.clone().oneshot(req).await.unwrap();
        let body = axum::body::to_bytes(res.into_body(), usize::MAX).await.unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&body).unwrap()["subsonic-response"]["status"], "ok");
    }

    #[tokio::test]
    async fn public_peer_rejected() {
        let fx = fixture();
        let app = app(&fx, "203.0.113.9:40000");
        let (status, _, _) = call(&app, &format!("/rest/ping?{}&f=json", auth()), None).await;
        assert_eq!(status, StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn failed_logins_rate_limited() {
        let fx = fixture();
        let app = app(&fx, "10.0.0.7:40000");
        for _ in 0..MAX_FAILURES {
            call_json(&app, "/rest/ping?u=bakin&p=wrong&f=json").await;
        }
        // now even the right password is refused for a while
        let r = call_json(&app, &format!("/rest/ping?{}&f=json", auth())).await;
        assert_eq!(r["error"]["code"], 40);
    }

    #[test]
    fn real_port_closes_on_stop() {
        let fx = fixture();
        let server = LanServer::start(0, PASS.into(), fx.lib.clone()).unwrap();
        let addr = *server.addresses().iter().find(|a| a.ip().is_loopback()).unwrap();
        let mut conn = std::net::TcpStream::connect(addr).unwrap();
        conn.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        write!(conn, "GET /rest/ping?{}&f=json HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n", auth()).unwrap();
        let mut reply = String::new();
        conn.read_to_string(&mut reply).unwrap();
        assert!(reply.starts_with("HTTP/1.1 200") && reply.contains("\"status\":\"ok\""), "{reply}");
        server.stop();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(300)) {
                Err(_) => break,
                Ok(_) if Instant::now() > deadline => panic!("{addr} still open after stop()"),
                Ok(_) => std::thread::sleep(Duration::from_millis(50)),
            }
        }
    }
}
