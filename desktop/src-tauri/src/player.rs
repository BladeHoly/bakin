//! Bakin Música's player, inside the shell process, so music never depends on
//! the window (docs/ARCHITECTURE-BACKGROUND.md).
//!
//! One thread decodes (Symphonia; libopus for Opus) and keeps about half a
//! second of stereo audio ready at the output's rate; the Windows output
//! (WASAPI through cpal) takes from it. When a song ends the next one is
//! decoded into the same stream, so there is no gap, or both are mixed for a
//! crossfade. Volume and pause act in the output, at once. The queue, its
//! order and the settings belong to the player, which saves them itself; the
//! page sends commands and draws the events it gets.

use std::collections::VecDeque;
use std::fs::File;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use rubato::{Fft, FixedSync, Indexing, Resampler};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use symphonia::core::codecs::audio::{AudioDecoder, AudioDecoderOptions};
use symphonia::core::codecs::registry::CodecRegistry;
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, FormatReader, SeekMode, SeekTo, TrackType};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::units::Time;

const AHEAD_SECS: f64 = 0.5; // audio kept ready in front of the output
const MAX_QUEUE: usize = 20_000;

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Track {
    pub id: String,
    pub path: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub artist: String,
    #[serde(default)]
    pub album: String,
    #[serde(default)]
    pub duration: Option<f64>,
    #[serde(default)]
    pub fav: bool,
    /// Unix time of the last counted play (0: never), for the smart shuffle.
    #[serde(default)]
    pub last_played: f64,
    /// dB toward the same loudness for every song (PLAN 0.16.1; the engine measures it with
    /// ffmpeg's EBU R128 and limits it so nothing clips). None: not measured.
    #[serde(default)]
    pub gain_db: Option<f32>,
}

// ------------------------------------------------------------------ data dir
/// Bakin's data folder, as the engine sees it (BAKIN_HOME for tests).
pub fn data_dir() -> PathBuf {
    if let Ok(home) = std::env::var("BAKIN_HOME") {
        return PathBuf::from(home);
    }
    let base = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into());
    let base = PathBuf::from(base).join("BakinData");
    // moved elsewhere (PLAN 0.18.4): redirect.txt holds the path; not there now → the usual one
    if let Ok(text) = std::fs::read_to_string(base.join("redirect.txt")) {
        let moved = PathBuf::from(text.trim());
        if moved.is_absolute() && moved.is_dir() {
            return moved;
        }
    }
    base
}

fn now_secs() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

// -------------------------------------------------------------------- source
fn codecs() -> &'static CodecRegistry {
    static REG: OnceLock<CodecRegistry> = OnceLock::new();
    REG.get_or_init(|| {
        let mut r = CodecRegistry::new();
        symphonia::default::register_enabled_codecs(&mut r);
        r.register_audio_decoder::<symphonia_adapter_libopus::OpusDecoder>();
        r
    })
}

/// One song being decoded into stereo frames at the output rate.
struct Source {
    format: Box<dyn FormatReader>,
    decoder: Box<dyn AudioDecoder>,
    track_id: u32,
    out_rate: u32,
    resampler: Option<Fft<f32>>,
    rs_in: Vec<f32>,  // stereo, input rate, waiting for a full resampler chunk
    rs_out: Vec<f32>, // resampler output scratch
    rs_skip: usize,   // resampler delay still to drop (output frames)
    in_total: u64,    // input frames fed to the resampler
    out_total: u64,   // output frames kept (after the delay)
    ratio: f64,
    skip_in: usize,   // input frames to drop after an accurate seek
    pending: VecDeque<f32>,
    scratch: Vec<f32>,
    eof: bool,
    flushed: bool,
    produced: u64, // output frames handed out since `start`
    start: f64,    // seconds into the song where this source began
    gain: f32,     // even volume (PLAN 0.16.1): 1.0 = as it is
}

impl Source {
    fn open(path: &str, start: f64, out_rate: u32) -> Result<Source, String> {
        let file = File::open(path).map_err(|e| format!("no se pudo abrir el archivo ({e})"))?;
        let mss = MediaSourceStream::new(Box::new(file), Default::default());
        let mut hint = Hint::new();
        if let Some(ext) = Path::new(path).extension().and_then(|e| e.to_str()) {
            hint.with_extension(ext);
        }
        let mut format = symphonia::default::get_probe()
            .probe(&hint, mss, FormatOptions::default(), MetadataOptions::default())
            .map_err(|e| format!("formato no reconocido ({e})"))?;
        let track = format.default_track(TrackType::Audio).ok_or("el archivo no tiene audio")?;
        let track_id = track.id;
        let time_base = track.time_base;
        let params = track
            .codec_params
            .as_ref()
            .and_then(|p| p.audio())
            .ok_or("el archivo no tiene audio")?
            .clone();
        let decoder = codecs()
            .make_audio_decoder(&params, &AudioDecoderOptions::default())
            .map_err(|e| format!("no se puede decodificar ({e})"))?;
        let in_rate = params.sample_rate.unwrap_or(out_rate);
        let mut skip_in = 0usize;
        if start > 0.05 {
            let secs = start.floor() as i64;
            let nanos = ((start - start.floor()) * 1e9) as u32;
            if let Some(time) = Time::try_new(secs, nanos) {
                if let Ok(seeked) = format.seek(SeekMode::Accurate, SeekTo::Time { time, track_id: Some(track_id) }) {
                    if let Some(tb) = time_base {
                        let diff = seeked.required_ts.get() - seeked.actual_ts.get();
                        if diff > 0 {
                            let secs = diff as f64 * tb.numer.get() as f64 / tb.denom.get() as f64;
                            skip_in = (secs * in_rate as f64) as usize;
                        }
                    }
                }
            }
        }
        let resampler = if in_rate != out_rate {
            Some(Fft::<f32>::new(in_rate as usize, out_rate as usize, 1024, 2, FixedSync::Input).map_err(|e| e.to_string())?)
        } else {
            None
        };
        let rs_skip = resampler.as_ref().map(|r| r.output_delay()).unwrap_or(0);
        let rs_out = vec![0.0; resampler.as_ref().map(|r| r.output_frames_max() * 2).unwrap_or(0)];
        Ok(Source {
            format,
            decoder,
            track_id,
            out_rate,
            resampler,
            rs_in: Vec::new(),
            rs_out,
            rs_skip,
            in_total: 0,
            out_total: 0,
            ratio: out_rate as f64 / in_rate as f64,
            skip_in,
            pending: VecDeque::new(),
            scratch: Vec::new(),
            eof: false,
            flushed: false,
            produced: 0,
            start,
            gain: 1.0,
        })
    }

    fn position(&self) -> f64 {
        self.start + self.produced as f64 / self.out_rate as f64
    }

    /// Decodes the next packet into `pending`; false at the end of the song.
    fn decode_more(&mut self) -> bool {
        if self.eof {
            return self.flush_resampler();
        }
        loop {
            let packet = match self.format.next_packet() {
                Ok(Some(p)) => p,
                Ok(None) | Err(_) => {
                    self.eof = true;
                    return self.flush_resampler();
                }
            };
            if packet.track_id != self.track_id {
                continue;
            }
            let audio = match self.decoder.decode(&packet) {
                Ok(a) => a,
                Err(SymError::DecodeError(_)) => continue, // a damaged packet: skip it
                Err(_) => {
                    self.eof = true;
                    return self.flush_resampler();
                }
            };
            let ch = audio.spec().channels().count().max(1);
            self.scratch.resize(audio.samples_interleaved(), 0.0);
            audio.copy_to_slice_interleaved(&mut self.scratch);
            let mut stereo = Vec::with_capacity(self.scratch.len() / ch * 2);
            for f in self.scratch.chunks_exact(ch) {
                let (l, r) = if ch == 1 { (f[0], f[0]) } else { (f[0], f[1]) };
                stereo.push(l);
                stereo.push(r);
            }
            if self.skip_in > 0 {
                let drop = (self.skip_in * 2).min(stereo.len());
                stereo.drain(..drop);
                self.skip_in -= drop / 2;
            }
            if stereo.is_empty() {
                continue;
            }
            self.push_input(&stereo);
            return true;
        }
    }

    fn push_input(&mut self, stereo: &[f32]) {
        if self.resampler.is_none() {
            self.pending.extend(stereo);
            return;
        }
        self.rs_in.extend_from_slice(stereo);
        self.in_total += (stereo.len() / 2) as u64;
        loop {
            let Some(rs) = self.resampler.as_mut() else { break };
            let need = rs.input_frames_next();
            if self.rs_in.len() < need * 2 {
                break;
            }
            let frames_out = rs.output_frames_max();
            let out = {
                let input = audioadapter_buffers::direct::InterleavedSlice::new(&self.rs_in[..need * 2], 2, need).unwrap();
                let mut output = audioadapter_buffers::direct::InterleavedSlice::new_mut(&mut self.rs_out[..frames_out * 2], 2, frames_out).unwrap();
                rs.process_into_buffer(&input, &mut output, None).map(|(_, o)| o).unwrap_or(0)
            };
            self.rs_in.drain(..need * 2);
            self.take_out(out);
        }
    }

    /// Keeps the resampler's last output, minus its initial delay.
    fn take_out(&mut self, frames: usize) {
        let out = &self.rs_out[..frames * 2];
        let drop = (self.rs_skip * 2).min(out.len());
        self.rs_skip -= drop / 2;
        self.pending.extend(&out[drop..]);
        self.out_total += ((out.len() - drop) / 2) as u64;
    }

    /// At the end: run what is left through the resampler (and silence for
    /// its delay) until the output holds exactly the song's length.
    fn flush_resampler(&mut self) -> bool {
        if self.flushed {
            return false;
        }
        self.flushed = true;
        if self.resampler.is_none() {
            return false;
        }
        let expected = (self.in_total as f64 * self.ratio).round() as u64;
        let mut left = self.rs_in.len() / 2;
        let mut offset = 0;
        for _ in 0..8 {
            if self.out_total >= expected {
                break;
            }
            let rs = self.resampler.as_mut().unwrap();
            let need = rs.input_frames_next();
            let n = left.min(need);
            let mut chunk = vec![0.0f32; need * 2];
            chunk[..n * 2].copy_from_slice(&self.rs_in[offset * 2..(offset + n) * 2]);
            left -= n;
            offset += n;
            let frames_out = rs.output_frames_max();
            let input = audioadapter_buffers::direct::InterleavedSlice::new(&chunk, 2, need).unwrap();
            let mut output = audioadapter_buffers::direct::InterleavedSlice::new_mut(&mut self.rs_out[..frames_out * 2], 2, frames_out).unwrap();
            let idx = Indexing { input_offset: 0, output_offset: 0, partial_len: Some(n), active_channels_mask: None };
            match rs.process_into_buffer(&input, &mut output, Some(&idx)) {
                Ok((_, out)) => self.take_out(out),
                Err(_) => break,
            }
        }
        if self.out_total > expected {
            let extra = ((self.out_total - expected) as usize * 2).min(self.pending.len());
            let keep = self.pending.len() - extra;
            self.pending.truncate(keep);
            self.out_total = expected;
        }
        self.rs_in.clear();
        !self.pending.is_empty()
    }

    /// Appends up to `frames` stereo frames to `out`; fewer only at the end.
    fn read(&mut self, out: &mut Vec<f32>, frames: usize) -> usize {
        let mut got = 0;
        while got < frames {
            if self.pending.is_empty() && !self.decode_more() && self.pending.is_empty() {
                break;
            }
            let take = ((frames - got) * 2).min(self.pending.len());
            if self.gain == 1.0 {
                out.extend(self.pending.drain(..take));
            } else {
                let g = self.gain;
                out.extend(self.pending.drain(..take).map(|x| x * g));
            }
            got += take / 2;
        }
        self.produced += got as u64;
        got
    }
}

// ----------------------------------------------------------------------- EQ
/// A 10-band equalizer (PLAN 0.16.2): peaking biquads (RBJ cookbook) at the usual ISO
/// frequencies, per channel, after the songs are mixed; a pre-gain of minus the biggest boost so
/// a boost never clips. Bands at 0 dB cost nothing.
pub const EQ_BANDS: [f32; 10] = [31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0];

#[derive(Clone)]
struct Eq {
    on: bool,
    gains: [f32; 10],
    rate: u32,
    coef: Vec<(usize, [f32; 5])>, // (band, b0 b1 b2 a1 a2 normalised)
    state: [[f32; 4]; 20],        // per band and channel: x1 x2 y1 y2
    pre: f32,
}

impl Eq {
    fn new(rate: u32) -> Eq {
        Eq { on: false, gains: [0.0; 10], rate, coef: Vec::new(), state: [[0.0; 4]; 20], pre: 1.0 }
    }

    fn set(&mut self, on: bool, gains: [f32; 10], rate: u32) {
        self.on = on;
        self.gains = gains.map(|g| g.clamp(-12.0, 12.0));
        self.rate = rate;
        self.coef.clear();
        self.state = [[0.0; 4]; 20];
        let boost = self.gains.iter().cloned().fold(0.0f32, f32::max);
        self.pre = 10f32.powf(-boost / 20.0);
        if !on {
            return;
        }
        for (i, (&f, &g)) in EQ_BANDS.iter().zip(self.gains.iter()).enumerate() {
            if g.abs() < 0.05 || f >= rate as f32 * 0.45 {
                continue;
            }
            let a = 10f64.powf(g as f64 / 40.0);
            let w0 = 2.0 * std::f64::consts::PI * f as f64 / rate as f64;
            let alpha = w0.sin() / (2.0 * 1.41);
            let cos = w0.cos();
            let a0 = 1.0 + alpha / a;
            let c = [
                (1.0 + alpha * a) / a0,
                (-2.0 * cos) / a0,
                (1.0 - alpha * a) / a0,
                (-2.0 * cos) / a0,
                (1.0 - alpha / a) / a0,
            ];
            self.coef.push((i, c.map(|x| x as f32)));
        }
    }

    /// Interleaved stereo, in place.
    fn process(&mut self, buf: &mut [f32]) {
        if !self.on || self.coef.is_empty() {
            return;
        }
        for frame in buf.chunks_exact_mut(2) {
            for (ch, x) in frame.iter_mut().enumerate() {
                let mut v = *x * self.pre;
                for (band, c) in &self.coef {
                    let st = &mut self.state[band * 2 + ch];
                    let y = c[0] * v + c[1] * st[0] + c[2] * st[1] - c[3] * st[2] - c[4] * st[3];
                    st[1] = st[0];
                    st[0] = v;
                    st[3] = st[2];
                    st[2] = y;
                    v = y;
                }
                *x = v;
            }
        }
    }
}

/// Where an A–B loop jumps back to, if the song reached B (PLAN 0.16.4).
fn ab_jump(time: f64, ab: Option<(f64, f64)>) -> Option<f64> {
    ab.filter(|(a, b)| b > a && time >= *b).map(|(a, _)| a)
}

/// The output devices Windows offers, by name (PLAN 0.16.2).
pub fn output_devices() -> Vec<String> {
    use cpal::traits::{DeviceTrait, HostTrait};
    let Ok(devices) = cpal::default_host().output_devices() else { return Vec::new() };
    devices.filter_map(|d| d.description().ok().map(|x| x.name().to_string())).collect()
}

// -------------------------------------------------------------------- output
/// Room for the samples kept in front of the output: half a second of stereo even at 192 kHz.
const RING_CAP: usize = 1 << 18;

/// The decoded audio between the decoder thread (the only writer) and the output callback (the
/// only reader), without a lock: the callback never waits and never plays silence because the
/// decoder happened to be holding the buffer (it used to, with a Mutex and `try_lock`, when a game
/// kept the CPU busy). A flush (a seek, a new song) is asked by the writer and done by the
/// reader, so what was already played is counted exactly.
struct Ring {
    buf: Box<[AtomicU32]>, // f32 bits
    head: AtomicUsize,     // next sample to read; only the reader moves it
    tail: AtomicUsize,     // next sample to write; only the writer moves it
    flush_req: AtomicU64,
    flush_ack: AtomicU64,
}

impl Ring {
    fn new() -> Ring {
        Ring {
            buf: (0..RING_CAP).map(|_| AtomicU32::new(0)).collect(),
            head: AtomicUsize::new(0),
            tail: AtomicUsize::new(0),
            flush_req: AtomicU64::new(0),
            flush_ack: AtomicU64::new(0),
        }
    }

    /// Samples waiting to be played.
    fn len(&self) -> usize {
        self.tail.load(Ordering::Acquire).wrapping_sub(self.head.load(Ordering::Acquire))
    }

    /// Writer: appends what fits (the decoder never asks for more than the room it saw).
    fn push(&self, samples: &[f32]) {
        let tail = self.tail.load(Ordering::Relaxed);
        let room = RING_CAP - tail.wrapping_sub(self.head.load(Ordering::Acquire));
        let n = samples.len().min(room);
        for (i, x) in samples[..n].iter().enumerate() {
            self.buf[tail.wrapping_add(i) & (RING_CAP - 1)].store(x.to_bits(), Ordering::Relaxed);
        }
        self.tail.store(tail.wrapping_add(n), Ordering::Release);
    }

    /// Reader: does a pending flush first (dropping everything written before it was asked).
    fn take_flush(&self) {
        let req = self.flush_req.load(Ordering::Acquire);
        if req != self.flush_ack.load(Ordering::Relaxed) {
            self.head.store(self.tail.load(Ordering::Acquire), Ordering::Release);
            self.flush_ack.store(req, Ordering::Release);
        }
    }

    /// Reader: the next sample, if there is one.
    fn pop(&self) -> Option<f32> {
        let head = self.head.load(Ordering::Relaxed);
        if head == self.tail.load(Ordering::Acquire) {
            return None;
        }
        let x = f32::from_bits(self.buf[head & (RING_CAP - 1)].load(Ordering::Relaxed));
        self.head.store(head.wrapping_add(1), Ordering::Release);
        Some(x)
    }

    /// Reader: drops up to `n` samples (the silent clock of the test copies).
    fn skip(&self, n: usize) -> usize {
        let head = self.head.load(Ordering::Relaxed);
        let n = n.min(self.tail.load(Ordering::Acquire).wrapping_sub(head));
        self.head.store(head.wrapping_add(n), Ordering::Release);
        n
    }

    /// Writer: empties the ring. With a reader running, it asks and waits for the reader to do it
    /// (a callback comes every few ms); without one, or if the device stopped calling back, it
    /// does it itself, since nothing else is reading.
    fn flush(&self, reader: bool) {
        let req = self.flush_req.fetch_add(1, Ordering::AcqRel) + 1;
        if reader {
            let until = Instant::now() + Duration::from_millis(250);
            while self.flush_ack.load(Ordering::Acquire) < req && Instant::now() < until {
                std::thread::sleep(Duration::from_millis(1));
            }
        }
        if self.flush_ack.load(Ordering::Acquire) < req {
            self.head.store(self.tail.load(Ordering::Acquire), Ordering::Release);
            self.flush_ack.store(req, Ordering::Release);
        }
    }
}

/// What the output callback shares with the decoder thread.
struct Shared {
    ring: Ring,
    paused: AtomicBool,
    gain: AtomicU32,     // f32 bits
    consumed: AtomicU64, // frames played since the start
    failed: AtomicBool,  // the device went away (unplugged…): rebuild the stream
}

/// Where the audio goes: the default output, or, for test copies
/// (BAKIN_MUTE), a clock that takes it at real-time pace without hardware, so
/// tests behave the same on a PC with no headphones connected.
enum Output {
    Device(#[allow(dead_code)] cpal::Stream), // held: dropping it stops the sound
    Silent(Arc<AtomicBool>),
}

impl Drop for Output {
    fn drop(&mut self) {
        if let Output::Silent(stop) = self {
            stop.store(true, Ordering::Relaxed);
        }
    }
}

fn open_silent(shared: Arc<Shared>) -> (Output, u32) {
    let stop = Arc::new(AtomicBool::new(false));
    let s2 = stop.clone();
    let rate = 48_000u32;
    std::thread::spawn(move || {
        let tick = Duration::from_millis(10);
        while !s2.load(Ordering::Relaxed) {
            std::thread::sleep(tick);
            if shared.paused.load(Ordering::Relaxed) {
                shared.ring.take_flush();
                continue;
            }
            let frames = (rate / 100) as usize;
            shared.ring.take_flush();
            let have = shared.ring.skip(frames * 2) / 2;
            shared.consumed.fetch_add(have as u64, Ordering::Relaxed);
        }
    });
    (Output::Silent(stop), rate)
}

fn open_output(shared: Arc<Shared>, want: &str) -> Result<(Output, u32), String> {
    if std::env::var_os("BAKIN_MUTE").is_some() {
        return Ok(open_silent(shared));
    }
    let host = cpal::default_host();
    // the device chosen in Configuración, if it's there; else Windows' default
    let chosen = if want.is_empty() {
        None
    } else {
        host.output_devices().ok().and_then(|mut ds| ds.find(|d| d.description().map(|x| x.name() == want).unwrap_or(false)))
    };
    let device = chosen
        .or_else(|| host.default_output_device())
        .ok_or("no hay salida de audio: conecta o enciende los auriculares o parlantes")?;
    let config = device.default_output_config().map_err(|e| e.to_string())?;
    let rate = config.sample_rate();
    let channels = config.channels() as usize;
    let s2 = shared.clone();
    let s3 = shared.clone();
    let stream = device
        .build_output_stream::<f32, _, _>(
            config.config(),
            move |out: &mut [f32], _| {
                out.fill(0.0);
                let ring = &s2.ring;
                ring.take_flush();
                if s2.paused.load(Ordering::Relaxed) {
                    return;
                }
                let gain = f32::from_bits(s2.gain.load(Ordering::Relaxed));
                let frames = out.len() / channels.max(1);
                let have = (ring.len() / 2).min(frames);
                for f in 0..have {
                    let l = ring.pop().unwrap_or(0.0) * gain;
                    let r = ring.pop().unwrap_or(0.0) * gain;
                    let o = &mut out[f * channels..(f + 1) * channels];
                    if channels == 1 {
                        o[0] = (l + r) * 0.5;
                    } else {
                        o[0] = l;
                        o[1] = r;
                    }
                }
                s2.consumed.fetch_add(have as u64, Ordering::Relaxed);
            },
            move |_e| s3.failed.store(true, Ordering::Relaxed),
            None,
        )
        .map_err(|e| e.to_string())?;
    stream.play().map_err(|e| e.to_string())?;
    Ok((Output::Device(stream), rate))
}

// -------------------------------------------------------------------- player
#[derive(Clone, Copy, PartialEq, Serialize, Deserialize, Debug)]
#[serde(rename_all = "lowercase")]
enum Repeat {
    Off,
    All,
    One,
}

/// What kind of shuffle (a right click on its button picks it).
#[derive(Clone, Copy, PartialEq, Serialize, Deserialize, Debug, Default)]
#[serde(rename_all = "lowercase")]
enum ShuffleMode {
    /// Any order.
    #[default]
    Random,
    /// Favourites and what hasn't played in a month come up more often, and
    /// never the same artist twice in a row when there is another one.
    Smart,
    /// Whole albums in their order, the albums in any order.
    Album,
    /// An artist at a time, their songs mixed; the artists in any order (2026-11-06, the user).
    Artist,
}

/// The main artist of a song, for grouping: the first of «A, B», «A & B», «A feat. B»; from the
/// file name («Artist - Song») when the tags have none.
fn main_artist(t: &Track) -> String {
    let raw = if t.artist.trim().is_empty() {
        let stem = t.name.rsplit_once('.').map(|(a, _)| a).unwrap_or(&t.name);
        stem.split_once(" - ").map(|(a, _)| a).unwrap_or("")
    } else {
        t.artist.as_str()
    };
    let lower = raw.to_lowercase();
    let mut end = lower.len();
    for sep in [",", " & ", " feat", " ft.", " x ", ";"] {
        if let Some(i) = lower.find(sep) {
            end = end.min(i);
        }
    }
    lower[..end].trim().to_string()
}

/// Where in the output stream a song begins (its first frame).
struct Mark {
    at: u64,
    pos: usize,
    start: f64,
}

pub enum Command {
    Do(Value, Sender<Value>),
    Shutdown,
}

/// The handle the shell keeps: send commands, read the latest state.
pub struct Player {
    tx: Sender<Command>,
}

/// Writes a file atomically on a background thread, one at a time and in order; a newer version of
/// the same file waiting in line replaces the older one. With `wait`, returns once it's on disk
/// (closing Bakin): going through the same line, an older write can't land after it.
fn write_later(path: PathBuf, text: String, wait: bool) {
    type Job = (PathBuf, String, Option<Sender<()>>);
    static TX: OnceLock<Sender<Job>> = OnceLock::new();
    let tx = TX.get_or_init(|| {
        let (tx, rx) = mpsc::channel::<Job>();
        let _ = std::thread::Builder::new().name("bakin-player-save".into()).spawn(move || {
            while let Ok(mut job) = rx.recv() {
                while job.2.is_none() {
                    match rx.try_recv() {
                        Ok(newer) => {
                            if newer.0 != job.0 {
                                write_atomic(&job.0, &job.1);
                            }
                            job = newer;
                        }
                        Err(_) => break,
                    }
                }
                write_atomic(&job.0, &job.1);
                if let Some(done) = job.2 {
                    let _ = done.send(());
                }
            }
        });
        tx
    });
    let (done_tx, done_rx) = mpsc::channel();
    match tx.send((path, text, wait.then_some(done_tx))) {
        Ok(()) if wait => {
            let _ = done_rx.recv_timeout(Duration::from_secs(5));
        }
        Ok(()) => {}
        Err(mpsc::SendError((path, text, _))) => write_atomic(&path, &text),
    }
}

fn write_atomic(path: &Path, text: &str) {
    let tmp = path.with_extension("tmp");
    if std::fs::write(&tmp, text).is_ok() {
        let _ = std::fs::rename(&tmp, path);
    }
}

impl Player {
    pub fn start(emit: impl Fn(&str, Value) + Send + 'static) -> Player {
        let (tx, rx) = mpsc::channel();
        std::thread::Builder::new()
            .name("bakin-player".into())
            .spawn(move || {
                // the decoder keeps the output fed: above the usual threads, so a game or a scan
                // busy on every core doesn't leave the ring empty (the output thread itself is
                // already real time: cpal registers it with MMCSS)
                #[cfg(windows)]
                unsafe {
                    use windows::Win32::System::Threading::{GetCurrentThread, SetThreadPriority, THREAD_PRIORITY_ABOVE_NORMAL};
                    let _ = SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
                }
                Engine::new(Box::new(emit)).run(rx)
            })
            .expect("player thread");
        Player { tx }
    }

    /// Runs a command (the page's JSON) and returns the player's state.
    pub fn call(&self, cmd: Value) -> Result<Value, String> {
        let (tx, rx) = mpsc::channel();
        self.tx.send(Command::Do(cmd, tx)).map_err(|_| "el reproductor no responde".to_string())?;
        rx.recv_timeout(Duration::from_secs(5)).map_err(|_| "el reproductor no responde".to_string())
    }

    pub fn shutdown(&self) {
        let _ = self.tx.send(Command::Shutdown);
    }
}

struct Engine {
    emit: Box<dyn Fn(&str, Value) + Send>,
    shared: Arc<Shared>,
    stream: Option<Output>,
    want_play: bool,     // play was asked for while there was no output: start when one appears
    output_tried: Instant,
    rate: u32,
    // queue
    tracks: Vec<Track>,
    order: Vec<usize>,
    pos: usize,
    queued: usize,
    shuffle: bool,
    shuffle_mode: ShuffleMode,
    repeat: Repeat,
    source_name: String,
    // settings
    volume: f32,
    muted: bool,
    crossfade: u32,
    loudness: bool,             // apply each song's gain_db (PLAN 0.16.1)
    eq: Eq,                     // PLAN 0.16.2
    device: String,             // "" = Windows' default
    ab: Option<(f64, f64)>,     // A–B repeat (PLAN 0.16.4), seconds in the song playing
    // playing
    playing: bool,
    finished: bool, // the queue ran out: play starts the song again
    cur: Option<Source>,
    cur_pos: usize, // order position being decoded (may be ahead of the one heard)
    fade: Option<(Source, usize, u64)>, // next song during a crossfade: source, pos, frames mixed
    marks: VecDeque<Mark>,
    written: u64,
    resume_at: f64,
    heard_pos: usize,
    // plays and timers
    listened: f64,
    counted: bool,
    last_consumed: u64,
    sleep_at: Option<Instant>,
    sleep_end: bool,
    errors: u32,
    // saving
    dirty: bool,
    saved_at: Instant,
    pos_saved_at: Instant,
    told_at: Instant,
}

impl Engine {
    fn new(emit: Box<dyn Fn(&str, Value) + Send>) -> Engine {
        let shared = Arc::new(Shared {
            ring: Ring::new(),
            paused: AtomicBool::new(true),
            gain: AtomicU32::new(0.8f32.to_bits()),
            consumed: AtomicU64::new(0),
            failed: AtomicBool::new(false),
        });
        let mut e = Engine {
            emit,
            shared,
            stream: None,
            want_play: false,
            output_tried: Instant::now(),
            rate: 48_000,
            tracks: Vec::new(),
            order: Vec::new(),
            pos: 0,
            queued: 0,
            shuffle: false,
            shuffle_mode: ShuffleMode::Random,
            repeat: Repeat::Off,
            source_name: String::new(),
            volume: 0.8,
            muted: false,
            crossfade: 0,
            loudness: false,
            eq: Eq::new(48_000),
            device: String::new(),
            ab: None,
            playing: false,
            finished: false,
            cur: None,
            cur_pos: 0,
            fade: None,
            marks: VecDeque::new(),
            written: 0,
            resume_at: 0.0,
            heard_pos: 0,
            listened: 0.0,
            counted: false,
            last_consumed: 0,
            sleep_at: None,
            sleep_end: false,
            errors: 0,
            dirty: false,
            saved_at: Instant::now(),
            pos_saved_at: Instant::now(),
            told_at: Instant::now(),
        };
        e.restore();
        e.apply_gain();
        e
    }

    // ------------------------------------------------------------- the loop
    fn run(mut self, rx: Receiver<Command>) {
        loop {
            let wait = if self.playing { Duration::from_millis(20) } else { Duration::from_millis(250) };
            match rx.recv_timeout(wait) {
                Ok(Command::Do(cmd, reply)) => {
                    let out = self.handle(&cmd);
                    let _ = reply.send(out);
                }
                Ok(Command::Shutdown) | Err(RecvTimeoutError::Disconnected) => {
                    self.save_with(true);
                    break;
                }
                Err(RecvTimeoutError::Timeout) => {}
            }
            if self.playing {
                self.fill();
                self.follow_output();
                if let Some(a) = ab_jump(self.time(), self.ab) {
                    self.seek(a);
                }
            }
            if self.shared.failed.swap(false, Ordering::Relaxed) {
                self.reopen_output();
            }
            // waiting for headphones or speakers to appear
            if self.want_play && self.output_tried.elapsed() > Duration::from_secs(2) {
                self.output_tried = Instant::now();
                if self.ensure_output() {
                    self.want_play = false;
                    self.play();
                    (self.emit)("notice", json!({ "message": "Volvió la salida de audio: sigue la música." }));
                }
            }
            if self.sleep_at.is_some_and(|t| Instant::now() >= t) {
                self.sleep_at = None;
                self.pause();
                (self.emit)("notice", json!({ "message": "Temporizador: se detuvo la música." }));
            }
            // the queue file only after a change (a song, a pause, the queue edited); where in
            // the song goes to its own small file, often, so nothing big is written while it plays
            if self.dirty && self.saved_at.elapsed() > Duration::from_secs(2) {
                self.save();
            }
            if self.playing && self.pos_saved_at.elapsed() > Duration::from_secs(5) {
                self.save_position();
            }
            if self.playing && self.told_at.elapsed() > Duration::from_millis(500) {
                self.told_at = Instant::now();
                (self.emit)("time", json!({ "time": self.time(), "duration": self.duration() }));
            }
        }
    }

    fn ensure_output(&mut self) -> bool {
        if self.stream.is_some() {
            return true;
        }
        match open_output(self.shared.clone(), &self.device) {
            Ok((stream, rate)) => {
                if rate != self.rate {
                    self.rate = rate;
                    let (on, gains) = (self.eq.on, self.eq.gains);
                    self.eq.set(on, gains, rate);
                }
                self.stream = Some(stream);
                true
            }
            Err(e) => {
                if !self.want_play {
                    (self.emit)("error", json!({ "message": format!("No se puede reproducir: {e}. Bakin empieza solo cuando haya una salida.") }));
                }
                false
            }
        }
    }

    /// Another device chosen: the stream is made again on it, the song goes on where it was.
    fn switch_output(&mut self) {
        let was_playing = self.playing;
        let at = self.time();
        if was_playing {
            self.pause();
        }
        self.stream = None;
        self.cur = None;
        self.resume_at = at;
        if was_playing && self.ensure_output() {
            self.play();
        }
    }

    fn reopen_output(&mut self) {
        let was_playing = self.playing;
        if was_playing {
            self.pause();
        }
        self.stream = None;
        self.cur = None; // decoded for the old output's rate
        if was_playing {
            self.want_play = true;
            self.output_tried = Instant::now();
            (self.emit)("notice", json!({ "message": "Se desconectó la salida de audio: la música sigue cuando vuelva." }));
        }
    }

    /// Keeps about half a second decoded in front of the output.
    fn fill(&mut self) {
        let target = (AHEAD_SECS * self.rate as f64) as usize * 2;
        let have = self.shared.ring.len();
        if have >= target {
            return;
        }
        let want = (target - have) / 2;
        let mut buf = Vec::with_capacity(want * 2);
        let mut got = 0;
        while got < want {
            let need = want - got;
            let start = buf.len();
            let n = match self.cur.as_mut() {
                Some(cur) => cur.read(&mut buf, need),
                None => break,
            };
            // crossfade: the next song comes in over the last seconds
            if let Some((next, _, mixed)) = self.fade.as_mut() {
                let total = (self.crossfade as f64 * self.rate as f64).max(1.0);
                let mut other = Vec::with_capacity(n * 2);
                let m = next.read(&mut other, n);
                for f in 0..n {
                    let k = (((*mixed + f as u64) as f64) / total).min(1.0) as f32;
                    let i = start + f * 2;
                    let (ol, or) = if f < m { (other[f * 2], other[f * 2 + 1]) } else { (0.0, 0.0) };
                    buf[i] = buf[i] * (1.0 - k) + ol * k;
                    buf[i + 1] = buf[i + 1] * (1.0 - k) + or * k;
                }
                *mixed += n as u64;
            }
            got += n;
            if n < need {
                // this song ended: the next one continues the same stream
                if !self.next_source(got as u64) {
                    break;
                }
            } else if self.fade.is_none() {
                self.maybe_start_fade(got as u64);
            }
        }
        if buf.is_empty() {
            return;
        }
        self.eq.process(&mut buf);
        self.shared.ring.push(&buf);
        self.written += got as u64;
    }

    fn maybe_start_fade(&mut self, offset: u64) {
        if self.crossfade == 0 || self.repeat == Repeat::One || self.sleep_end {
            return;
        }
        let Some(cur) = self.cur.as_ref() else { return };
        let Some(dur) = self.tracks.get(self.order[self.cur_pos]).and_then(|t| t.duration) else { return };
        let left = dur - cur.position();
        if dur < self.crossfade as f64 * 3.0 || left > self.crossfade as f64 {
            return;
        }
        let Some(next) = self.after(self.cur_pos, true) else { return };
        if next == self.cur_pos {
            return;
        }
        if let Ok(src) = self.open_at(next, 0.0) {
            self.marks.push_back(Mark { at: self.written + offset, pos: next, start: 0.0 });
            self.fade = Some((src, next, 0));
        }
    }

    /// The decoded song ended at `offset` frames into this fill: go on with
    /// the next one (or the one fading in). False when nothing follows.
    fn next_source(&mut self, offset: u64) -> bool {
        if let Some((src, pos, _)) = self.fade.take() {
            self.cur = Some(src);
            self.cur_pos = pos;
            return true;
        }
        if self.sleep_end {
            self.cur = None;
            return false;
        }
        let Some(next) = self.after(self.cur_pos, true) else {
            self.cur = None;
            return false;
        };
        let mut tries = 0;
        let mut pos = next;
        loop {
            match self.open_at(pos, 0.0) {
                Ok(src) => {
                    self.cur = Some(src);
                    self.cur_pos = pos;
                    self.marks.push_back(Mark { at: self.written + offset, pos, start: 0.0 });
                    return true;
                }
                Err(e) => {
                    self.report_error(pos, &e);
                    tries += 1;
                    match self.after(pos, false) {
                        Some(p) if tries < 5 && p != pos => pos = p,
                        _ => {
                            self.cur = None;
                            return false;
                        }
                    }
                }
            }
        }
    }

    /// Notices songs starting at the output, play counts, the end of the queue.
    fn follow_output(&mut self) {
        let consumed = self.shared.consumed.load(Ordering::Relaxed);
        let delta = consumed.saturating_sub(self.last_consumed);
        self.last_consumed = consumed;
        self.listened += delta as f64 / self.rate as f64;
        let mut changed = false;
        while self.marks.len() > 1 && self.marks[1].at <= consumed {
            self.marks.pop_front();
            changed = true;
        }
        if changed {
            let pos = self.marks[0].pos;
            if pos != self.pos && self.queued > 0 {
                self.queued -= 1;
            }
            self.pos = pos;
            self.heard_pos = pos;
            self.listened = 0.0;
            self.counted = false;
            self.dirty = true;
            self.tell_state();
        }
        if !self.counted {
            if let Some((id, dur)) = self.current().map(|t| (t.id.clone(), t.duration)) {
                // a play counts after half the song or four minutes, as most players do
                let need = dur.map(|d| (d / 2.0).min(240.0)).unwrap_or(240.0);
                if self.listened >= need {
                    self.counted = true;
                    (self.emit)("played", json!({ "id": id }));
                }
            }
        }
        // the queue ran out and the output has played everything
        if self.cur.is_none() && consumed >= self.written {
            self.playing = false;
            self.shared.paused.store(true, Ordering::Relaxed);
            self.finished = true;
            if self.sleep_end {
                self.sleep_end = false;
                (self.emit)("notice", json!({ "message": "Temporizador: terminó el tema y se detuvo la música." }));
            }
            self.dirty = true;
            self.tell_state();
        }
    }

    // ------------------------------------------------------------ queue logic
    fn current(&self) -> Option<&Track> {
        self.order.get(self.pos).and_then(|&i| self.tracks.get(i))
    }

    /// The position after `pos`: when a song ends (`auto`) or on «siguiente».
    fn after(&self, pos: usize, auto: bool) -> Option<usize> {
        if self.order.is_empty() {
            return None;
        }
        if auto && self.repeat == Repeat::One {
            return Some(pos);
        }
        if pos + 1 < self.order.len() {
            Some(pos + 1)
        } else if self.repeat == Repeat::All {
            Some(0)
        } else {
            None
        }
    }

    fn time(&self) -> f64 {
        let consumed = self.shared.consumed.load(Ordering::Relaxed);
        match self.marks.front() {
            Some(m) if self.stream.is_some() => m.start + consumed.saturating_sub(m.at) as f64 / self.rate as f64,
            _ => self.resume_at,
        }
    }

    fn duration(&self) -> Option<f64> {
        self.current().and_then(|t| t.duration)
    }

    /// Starts decoding song `pos` at `at` seconds, dropping what was ready.
    fn load_at(&mut self, pos: usize, at: f64) -> bool {
        if pos >= self.order.len() {
            return false;
        }
        if !self.ensure_output() {
            self.pos = pos;
            self.resume_at = at.max(0.0);
            self.want_play = true;
            self.output_tried = Instant::now();
            self.dirty = true;
            return false;
        }
        self.shared.ring.flush(self.stream.is_some());
        let consumed = self.shared.consumed.load(Ordering::Relaxed);
        self.written = consumed;
        self.last_consumed = consumed;
        self.marks.clear();
        self.fade = None;
        let mut pos = pos;
        let mut tries = 0;
        loop {
            match self.open_at(pos, at.max(0.0)) {
                Ok(src) => {
                    self.cur = Some(src);
                    self.cur_pos = pos;
                    self.marks.push_back(Mark { at: consumed, pos, start: at.max(0.0) });
                    break;
                }
                Err(e) => {
                    self.report_error(pos, &e);
                    tries += 1;
                    match self.after(pos, false) {
                        Some(p) if tries < 5 && p != pos => pos = p,
                        _ => {
                            self.cur = None;
                            self.playing = false;
                            self.shared.paused.store(true, Ordering::Relaxed);
                            return false;
                        }
                    }
                }
            }
        }
        if pos != self.pos {
            self.listened = 0.0;
            self.counted = false;
        }
        self.pos = pos;
        self.heard_pos = pos;
        self.finished = false;
        self.resume_at = 0.0;
        self.dirty = true;
        true
    }

    fn report_error(&mut self, pos: usize, e: &str) {
        self.errors += 1;
        let title = self.tracks.get(self.order[pos]).map(|t| t.title.clone()).unwrap_or_default();
        (self.emit)("error", json!({ "message": format!("No se pudo reproducir «{title}»: {e}.") }));
    }

    fn play(&mut self) {
        if self.order.is_empty() {
            return;
        }
        if !self.ensure_output() {
            self.want_play = true;
            self.output_tried = Instant::now();
            self.tell_state();
            return;
        }
        if self.cur.is_none() || self.finished {
            let at = if self.finished { 0.0 } else { self.resume_at };
            if !self.load_at(self.pos, at) {
                self.tell_state();
                return;
            }
        }
        self.playing = true;
        self.shared.paused.store(false, Ordering::Relaxed);
        self.fill();
        self.tell_state();
    }

    fn pause(&mut self) {
        self.want_play = false;
        if !self.playing {
            return;
        }
        self.resume_at = self.time();
        self.playing = false;
        self.shared.paused.store(true, Ordering::Relaxed);
        self.dirty = true;
        self.save();
        self.tell_state();
    }

    fn jump(&mut self, pos: usize, play: bool) {
        if pos >= self.order.len() {
            return;
        }
        self.errors = 0;
        if play {
            if self.load_at(pos, 0.0) {
                self.playing = true;
                self.shared.paused.store(false, Ordering::Relaxed);
                self.fill();
            }
        } else {
            self.pos = pos;
            self.cur = None;
            self.marks.clear();
            self.resume_at = 0.0;
            self.dirty = true;
        }
        self.tell_state();
        self.tell_queue();
    }

    fn skip(&mut self, step: i64) {
        if self.order.is_empty() {
            return;
        }
        if step < 0 && self.time() > 3.0 {
            let pos = self.pos;
            let play = self.playing;
            self.jump(pos, play);
            return;
        }
        // «next» before 30 s of a song that hadn't counted as a play: a skip, which the mixes
        // weigh down (PLAN 0.14.4)
        if step > 0 && !self.counted && self.time() < 30.0 {
            if let Some(id) = self.current().map(|t| t.id.clone()) {
                (self.emit)("skipped", json!({ "id": id }));
            }
        }
        let len = self.order.len() as i64;
        let mut p = self.pos as i64 + step;
        if p >= len {
            if self.repeat == Repeat::All { p = 0 } else { return }
        }
        if p < 0 {
            p = if self.repeat == Repeat::All { len - 1 } else { 0 };
        }
        if step > 0 && self.queued > 0 {
            self.queued -= 1;
        }
        self.jump(p as usize, true);
    }

    fn seek(&mut self, to: f64) {
        let to = to.max(0.0);
        if self.cur.is_none() {
            self.resume_at = to;
            self.tell_state();
            return;
        }
        let pos = self.pos;
        if self.load_at(pos, to) && self.playing {
            self.fill();
        }
        self.tell_state();
    }

    fn set_shuffle(&mut self, on: bool) {
        self.shuffle = on;
        if !self.order.is_empty() {
            let current = self.order[self.pos];
            if on {
                self.order = shuffled(&self.tracks, current, self.shuffle_mode);
            } else {
                self.order = (0..self.tracks.len()).collect();
            }
            self.pos = self.order.iter().position(|&i| i == current).unwrap_or(0);
            self.cur_pos = self.pos;
            self.queued = 0;
            self.drop_lookahead();
            self.tell_queue();
        }
        self.dirty = true;
    }

    /// Order or repeat changed: what was decoded past the current song is
    /// no longer what comes next.
    fn drop_lookahead(&mut self) {
        if self.playing && (self.marks.len() > 1 || self.fade.is_some() || self.cur_pos != self.pos) {
            let at = self.time();
            let pos = self.pos;
            self.load_at(pos, at);
            self.fill();
        }
    }

    fn load_queue(&mut self, tracks: Vec<Track>, start: usize, source: String, play: bool) {
        let tracks: Vec<Track> = tracks.into_iter().take(MAX_QUEUE).collect();
        if tracks.is_empty() {
            return;
        }
        let start = start.min(tracks.len() - 1);
        let order: Vec<usize> = if self.shuffle { shuffled(&tracks, start, self.shuffle_mode) } else { (0..tracks.len()).collect() };
        self.tracks = tracks;
        self.order = order;
        self.queued = 0;
        self.source_name = source;
        let pos = self.order.iter().position(|&i| i == start).unwrap_or(0);
        self.errors = 0;
        self.jump(pos, play);
        self.tell_queue();
    }

    fn enqueue(&mut self, tracks: Vec<Track>) {
        if tracks.is_empty() {
            return;
        }
        if self.order.is_empty() {
            self.load_queue(tracks, 0, "Cola".into(), true);
            return;
        }
        let first = self.tracks.len();
        let n = tracks.len();
        self.tracks.extend(tracks);
        let at = (self.pos + 1 + self.queued).min(self.order.len());
        self.order.splice(at..at, first..first + n);
        self.queued += n;
        self.drop_lookahead();
        self.dirty = true;
        self.tell_queue();
    }

    fn remove(&mut self, pos: usize) {
        if pos >= self.order.len() || pos == self.pos {
            return;
        }
        self.order.remove(pos);
        if pos < self.pos {
            self.pos -= 1;
            self.cur_pos = self.cur_pos.saturating_sub(1);
            for m in self.marks.iter_mut() {
                m.pos = m.pos.saturating_sub(1);
            }
        } else if pos <= self.pos + self.queued {
            self.queued = self.queued.saturating_sub(1);
        }
        self.drop_lookahead();
        self.dirty = true;
        self.tell_queue();
    }

    /// Cola, dragged (pulido P1 #4): a song that comes later moves to another later place; the
    /// one playing and the ones already played stay put. «Play next» songs keep their count.
    fn move_to(&mut self, from: usize, to: usize) {
        let len = self.order.len();
        if from >= len || to >= len || from == to || from <= self.pos || to <= self.pos {
            return;
        }
        let next = |p: usize, s: &Self| p > s.pos && p <= s.pos + s.queued;
        let (was, will) = (next(from, self), next(to, self));
        let song = self.order.remove(from);
        self.order.insert(to, song);
        if was && !will {
            self.queued = self.queued.saturating_sub(1);
        } else if !was && will {
            self.queued += 1;
        }
        self.drop_lookahead();
        self.dirty = true;
        self.tell_queue();
    }

    fn clear_after(&mut self) {
        self.order.truncate(self.pos + 1);
        self.queued = 0;
        self.drop_lookahead();
        self.dirty = true;
        self.tell_queue();
    }

    fn set_fav(&mut self, id: &str, fav: bool) {
        for t in self.tracks.iter_mut().filter(|t| t.id == id) {
            t.fav = fav;
        }
        self.dirty = true;
        self.tell_state();
    }

    /// A song of the order opened at `start`, with its even-volume gain.
    fn open_at(&self, pos: usize, start: f64) -> Result<Source, String> {
        let mut src = Source::open(&self.tracks[self.order[pos]].path, start, self.rate)?;
        src.gain = self.track_gain(pos);
        Ok(src)
    }

    fn track_gain(&self, pos: usize) -> f32 {
        match self.order.get(pos).and_then(|&i| self.tracks.get(i)).and_then(|t| t.gain_db) {
            Some(db) if self.loudness => 10f32.powf(db.clamp(-24.0, 12.0) / 20.0),
            _ => 1.0,
        }
    }

    /// The gains changed (another mode, a new measure): the songs ahead use them now.
    fn regain(&mut self) {
        let g = self.track_gain(self.cur_pos);
        if let Some(cur) = self.cur.as_mut() {
            cur.gain = g;
        }
        if let Some(p) = self.fade.as_ref().map(|f| f.1) {
            let g = self.track_gain(p);
            if let Some((src, _, _)) = self.fade.as_mut() {
                src.gain = g;
            }
        }
    }

    fn apply_gain(&self) {
        // BAKIN_MUTE: test copies play everything without a sound
        let silent = self.muted || std::env::var_os("BAKIN_MUTE").is_some();
        let g = if silent { 0.0 } else { self.volume.clamp(0.0, 1.0) };
        // a gentle curve: the slider's middle sounds like half as loud
        self.shared.gain.store((g * g).to_bits(), Ordering::Relaxed);
    }

    // ----------------------------------------------------------- commands
    fn handle(&mut self, cmd: &Value) -> Value {
        let s = |k: &str| cmd.get(k);
        match cmd.get("cmd").and_then(|c| c.as_str()).unwrap_or("") {
            "load" => {
                let tracks: Vec<Track> = serde_json::from_value(s("tracks").cloned().unwrap_or(json!([]))).unwrap_or_default();
                let start = s("start").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let source = s("source").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let play = s("play").and_then(|v| v.as_bool()).unwrap_or(true);
                self.load_queue(tracks, start, source, play);
            }
            "enqueue" => {
                let tracks: Vec<Track> = serde_json::from_value(s("tracks").cloned().unwrap_or(json!([]))).unwrap_or_default();
                self.enqueue(tracks);
            }
            "play" => self.play(),
            "pause" => self.pause(),
            "toggle" => {
                if self.playing { self.pause() } else { self.play() }
            }
            "next" => self.skip(1),
            "prev" => self.skip(-1),
            "jump" => {
                let pos = s("pos").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                if pos > self.pos {
                    self.queued = self.queued.saturating_sub(pos - self.pos);
                }
                self.jump(pos, true);
            }
            "seek" => self.seek(s("time").and_then(|v| v.as_f64()).unwrap_or(0.0)),
            "skip_secs" => {
                let d = s("secs").and_then(|v| v.as_f64()).unwrap_or(0.0);
                let t = self.time();
                self.seek(t + d);
            }
            "remove" => self.remove(s("pos").and_then(|v| v.as_u64()).unwrap_or(u64::MAX) as usize),
            "clear_after" => self.clear_after(),
            "move" => {
                let at = |k: &str| s(k).and_then(|v| v.as_u64()).unwrap_or(u64::MAX) as usize;
                self.move_to(at("from"), at("to"));
            }
            "fav" => {
                let id = s("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                self.set_fav(&id, s("value").and_then(|v| v.as_bool()).unwrap_or(false));
            }
            "set" => {
                if let Some(v) = s("volume").and_then(|v| v.as_f64()) {
                    self.volume = v.clamp(0.0, 1.0) as f32;
                    self.muted = false;
                }
                if let Some(m) = s("muted").and_then(|v| v.as_bool()) {
                    self.muted = m;
                }
                if let Some(r) = s("repeat").and_then(|v| serde_json::from_value::<Repeat>(v.clone()).ok()) {
                    self.repeat = r;
                    self.drop_lookahead();
                }
                // another kind of shuffle: a new order now, if shuffle is on
                let mut reshuffle = false;
                if let Some(m) = s("shuffle_mode").and_then(|v| serde_json::from_value::<ShuffleMode>(v.clone()).ok()) {
                    if m != self.shuffle_mode {
                        self.shuffle_mode = m;
                        self.dirty = true;
                        reshuffle = self.shuffle;
                    }
                }
                if let Some(sh) = s("shuffle").and_then(|v| v.as_bool()) {
                    if sh != self.shuffle {
                        self.set_shuffle(sh);
                        reshuffle = false;
                    }
                }
                if reshuffle {
                    self.set_shuffle(true);
                }
                if let Some(c) = s("crossfade").and_then(|v| v.as_u64()) {
                    self.crossfade = c.min(12) as u32;
                    self.drop_lookahead();
                }
                if let Some(on) = s("loudness").and_then(|v| v.as_bool()) {
                    self.loudness = on;
                    self.regain();
                }
                if let Some(eq) = s("eq") {
                    let on = eq.get("on").and_then(|v| v.as_bool()).unwrap_or(self.eq.on);
                    let mut gains = self.eq.gains;
                    if let Some(list) = eq.get("gains").and_then(|v| v.as_array()) {
                        for (i, g) in list.iter().take(10).enumerate() {
                            gains[i] = g.as_f64().unwrap_or(0.0) as f32;
                        }
                    }
                    let rate = self.rate;
                    self.eq.set(on, gains, rate);
                }
                if let Some(d) = s("device").and_then(|v| v.as_str()) {
                    if d != self.device {
                        self.device = d.to_string();
                        self.switch_output();
                    }
                }
                if let Some(ab) = s("ab") {
                    self.ab = ab.as_array().and_then(|v| Some((v.first()?.as_f64()?, v.get(1)?.as_f64()?))).filter(|(a, b)| b > a);
                }
                if let Some(sleep) = s("sleep") {
                    self.sleep_end = sleep.as_str() == Some("end");
                    self.sleep_at = sleep.as_f64().filter(|m| *m > 0.0).map(|m| Instant::now() + Duration::from_secs_f64(m * 60.0));
                }
                self.apply_gain();
                self.dirty = true;
                self.tell_state();
            }
            "queue" => return self.queue_json(),
            "devices" => return json!({ "devices": output_devices(), "device": self.device }),
            "gains" => {
                // {id: dB} from the engine (a new measure, another mode): kept on the songs
                if let Some(map) = s("gains").and_then(|v| v.as_object()) {
                    for t in self.tracks.iter_mut() {
                        if let Some(g) = map.get(&t.id) {
                            t.gain_db = g.as_f64().map(|x| x as f32);
                        }
                    }
                    self.regain();
                    self.dirty = true;
                }
            }
            "save" => self.save_with(true),
            _ => {}
        }
        self.state_json()
    }

    // -------------------------------------------------------------- events
    fn state_json(&self) -> Value {
        let sleep = if self.sleep_end {
            json!("end")
        } else if let Some(t) = self.sleep_at {
            json!(now_secs() + t.saturating_duration_since(Instant::now()).as_secs_f64())
        } else {
            Value::Null
        };
        json!({
            "track": self.current(),
            "pos": self.pos,
            "count": self.order.len(),
            "queued": self.queued,
            "playing": self.playing,
            "waiting_output": self.want_play,
            "time": self.time(),
            "duration": self.duration(),
            "shuffle": self.shuffle,
            "shuffle_mode": self.shuffle_mode,
            "repeat": self.repeat,
            "volume": self.volume,
            "muted": self.muted,
            "crossfade": self.crossfade,
            "loudness": self.loudness,
            "eq": { "on": self.eq.on, "gains": self.eq.gains },
            "device": self.device,
            "ab": self.ab.map(|(a, b)| json!([a, b])),
            "sleep": sleep,
            "source": self.source_name,
        })
    }

    fn queue_json(&self) -> Value {
        let ordered: Vec<&Track> = self.order.iter().filter_map(|&i| self.tracks.get(i)).collect();
        json!({ "tracks": ordered, "pos": self.pos, "queued": self.queued, "source": self.source_name })
    }

    fn tell_state(&mut self) {
        let st = self.state_json();
        (self.emit)("state", st);
    }

    fn tell_queue(&mut self) {
        (self.emit)("queue", json!({ "count": self.order.len(), "pos": self.pos }));
    }

    // ------------------------------------------------------------ saving
    fn state_file() -> PathBuf {
        data_dir().join("music").join("player.json")
    }

    fn save(&mut self) {
        self.save_with(false);
    }

    /// `wait`: on disk before returning (Bakin is closing).
    fn save_with(&mut self, wait: bool) {
        self.dirty = false;
        self.saved_at = Instant::now();
        let data = json!({
            "tracks": self.tracks,
            "ids": self.tracks.iter().map(|t| t.id.clone()).collect::<Vec<_>>(),
            "order": self.order,
            "pos": self.pos,
            "queued": self.queued,
            "time": if self.playing { self.time() } else { self.resume_at },
            "shuffle": self.shuffle,
            "shuffle_mode": self.shuffle_mode,
            "repeat": self.repeat,
            "volume": self.volume,
            "muted": self.muted,
            "crossfade": self.crossfade,
            "loudness": self.loudness,
            "eq": { "on": self.eq.on, "gains": self.eq.gains },
            "device": self.device,
            "source": self.source_name,
        });
        let path = Self::state_file();
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        self.save_position();
        // the queue can be megabytes: written by another thread, so the decoder never waits on the disk
        write_later(path, data.to_string(), wait);
    }

    fn position_file() -> PathBuf {
        data_dir().join("music").join("player-position.json")
    }

    fn save_position(&mut self) {
        self.pos_saved_at = Instant::now();
        let id = self.current().map(|t| t.id.clone()).unwrap_or_default();
        let data = json!({ "pos": self.pos, "id": id, "time": self.time() });
        let path = Self::position_file();
        let tmp = path.with_extension("tmp2");
        if std::fs::write(&tmp, data.to_string()).is_ok() {
            let _ = std::fs::rename(&tmp, &path);
        }
    }

    fn restore(&mut self) {
        let Ok(text) = std::fs::read_to_string(Self::state_file()) else { return };
        let Ok(v) = serde_json::from_str::<Value>(&text) else { return };
        self.volume = v.get("volume").and_then(|x| x.as_f64()).unwrap_or(0.8).clamp(0.0, 1.0) as f32;
        self.muted = v.get("muted").and_then(|x| x.as_bool()).unwrap_or(false);
        self.shuffle = v.get("shuffle").and_then(|x| x.as_bool()).unwrap_or(false);
        self.shuffle_mode = v.get("shuffle_mode").and_then(|x| serde_json::from_value(x.clone()).ok()).unwrap_or_default();
        self.repeat = v.get("repeat").and_then(|x| serde_json::from_value(x.clone()).ok()).unwrap_or(Repeat::Off);
        self.crossfade = v.get("crossfade").and_then(|x| x.as_u64()).unwrap_or(0).min(12) as u32;
        self.loudness = v.get("loudness").and_then(|x| x.as_bool()).unwrap_or(false);
        if let Some(eq) = v.get("eq") {
            let mut gains = [0.0f32; 10];
            for (i, g) in eq.get("gains").and_then(|x| x.as_array()).into_iter().flatten().take(10).enumerate() {
                gains[i] = g.as_f64().unwrap_or(0.0) as f32;
            }
            let rate = self.rate;
            self.eq.set(eq.get("on").and_then(|x| x.as_bool()).unwrap_or(false), gains, rate);
        }
        self.device = v.get("device").and_then(|x| x.as_str()).unwrap_or("").to_string();
        self.source_name = v.get("source").and_then(|x| x.as_str()).unwrap_or("").to_string();
        self.apply_gain();
        // 0.6 saved only ids: the page fills the songs in (it knows the paths)
        let tracks: Vec<Track> = v.get("tracks").and_then(|x| serde_json::from_value(x.clone()).ok()).unwrap_or_default();
        if tracks.is_empty() {
            return;
        }
        let n = tracks.len();
        let order: Vec<usize> = v
            .get("order")
            .and_then(|x| serde_json::from_value::<Vec<usize>>(x.clone()).ok())
            .filter(|o| { let mut s = o.clone(); s.sort_unstable(); s == (0..n).collect::<Vec<_>>() })
            .unwrap_or_else(|| (0..n).collect());
        self.tracks = tracks;
        self.order = order;
        self.pos = (v.get("pos").and_then(|x| x.as_u64()).unwrap_or(0) as usize).min(n - 1);
        self.queued = v.get("queued").and_then(|x| x.as_u64()).unwrap_or(0) as usize;
        self.resume_at = v.get("time").and_then(|x| x.as_f64()).unwrap_or(0.0).max(0.0);
        // a newer position (saved every few seconds while playing) for the same song
        if let Some(p) = std::fs::read_to_string(Self::position_file()).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
            let pos = p.get("pos").and_then(|x| x.as_u64()).unwrap_or(u64::MAX) as usize;
            let same = self.order.get(pos).and_then(|&i| self.tracks.get(i)).map(|t| Some(t.id.as_str()) == p.get("id").and_then(|x| x.as_str()));
            if same == Some(true) {
                self.pos = pos;
                self.resume_at = p.get("time").and_then(|x| x.as_f64()).unwrap_or(self.resume_at).max(0.0);
            }
        }
    }
}

/// A small xorshift seeded from the clock (no crate needed).
struct Rng(u64);

impl Rng {
    fn new() -> Self {
        Rng(SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0x9E37_79B9) | 1)
    }
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    /// In (0, 1].
    fn unit(&mut self) -> f64 {
        ((self.next() >> 11) as f64 + 1.0) / (1u64 << 53) as f64
    }
}

/// Fisher–Yates.
fn shuffle_with(items: &mut [usize], rng: &mut Rng) {
    for i in (1..items.len()).rev() {
        let j = (rng.next() % (i as u64 + 1)) as usize;
        items.swap(i, j);
    }
}

/// The play order for a shuffle of this kind, `first` (the song playing or
/// picked) at the top.
fn shuffled(tracks: &[Track], first: usize, mode: ShuffleMode) -> Vec<usize> {
    let mut rng = Rng::new();
    let mut rest: Vec<usize> = (0..tracks.len()).filter(|&i| i != first).collect();
    match mode {
        ShuffleMode::Random => {
            shuffle_with(&mut rest, &mut rng);
            std::iter::once(first).chain(rest).collect()
        }
        ShuffleMode::Smart => {
            // a weighted random order (Efraimidis–Spirakis): a favourite
            // weighs 2.5, a song not heard in 30 days (or never) one more
            let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
            let mut keyed: Vec<(f64, usize)> = rest
                .into_iter()
                .map(|i| {
                    let t = &tracks[i];
                    let stale = t.last_played <= 0.0 || now - t.last_played > 30.0 * 86_400.0;
                    let w = 1.0 + if t.fav { 1.5 } else { 0.0 } + if stale { 1.0 } else { 0.0 };
                    (rng.unit().powf(1.0 / w), i)
                })
                .collect();
            keyed.sort_by(|a, b| b.0.total_cmp(&a.0));
            let mut order: Vec<usize> = std::iter::once(first).chain(keyed.into_iter().map(|k| k.1)).collect();
            // the same artist twice in a row: bring the next other one forward,
            // or, with none left ahead, tuck the song in earlier between two
            // others (each fix removes a repeat, so this ends; the windows
            // keep a huge queue quick)
            let artist = |i: usize| tracks[i].artist.trim().to_lowercase();
            let mut k = 1;
            while k < order.len() {
                let a = artist(order[k]);
                if a.is_empty() || a != artist(order[k - 1]) {
                    k += 1;
                    continue;
                }
                if let Some(j) = (k + 1..order.len().min(k + 64)).find(|&j| artist(order[j]) != a) {
                    order[k..=j].rotate_right(1);
                    k += 1;
                } else if let Some(p) = (k.saturating_sub(512).max(1)..k).rev().find(|&p| artist(order[p - 1]) != a && artist(order[p]) != a) {
                    let song = order.remove(k);
                    order.insert(p, song);
                } else {
                    k += 1;
                }
            }
            order
        }
        ShuffleMode::Album => {
            // albums as the list has them; a song without an album is one by itself
            let mut groups: Vec<Vec<usize>> = Vec::new();
            let mut by_name: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
            for (i, t) in tracks.iter().enumerate() {
                let name = t.album.trim().to_lowercase();
                match by_name.get(&name) {
                    Some(&g) if !name.is_empty() => groups[g].push(i),
                    _ => {
                        if !name.is_empty() {
                            by_name.insert(name, groups.len());
                        }
                        groups.push(vec![i]);
                    }
                }
            }
            // the album of the song playing goes first, from that song on
            let home = groups.iter().position(|g| g.contains(&first)).unwrap_or(0);
            let mut current = groups.remove(home);
            let at = current.iter().position(|&i| i == first).unwrap_or(0);
            current.rotate_left(at);
            let mut idx: Vec<usize> = (0..groups.len()).collect();
            shuffle_with(&mut idx, &mut rng);
            current.into_iter().chain(idx.into_iter().flat_map(|g| groups[g].clone())).collect()
        }
        ShuffleMode::Artist => {
            // one block per artist (a song without one is a block by itself)
            let mut groups: Vec<Vec<usize>> = Vec::new();
            let mut by_name: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
            for i in 0..tracks.len() {
                let name = main_artist(&tracks[i]);
                match by_name.get(&name) {
                    Some(&g) if !name.is_empty() => groups[g].push(i),
                    _ => {
                        if !name.is_empty() {
                            by_name.insert(name, groups.len());
                        }
                        groups.push(vec![i]);
                    }
                }
            }
            for g in groups.iter_mut() {
                shuffle_with(g, &mut rng);
            }
            // the artist of the song playing goes first, that song at the top
            let home = groups.iter().position(|g| g.contains(&first)).unwrap_or(0);
            let mut current = groups.remove(home);
            current.retain(|&i| i != first);
            let mut idx: Vec<usize> = (0..groups.len()).collect();
            shuffle_with(&mut idx, &mut rng);
            std::iter::once(first).chain(current).chain(idx.into_iter().flat_map(|g| groups[g].clone())).collect()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_ring_keeps_order_across_the_wrap_and_never_overfills() {
        let ring = Ring::new();
        let mut next = 0u32;
        let mut want = 0u32;
        for _ in 0..5 {
            // three quarters in, three quarters out: the indices wrap past the end
            let chunk: Vec<f32> = (0..RING_CAP * 3 / 4).map(|_| { next += 1; next as f32 }).collect();
            ring.push(&chunk);
            assert_eq!(ring.len(), chunk.len());
            for _ in 0..chunk.len() {
                want += 1;
                assert_eq!(ring.pop(), Some(want as f32));
            }
            assert_eq!(ring.pop(), None);
        }
        ring.push(&vec![1.0; RING_CAP + 10]);
        assert_eq!(ring.len(), RING_CAP); // what doesn't fit is not written over unread samples
    }

    #[test]
    fn a_flush_is_done_by_the_reader_and_drops_only_what_came_before() {
        let ring = Arc::new(Ring::new());
        ring.push(&[1.0; 1000]);
        ring.flush(false); // no output: the writer empties it itself
        assert_eq!(ring.len(), 0);
        ring.push(&[2.0; 1000]);
        let reader = {
            let ring = ring.clone();
            std::thread::spawn(move || {
                for _ in 0..500 {
                    ring.take_flush();
                    std::thread::sleep(Duration::from_millis(1));
                }
            })
        };
        ring.flush(true); // returns once the reader did it
        ring.push(&[3.0; 10]);
        assert_eq!(ring.len(), 10);
        assert_eq!(ring.pop(), Some(3.0));
        reader.join().unwrap();
        assert_eq!(ring.len(), 9); // the reader's later checks don't flush again
    }

    /// A sine wave as a 16-bit WAV file.
    fn wav(path: &Path, rate: u32, channels: u16, secs: f64) {
        let frames = (rate as f64 * secs) as u32;
        let data_len = frames * channels as u32 * 2;
        let mut b = Vec::new();
        b.extend_from_slice(b"RIFF");
        b.extend_from_slice(&(36 + data_len).to_le_bytes());
        b.extend_from_slice(b"WAVEfmt ");
        b.extend_from_slice(&16u32.to_le_bytes());
        b.extend_from_slice(&1u16.to_le_bytes());
        b.extend_from_slice(&channels.to_le_bytes());
        b.extend_from_slice(&rate.to_le_bytes());
        b.extend_from_slice(&(rate * channels as u32 * 2).to_le_bytes());
        b.extend_from_slice(&(channels * 2).to_le_bytes());
        b.extend_from_slice(&16u16.to_le_bytes());
        b.extend_from_slice(b"data");
        b.extend_from_slice(&data_len.to_le_bytes());
        for i in 0..frames {
            let v = ((i as f64 * 440.0 / rate as f64 * std::f64::consts::TAU).sin() * 8000.0) as i16;
            for _ in 0..channels {
                b.extend_from_slice(&v.to_le_bytes());
            }
        }
        std::fs::write(path, b).unwrap();
    }

    fn read_all(src: &mut Source) -> Vec<f32> {
        let mut out = Vec::new();
        while src.read(&mut out, 4096) == 4096 {}
        out
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("bakin-player-test-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d.join(name)
    }

    #[test]
    fn resampled_song_has_exactly_its_length() {
        // 44.1 kHz -> 48 kHz: one second must be 48000 frames, no more, no less,
        // or songs would overlap or leave a gap between them
        let p = tmp("a.wav");
        wav(&p, 44_100, 1, 1.0);
        let mut src = Source::open(p.to_str().unwrap(), 0.0, 48_000).unwrap();
        let out = read_all(&mut src);
        assert_eq!(out.len() / 2, 48_000);
        let peak = out.iter().fold(0f32, |m, v| m.max(v.abs()));
        assert!(peak > 0.2 && peak < 0.3, "the sine survives resampling (peak {peak})");
        // the start is not the resampler's silence
        assert!(out[200..400].iter().any(|v| v.abs() > 0.01));
    }

    #[test]
    fn same_rate_passes_through() {
        let p = tmp("b.wav");
        wav(&p, 48_000, 2, 0.5);
        let mut src = Source::open(p.to_str().unwrap(), 0.0, 48_000).unwrap();
        assert_eq!(read_all(&mut src).len() / 2, 24_000);
        assert!((src.position() - 0.5).abs() < 0.001);
    }

    #[test]
    fn seeking_starts_where_asked() {
        let p = tmp("c.wav");
        wav(&p, 48_000, 2, 2.0);
        let mut src = Source::open(p.to_str().unwrap(), 1.5, 48_000).unwrap();
        let left = read_all(&mut src).len() / 2;
        assert!((left as i64 - 24_000).abs() < 1_200, "about half a second left, got {left} frames");
    }

    #[test]
    fn a_missing_file_is_an_error_not_a_crash() {
        assert!(Source::open("C:/no/such/file.opus", 0.0, 48_000).is_err());
    }

    fn song(artist: &str, album: &str, fav: bool) -> Track {
        Track { artist: artist.into(), album: album.into(), fav, ..Default::default() }
    }

    fn is_permutation(order: &[usize], n: usize) -> bool {
        let mut s = order.to_vec();
        s.sort_unstable();
        s == (0..n).collect::<Vec<_>>()
    }

    #[test]
    fn every_shuffle_keeps_every_song_once_and_starts_with_the_one_asked() {
        let tracks: Vec<Track> = (0..300).map(|i| song(&format!("a{}", i % 7), &format!("al{}", i % 23), i % 5 == 0)).collect();
        for mode in [ShuffleMode::Random, ShuffleMode::Smart, ShuffleMode::Album, ShuffleMode::Artist] {
            let o = shuffled(&tracks, 42, mode);
            assert_eq!(o[0], 42, "{mode:?}");
            assert!(is_permutation(&o, tracks.len()), "{mode:?}");
        }
    }

    #[test]
    fn smart_shuffle_spaces_out_artists() {
        // half the songs by one artist: still never two of theirs in a row
        let tracks: Vec<Track> = (0..200).map(|i| song(&if i % 2 == 0 { "same".to_string() } else { format!("x{i}") }, "", false)).collect();
        let o = shuffled(&tracks, 0, ShuffleMode::Smart);
        let twice = o.windows(2).filter(|w| tracks[w[0]].artist == tracks[w[1]].artist).count();
        assert_eq!(twice, 0);
    }

    #[test]
    fn smart_shuffle_brings_favourites_forward() {
        // everything heard yesterday, one in ten a favourite
        let yesterday = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64() - 86_400.0;
        let tracks: Vec<Track> = (0..1000).map(|i| Track { last_played: yesterday, ..song(&format!("a{i}"), "", i % 10 == 0) }).collect();
        let o = shuffled(&tracks, 1, ShuffleMode::Smart);
        let early = o[..300].iter().filter(|&&i| tracks[i].fav).count();
        assert!(early > 40, "{early} favourites in the first 300 (30 by chance, ~57 expected)");
    }

    #[test]
    fn eq_flat_is_identity() {
        let mut eq = Eq::new(48_000);
        eq.set(true, [0.0; 10], 48_000);
        let mut buf: Vec<f32> = (0..2000).map(|i| ((i as f32) * 0.01).sin() * 0.5).collect();
        let before = buf.clone();
        eq.process(&mut buf);
        assert_eq!(buf, before);
    }

    #[test]
    fn eq_boost_does_not_clip() {
        // a full-scale 1 kHz tone with +12 dB at 1 kHz: the pre-gain keeps it under 1.0
        let mut eq = Eq::new(48_000);
        let mut g = [0.0; 10];
        g[5] = 12.0;
        eq.set(true, g, 48_000);
        let mut buf: Vec<f32> = (0..96_000).flat_map(|i| {
            let x = (2.0 * std::f32::consts::PI * 1000.0 * i as f32 / 48_000.0).sin() * 0.99;
            [x, x]
        }).collect();
        eq.process(&mut buf);
        let peak = buf[20_000..].iter().fold(0.0f32, |m, x| m.max(x.abs()));
        assert!(peak <= 1.0, "peak {peak}");
        assert!(peak > 0.8, "the band still sounds: {peak}");
    }

    #[test]
    fn even_volume_gain_follows_the_setting() {
        let mut e = Engine::new(Box::new(|_, _| {}));
        e.tracks = vec![Track { gain_db: Some(-6.0), ..song("a", "", false) }, song("b", "", false)];
        e.order = vec![0, 1];
        assert_eq!(e.track_gain(0), 1.0, "off: as it is");
        e.loudness = true;
        assert!((e.track_gain(0) - 0.501).abs() < 0.01);
        assert_eq!(e.track_gain(1), 1.0, "not measured: as it is");
    }

    #[test]
    fn even_volume_with_nothing_queued_does_not_panic() {
        let mut e = Engine::new(Box::new(|_, _| {}));
        e.loudness = true;
        e.regain();
        assert_eq!(e.track_gain(3), 1.0);
    }

    #[test]
    fn cola_moves_only_what_comes_later() {
        let mut e = Engine::new(Box::new(|_, _| {}));
        e.order = vec![10, 11, 12, 13, 14];
        e.pos = 1;
        e.move_to(4, 2); // the last one, right after the one playing
        assert_eq!(e.order, vec![10, 11, 14, 12, 13]);
        e.move_to(1, 3); // the one playing doesn't move
        e.move_to(3, 0); // nor to before it
        e.move_to(9, 2); // nor from nowhere
        assert_eq!(e.order, vec![10, 11, 14, 12, 13]);
        e.queued = 1; // «play next»: 14
        e.move_to(2, 4); // out of «next»: the count goes down
        assert_eq!((e.order.clone(), e.queued), (vec![10, 11, 12, 13, 14], 0));
    }

    #[test]
    fn ab_repeat_loops() {
        assert_eq!(ab_jump(10.0, Some((5.0, 20.0))), None);
        assert_eq!(ab_jump(20.1, Some((5.0, 20.0))), Some(5.0));
        assert_eq!(ab_jump(30.0, Some((20.0, 5.0))), None, "B before A: no loop");
        assert_eq!(ab_jump(30.0, None), None);
    }

    #[test]
    fn artist_shuffle_plays_an_artist_at_a_time() {
        // 8 artists × 5 songs, interleaved in the list; one more with the artist only in its name
        let mut tracks: Vec<Track> = (0..40).map(|i| song(&format!("Artist {} feat. Guest", i % 8), "", false)).collect();
        tracks.push(Track { name: "Artist 3 - Extra [abcdefghijk].opus".into(), ..song("", "", false) });
        let o = shuffled(&tracks, 11, ShuffleMode::Artist);
        assert!(is_permutation(&o, tracks.len()));
        let who: Vec<String> = o.iter().map(|&i| main_artist(&tracks[i])).collect();
        assert_eq!(who[0], "artist 3");
        assert!(who[..6].iter().all(|a| a == "artist 3"), "the playing artist's six songs first: {who:?}");
        let changes = who.windows(2).filter(|w| w[0] != w[1]).count();
        assert_eq!(changes, 7, "each artist once, in one block: {who:?}");
    }

    #[test]
    fn album_shuffle_keeps_albums_whole_and_in_order() {
        let tracks: Vec<Track> = (0..60).map(|i| song("a", &format!("album {}", i / 6), false)).collect();
        let o = shuffled(&tracks, 14, ShuffleMode::Album);
        assert_eq!(&o[..4], &[14, 15, 16, 17], "the playing album from that song on");
        // every other album: its six songs together, in their order
        for chunk in o[6..].chunks(6) {
            assert!(chunk.windows(2).all(|w| w[1] == w[0] + 1), "{chunk:?}");
        }
    }

    #[test]
    fn shuffle_keeps_every_song_once() {
        let mut v: Vec<usize> = (0..500).collect();
        shuffle_with(&mut v, &mut Rng::new());
        assert_ne!(v, (0..500).collect::<Vec<_>>());
        v.sort_unstable();
        assert_eq!(v, (0..500).collect::<Vec<_>>());
    }
}
