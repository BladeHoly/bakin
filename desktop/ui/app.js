// Bakin desktop UI: glass-midas page in a Tauri window, driving the Python
// engine (bakin.sidecar) through the `engine_call` command and `engine` events.
// Session values typed here go to the engine once and the inputs are cleared;
// nothing secret is ever rendered back into the page.
import { tr, tn, LOCALE, lang as LANG_NOW, syncLang } from './i18n.js';  // first: translates the page before the components draw
import { tokensToCss } from '@glass-midas/core';
import { toast } from './vendor/glass-midas/components/index.js';  // over modal dialogs too (0.8.1)

document.head.insertAdjacentHTML('beforeend', `<style>${tokensToCss()}</style>`);

// gm-select moves a <select>'s id to the button it draws; $ still returns the select.
const $ = (id) => {
  const el = document.getElementById(id);
  return el?.classList.contains('gm-select-trigger') ? el.parentElement.querySelector(':scope > select') : el;
};
const tauri = window.__TAURI__;
const appWindow = tauri.window.getCurrentWindow();
const call = (method, params = {}) => tauri.core.invoke('engine_call', { method, params });

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

let running = false;
let queue = [];          // jobs waiting behind the running one (from the engine)
let jobKind = '';
let total = null;

// ------------------------------------------------------------ window chrome
async function syncMax() {
  const max = await appWindow.isMaximized();
  document.body.classList.toggle('maximized', max);
  const label = max ? tr('Restaurar') : tr('Maximizar');
  $('win-max').setAttribute('aria-label', label);
  $('win-max').title = label;
}

function wireWindow() {
  $('win-min').addEventListener('click', () => appWindow.minimize());
  $('win-max').addEventListener('click', () => appWindow.toggleMaximize());
  // With background on, the shell hides the window (keep-alive); otherwise ask before stopping work.
  $('win-close').addEventListener('click', () => ((running || queue.length) && !bgEnabled() ? $('quit-dialog').show() : appWindow.close()));
  $('quit-dialog').addEventListener('gm-close', async (e) => {
    if (e.detail.value !== 'quit') return;
    await call('cancel').catch(() => {});
    appWindow.close();
  });
  for (const edge of document.querySelectorAll('.edges [data-dir]')) {
    edge.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      appWindow.startResizeDragging(edge.dataset.dir);
    });
  }
  appWindow.onResized(syncMax);
  syncMax();
}

// ------------------------------------------------------------------ options
function chipGroup(el, onChange) {
  const chips = [...el.querySelectorAll('.gm-chip')];
  const set = (value) => chips.forEach((c) => c.setAttribute('aria-pressed', String(c.value === value)));
  chips.forEach((c) => c.addEventListener('click', () => { set(c.value); onChange(c.value); }));
  return { set, get value() { return chips.find((c) => c.getAttribute('aria-pressed') === 'true')?.value; } };
}

function wireOptions() {
  $('btn-folder').addEventListener('click', async () => {
    const dir = await tauri.dialog.open({ directory: true, defaultPath: $('out-dir').value || undefined,
                                          title: tr('Carpeta de descargas') });
    if (typeof dir === 'string') changeOutDir(dir);
  });
  $('out-dir').addEventListener('change', () => changeOutDir($('out-dir').value));
  // Configuración › General › Idioma: saved by the engine, then the page is drawn again in it
  // (the gm-select keeps the value: glass-midas moves the select's id to its button)
  $('ui-lang-box').addEventListener('gm-change', async () => {
    const r = await call('settings_set', { changes: { lang: $('ui-lang-box').value } }).catch((e) => ({ ok: false, error: String(e) }));
    if (!r.ok) { toast(tr(r.error)); return; }
    syncLang(r.settings.lang);
  });
}

function options() {
  return { out_dir: $('out-dir').value.trim() };
}

function applySettings(s) {
  $('ui-lang-box').value = s.lang || 'es';
  $('out-dir').value = s.out_dir;
  L.sound = { mode: s.loudness_mode || 'off', preamp: s.loudness_preamp || 0 };
  $('ly-online').checked = !!s.lyrics_online;
  $('ly-beside').checked = s.lyrics_beside !== false;
  $('perf-hourly').value = s.catalog_hourly ?? 100;
  $('perf-covers').checked = s.catalog_covers !== false;
  L.catalogCovers = s.catalog_covers !== false;
  $('mb-on').checked = !!s.musicbrainz;
  $('lan-on').checked = !!s.lan_enabled;
  L.musicbrainz = !!s.musicbrainz;
}

// ------------------------------------------------------------------- jobs
function setMeter(done, max) {
  const m = $('meter');
  ringDone = max ? Math.max(0, Math.min(1, done / max)) : null;
  syncProgressRing();
  if (!max) { m.setAttribute('aria-busy', 'true'); m.removeAttribute('aria-valuenow'); return; }
  m.removeAttribute('aria-busy');
  const v = Math.max(0, Math.min(1, done / max));
  m.style.setProperty('--value', v.toFixed(4));
  m.setAttribute('aria-valuenow', Math.round(v * 100));
}

let queueMore = null;

// ------------------------------------------------------------ background
// With work pending, closing the window keeps Bakin in the tray (the shell
// hides the window instead of quitting while keep-alive is on).
const bgEnabled = () => store.get('bakin.background', 'on') === 'on';
function syncKeepAlive() {
  const work = running || (queueState.total || 0) > 0 || !!queueState.paused_until || plPlaying();
  tauri.core.invoke('set_keep_alive', { value: bgEnabled() && work }).catch(() => {});
}

// ------------------------------------------------------------ background
// Not flat black: soft lights in the interface's colours and a fine grain
// (no dot lattice: the user found it distracting), drawn once on a canvas behind
// everything. It is drawn again only when the window size changes or, with
// «Con el color de la portada», when the song (its cover) changes; never per
// frame. The new one fades in over the old.
const BACKDROP_HUES = [[124, 92, 255], [40, 190, 170], [210, 80, 190], [90, 120, 255]];
const BD = { tint: null, size: '' };

function backdropHues() {
  if (!BD.tint) return BACKDROP_HUES;
  const [h, s] = BD.tint;
  const rgb = (hh, ss, ll) => {
    const k = (n) => (n + hh / 30) % 12;
    const a = ss * Math.min(ll, 1 - ll);
    return [0, 8, 4].map((n) => Math.round(255 * (ll - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1)))));
  };
  const sat = Math.min(0.85, Math.max(0.45, s));
  return [rgb(h, sat, 0.55), rgb((h + 35) % 360, sat, 0.5), rgb((h + 325) % 360, sat, 0.45), rgb(h, sat * 0.8, 0.62)];
}

function drawBackdrop() {
  const w = Math.max(1, innerWidth);
  const h = Math.max(1, innerHeight);
  BD.size = `${w}x${h}`;
  const c = document.createElement('canvas');
  c.className = 'backdrop';
  c.setAttribute('aria-hidden', 'true');
  c.width = w;
  c.height = h;
  const g = c.getContext('2d');
  g.fillStyle = '#0d0c14';
  g.fillRect(0, 0, w, h);
  // the lights
  const hues = backdropHues();
  const big = Math.max(w, h);
  [[0.1, 0.08, 0.7], [0.92, 0.22, 0.6], [0.58, 1.0, 0.75], [0.3, 0.62, 0.42]].forEach(([x, y, r], i) => {
    const [R, G, B] = hues[i % hues.length];
    const gr = g.createRadialGradient(x * w, y * h, 0, x * w, y * h, big * r);
    gr.addColorStop(0, `rgb(${R} ${G} ${B} / 0.17)`);
    gr.addColorStop(1, `rgb(${R} ${G} ${B} / 0)`);
    g.fillStyle = gr;
    g.fillRect(0, 0, w, h);
  });
  // grain, so the gradients don't band (same seed every time: no shimmer)
  const img = g.getImageData(0, 0, w, h);
  const d = img.data;
  let x = 0x2f6b1d3 | 0;
  for (let i = 0; i < d.length; i += 4) {
    x ^= x << 13; x ^= x >>> 17; x ^= x << 5;
    const n = ((x >>> 24) - 128) / 32;
    d[i] += n; d[i + 1] += n; d[i + 2] += n;
  }
  g.putImageData(img, 0, 0);
  // in, over the old one
  const old = [...document.querySelectorAll('canvas.backdrop')];
  document.body.prepend(c);
  requestAnimationFrame(() => c.classList.add('in'));
  setTimeout(() => old.forEach((o) => o.remove()), 900);
}

// The cover's colour: hue and saturation of its colourful pixels.
async function coverTint(src) {
  if (!src) return null;
  const img = new Image();
  img.src = src;
  try { await img.decode(); } catch { return null; }
  const c = document.createElement('canvas');
  c.width = c.height = 24;
  const g = c.getContext('2d');
  g.drawImage(img, 0, 0, 24, 24);
  const d = g.getImageData(0, 0, 24, 24).data;
  let sx = 0; let sy = 0; let ss = 0; let n = 0;
  for (let i = 0; i < d.length; i += 4) {
    const r = d[i] / 255; const gg = d[i + 1] / 255; const b = d[i + 2] / 255;
    const max = Math.max(r, gg, b); const min = Math.min(r, gg, b);
    const sat = max === 0 ? 0 : (max - min) / max;
    if (sat < 0.2 || max < 0.15) continue;  // greys and near-black say nothing
    let hue = max === r ? (gg - b) / (max - min) : max === gg ? 2 + (b - r) / (max - min) : 4 + (r - gg) / (max - min);
    hue = (hue * 60 + 360) % 360;
    sx += Math.cos(hue * Math.PI / 180) * sat; sy += Math.sin(hue * Math.PI / 180) * sat; ss += sat; n++;
  }
  if (n < 12) return null;  // a grey cover: keep the usual colours
  return [(Math.atan2(sy, sx) * 180 / Math.PI + 360) % 360, ss / n];
}

const tintOn = () => store.get('bakin.bg.tint', 'off') === 'on';

async function retintBackdrop(cover) {
  const tint = tintOn() ? await coverTint(cover) : null;
  const same = (a, b) => (!a && !b) || (a && b && Math.abs(a[0] - b[0]) < 8 && Math.abs(a[1] - b[1]) < 0.08);
  if (same(tint, BD.tint)) return;
  BD.tint = tint;
  drawBackdrop();
}

function wireBackdrop() {
  drawBackdrop();
  let t;
  addEventListener('resize', () => {
    clearTimeout(t);
    t = setTimeout(() => { if (BD.size !== `${innerWidth}x${innerHeight}`) drawBackdrop(); }, 350);
  });
  $('bg-tint').checked = tintOn();
  // the small window's place and size are kept by the shell (mini.rs), which reads them before building it
  tauri.core.invoke('mini_prefs_get').then((p) => { $('mini-remember').checked = p.remember !== false; }).catch(() => {});
  $('mini-remember').addEventListener('change', (e) => tauri.core.invoke('mini_prefs_set', { remember: e.target.checked }).catch(() => {}));
  // backups (0.18.1)
  $('backup-export').addEventListener('click', async () => {
    const dir = await tauri.dialog.open({ directory: true, title: tr('Dónde guardar la copia de Bakin') });
    if (typeof dir !== 'string') return;
    const r = await call('backup_export', { dest: dir }).catch((e) => ({ ok: false, error: String(e) }));
    toast(r.ok ? tr('Copia guardada: {path}', { path: r.path }) : tr(r.error), { duration: 8000 });
  });
  $('backup-import').addEventListener('click', async () => {
    const f = await tauri.dialog.open({ title: tr('Elegir una copia de Bakin'), filters: [{ name: tr('Copia de Bakin'), extensions: ['zip'] }] });
    if (typeof f !== 'string') return;
    const p = await call('backup_inspect', { path: f }).catch((e) => ({ ok: false, error: String(e) }));
    if (!p.ok) { toast(tr(p.error), { duration: 7000 }); return; }
    const when = p.manifest?.created ? new Date(p.manifest.created).toLocaleDateString(LOCALE) : '';
    const what = (p.lines || []).join(', ') || tr('nada para sumar');
    if (!(await ask(tr('¿Importar esta copia?'), tr('Copia del {when} (Bakin {x}): {what}. Se combina con lo que ya tienes; no se borra nada. Antes se guarda una copia de lo de hoy.', { when, x: p.manifest?.bakin_version || '', what }), tr('Importar')))) return;
    const r = await call('backup_import', { path: f }).catch((e) => ({ ok: false, error: String(e) }));
    if (!r.ok) { toast(tr(r.error), { duration: 7000 }); return; }
    const skipped = r.collections_skipped?.length ? tr(' Carpetas que no están en esta PC: {collections_skipped}.', { collections_skipped: r.collections_skipped.join(', ') }) : '';
    toast((r.already_imported ? tr('Copia importada (ya se había importado: las reproducciones no se suman de nuevo).') : tr('Copia importada.')) + skipped, { duration: 9000 });
    refreshLibrary(true);
    loadMedia();
  });
  // the data folder (0.18.4)
  call('data_dir_info').then((d) => {
    $('data-dir').textContent = d.path;
    $('data-dir-note').textContent = d.missing ? tr('No se encuentra {missing} (¿un disco desconectado?): se usa la carpeta de siempre.', { missing: d.missing }) : '';
    if (d.missing) toast(tr('No se encuentra la carpeta de datos ({missing}): Bakin usa la de siempre hasta que vuelva.', { missing: d.missing }), { duration: 9000 });
  }).catch(() => {});
  $('data-dir-move').addEventListener('click', async () => {
    const dir = await tauri.dialog.open({ directory: true, title: tr('Dónde guardar los datos de Bakin') });
    if (typeof dir !== 'string') return;
    if (!(await ask(tr('¿Mover los datos de Bakin?'), tr('Se copian a {dir}, se revisa la copia y Bakin se reinicia usándola. La carpeta vieja queda hasta que la borres.', { dir }), tr('Mover y reiniciar')))) return;
    const r = await call('data_dir_move', { dest: dir }).catch((e) => ({ ok: false, error: String(e) }));
    if (!r.ok) { toast(tr(r.error), { duration: 7000 }); return; }
    toast(tr('Datos copiados: Bakin se reinicia…'));
    setTimeout(() => tauri.core.invoke('app_restart'), 1200);
  });
  $('mini-dim').checked = store.get('bakin.mini.dim', 'off') === 'on';
  $('mini-dim').addEventListener('change', (e) => store.set('bakin.mini.dim', e.target.checked ? 'on' : 'off'));
  $('bg-tint').addEventListener('change', (e) => {
    store.set('bakin.bg.tint', e.target.checked ? 'on' : 'off');
    retintBackdrop($('pl-cover').querySelector('img')?.src || '');
  });
}

async function wireBackground() {
  $('bg-keep').checked = bgEnabled();
  $('bg-notify').checked = notifyOn();
  // the shell sends them itself while the window is closed in the tray
  tauri.core.invoke('set_notify', { value: notifyOn() }).catch(() => {});
  $('bg-notify').addEventListener('change', (e) => {
    store.set('bakin.notify', e.target.checked ? 'on' : 'off');
    tauri.core.invoke('set_notify', { value: e.target.checked }).catch(() => {});
  });
  $('bg-keep').addEventListener('change', (e) => { store.set('bakin.background', e.target.checked ? 'on' : 'off'); syncKeepAlive(); });
  try { $('bg-autostart').checked = await tauri.core.invoke('autostart_get'); } catch { $('bg-autostart').disabled = true; }
  $('bg-autostart').addEventListener('change', async (e) => {
    try {
      e.target.checked = await tauri.core.invoke('autostart_set', { enabled: e.target.checked });
      toast(e.target.checked ? tr('Bakin va a arrancar con Windows, oculto en la bandeja.') : tr('Bakin ya no arranca con Windows.'));
    } catch (err) { toast(tr(String(err))); e.target.checked = !e.target.checked; }
  });
  $('btn-queue-pause').addEventListener('click', () => call('pause_queue').catch((e) => toast(tr(String(e)))));
  $('media-limit').addEventListener('change', async (e) => {
    const n = Math.max(0, Number(e.target.value) || 0);
    await call('media_set_option', { key: 'daily_limit', value: n });
    toast(n ? tr('Límite diario: {n} descargas de música/video.', { n }) : tr('Sin límite diario.'));
  });
}

function setBusy(on) {
  running = on;
  $('btn-cancel').hidden = !on;
  $('btn-cancel').disabled = false;
  $('busy-dot').hidden = !on;
  syncKeepAlive();
  syncProgressRing();
}

// The ring next to «Descargar» (0.15.3): while something downloads, how far it is and how many
// are left; a click shows Descargas. Hidden when nothing's going on.
let ringDone = null;
function syncProgressRing() {
  const left = (queueState.total || 0) + (running ? 1 : 0);
  const b = $('tb-progress');
  b.hidden = !running && !left;
  if (b.hidden) return;
  b.classList.toggle('indeterminate', ringDone === null);
  if (ringDone !== null) b.style.setProperty('--done', String(Math.round(ringDone * 100)));
  $('tb-progress-n').textContent = left > 1 ? String(left) : '';
  b.title = queueState.paused ? tr('Descargas en pausa: {x}', { x: tr(queueState.reason || '') }) : tn('{n} descarga · clic para ver', '{n} descargas · clic para ver', left);
}

// The engine runs jobs one at a time; everything else waits here.
let queueState = { total: 0, paused: false };

const notifyOn = () => store.get('bakin.notify', 'on') === 'on';
async function notifyIfAway(title, body) {
  if (!notifyOn()) return;
  try {
    if ((await appWindow.isVisible()) && (await appWindow.isFocused())) return;   // already looking at Bakin
    await tauri.core.invoke('notify', { title, body });
  } catch { /* notifications unavailable */ }
}

let queueLane = null;  // the lane whose jobs the list shows
const LANE_ICON = {
  media: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
  wallhaven: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="3"/><path d="m3 15 5-5 5 5 3-3 5 5"/></svg>',
  convert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h13l-3-3M20 17H7l3 3"/></svg>',
};

function laneRow(l, st) {
  const row = document.createElement('div');
  row.className = 'lane';
  row.dataset.lane = l.id;
  row.setAttribute('role', 'tab');
  row.tabIndex = 0;
  row.setAttribute('aria-selected', String(l.id === queueLane));
  const icon = document.createElement('span');
  icon.className = 'lane-icon';
  icon.innerHTML = LANE_ICON[l.id] || '';
  const text = document.createElement('span');
  text.className = 'lane-text';
  const name = document.createElement('strong');
  name.textContent = `${l.name} · ${l.count}`;
  const state = document.createElement('small');
  const when = l.held?.until ? new Date(l.held.until * 1000) : null;
  const fmt = (d) => {
    const today = new Date();
    const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
    const day = d.toDateString() === today.toDateString() ? tr('hoy') : d.toDateString() === tomorrow.toDateString() ? tr('mañana') : d.toLocaleDateString(LOCALE, { weekday: 'long' });
    return `${day} ${d.toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' })}`;
  };
  // «Sigue sola mañana» / «Se reintenta solo en…»: the time already says it
  const why = (l.held?.reason || '').replace(/\s*(Sigue sola|Se reintenta solo)[^.]*\.?\s*$/, '');
  state.textContent = l.running ? tr('Bajando ahora') : l.held
    ? `${when ? tr('Espera hasta {when}', { when: fmt(when) }) : tr('En pausa')}${why && why !== 'Pausada por ti.' ? ` · ${tr(why)}` : ''}`
    : tr('En espera');
  if (l.id === 'media' && st.daily_limit) state.textContent += tr(' · hoy {today} de {limit}', { today: st.downloads_today, limit: st.daily_limit });
  if (l.held) row.dataset.tone = 'warning';
  text.append(name, state);
  const act = document.createElement('button');
  act.type = 'button';
  act.className = 'gm-chip gm-chip-plain';
  act.textContent = l.held ? (when ? tr('Seguir ahora') : tr('Reanudar')) : tr('Pausar');
  act.addEventListener('click', (e) => {
    e.stopPropagation();
    call(l.held ? 'queue_resume' : 'queue_pause', { lane: l.id }).catch((err) => toast(tr(String(err))));
  });
  row.append(icon, text, act);
  const pick = () => { queueLane = l.id; renderQueue(queueState); };
  row.addEventListener('click', pick);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
  return row;
}

function renderQueue(st) {
  setTimeout(syncProgressRing);  // after queueState below
  const prev = queueState;
  if ((prev.total || 0) > 0 && !(st.total || 0) && !st.paused) {
    notifyIfAway(tr('Bakin terminó'), tr('La cola quedó vacía: todo lo pendiente ya se procesó.'));
  } else if (st.paused && st.reason && st.reason !== prev.reason && prev.total !== undefined) {
    notifyIfAway(tr('Bakin pausó la cola'), tr(st.reason));
  }
  const allWaiting = st.items.filter((i) => i.status !== 'running');
  queue = allWaiting;
  queueState = st;
  const left = Math.max(0, (st.total ?? allWaiting.length) - (st.items.length - allWaiting.length));
  // Colas (0.13 review): a row per lane with its count and state; each pauses on its own
  const lanes = (st.lanes || []).filter((l) => l.count || l.held);
  $('queue-card').hidden = !lanes.length;
  $('queue-count').textContent = left ? tn('{n} trabajo en total', '{n} trabajos en total', left) : '';
  const anyHeld = lanes.some((l) => l.held);
  $('btn-queue-resume').hidden = !anyHeld;
  $('btn-queue-pause').hidden = !lanes.some((l) => !l.held && l.count);
  if (!lanes.some((l) => l.id === queueLane)) queueLane = (lanes.find((l) => l.running) || lanes.find((l) => !l.held) || lanes[0])?.id;
  $('lanes').replaceChildren(...lanes.map((l) => laneRow(l, st)));
  syncKeepAlive();
  $('btn-cancel').textContent = allWaiting.length ? tr('Cancelar todo') : tr('Cancelar');
  const waiting = allWaiting.filter((i) => (i.lane || 'media') === queueLane);
  const laneLeft = (lanes.find((l) => l.id === queueLane)?.count || 0) - (st.items.some((i) => i.status === 'running' && i.lane === queueLane) ? 1 : 0);
  const ol = $('queue');
  ol.replaceChildren();
  if (laneLeft > waiting.length) {
    const more = document.createElement('li');
    more.className = 'queue-more hint';
    more.textContent = tr('… y {x} más', { x: laneLeft - waiting.length });
    queueMore = more;
  } else queueMore = null;
  waiting.forEach((item, n) => {
    const li = document.createElement('li');
    li.className = 'queue-item';
    const pos = document.createElement('span');
    pos.className = 'queue-pos';
    pos.textContent = String(n + 1);
    const name = document.createElement('strong');
    name.textContent = item.title;
    const what = document.createElement('span');
    what.className = 'gm-badge';
    what.dataset.tone = item.status === 'paused' ? 'warning' : 'info';
    what.textContent = `${item.mode === 'analyze' ? tr('Analizar') : tr('Descargar')}${item.status === 'paused' ? tr(' · en pausa') : ''}`;
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'gm-chip gm-chip-plain gm-chip-round';
    rm.title = tr('Quitar de la cola');
    rm.setAttribute('aria-label', tr('Quitar {title} de la cola', { title: item.title }));
    rm.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    rm.addEventListener('click', () => call('remove_queued', { item_id: item.id }).catch((e) => toast(tr(String(e)))));
    li.append(pos, name, what, rm);
    ol.append(li);
  });
  if (queueMore) ol.append(queueMore);
}

function log(tone, text) {
  const ol = $('log');
  const atBottom = ol.scrollHeight - ol.scrollTop - ol.clientHeight < 8;
  const li = document.createElement('li');
  if (tone) li.dataset.tone = tone;
  li.textContent = text;
  ol.append(li);
  while (ol.childElementCount > 400) ol.firstElementChild.remove();
  if (atBottom) ol.scrollTop = ol.scrollHeight;
}

const KIND_TONE = { photo: 'success', error: 'danger', warn: 'warning', unsupported: 'warning', info: 'info', profile: 'info' };

function onEngineEvent(ev) {
  switch (ev.kind) {
    case 'queue':
      renderQueue(ev);
      break;
    case 'media_url': case 'media_item': case 'media_progress': case 'media_post': case 'media_file': case 'media_raw':
      onMediaEvent(ev);
      break;
    case 'mb_result': case 'mb_done':
      onMusicBrainz(ev);
      break;
    case 'sp_track': case 'sp_done':
      onSpotify(ev);
      break;
    case 'mirror_done':
      onMirrorDone(ev);
      break;
    case 'loud_progress':
      $('ld-status').textContent = tr('Midiendo el volumen: {done} de {total}…', { done: ev.done, total: ev.total });
      break;
    case 'loud_done':
      $('ld-status').textContent = ev.measured || ev.failed ? tn('Listo: {n} tema medido', 'Listo: {n} temas medidos', ev.measured) + (ev.failed ? tr(', {failed} no se pudo', { failed: ev.failed }) : '') + '.' : tr('Todos los temas están medidos.');
      pushGains();
      break;
    case 'wallhaven_progress':  // one wallpaper of the job
      setMeter(ev.index - 1, ev.count);
      $('status').textContent = tr('Fondo {index} de {count}…', { index: ev.index, count: ev.count });
      break;
    case 'media_leftovers':
      onLeftovers(ev);
      break;
    case 'cookies_bad':
      toast(tr('Las cookies de «{preset}» dejaron de servir: los temas que piden tu cuenta (+18) van a fallar hasta que exportes un cookies.txt nuevo.', { preset: ev.preset }), { duration: 12000 });
      notifyIfAway(tr('Bakin: cookies vencidas'), tr('Exporta un cookies.txt nuevo para los temas que piden tu cuenta.'));
      renderCookiesHealth();
      break;
    case 'tools_progress': case 'tools_done':
      onToolsEvent(ev);
      break;
    case 'convert_file':
      onConvertEvent(ev);
      break;
    case 'move':
    case 'move_done':
      onMoveEvent(ev);
      break;
    case 'start':
      setBusy(true);
      jobKind = ev.target_kind;
      total = null;
      $('job-title').textContent = ev.title;
      $('job-badge').hidden = true;
      $('stop-note').hidden = true;
      $('status').textContent = { media: tr('Preparando yt-dlp…'), convert: tr('Convirtiendo…'), wallhaven: tr('Buscando en Wallhaven…') }[ev.mode] || tr('Empezando…');
      $('btn-open').disabled = true;
      setMeter(0, null);
      log('info', tr(ev.message));
      break;
    case 'phase':
      $('status').textContent = tr(ev.message);
      log('muted', tr(ev.message));
      break;
    case 'wait':
      $('status').textContent = tr('Esperando {seconds} s ({message})…', { seconds: Math.round(ev.seconds), message: tr(ev.message) });
      break;
    case 'finished': {
      setBusy(false);
      if (ev.mode === 'convert') onConvertFinished();
      libraryChanged();
      if (ev.dir) $('btn-open').disabled = false;
      if (ev.outcome === 'ok') {
        setMeter(1, 1);
        $('status').textContent = tr(ev.message);
        if (ev.lane) doneToast(ev);
        else toast(tr(ev.message), ev.dir ? { action: { label: tr('Abrir carpeta'), onClick: () => call('open_dir') }, duration: 7000 } : {});
        log('success', tr(ev.message));
      } else if (ev.outcome === 'cancelled') {
        $('status').textContent = tr(ev.message);
        toast(tr(ev.message));
        log('muted', tr(ev.message));
      } else {
        $('status').textContent = ev.outcome === 'stopped' ? tr('El sitio pidió parar. Lo bajado se conserva.') : 'Error.';
        const note = $('stop-note');
        note.hidden = false;
        note.textContent = tr(ev.message);
        // the reason, not just «Ocurrió un error» (0.9.2: a song of the queue failed and nobody knew which)
        const why = tr(String(ev.message || '')).split('\n')[0];
        toast(ev.outcome === 'stopped' ? tr('El sitio pidió parar') : why ? `Error: ${why.length > 220 ? `${why.slice(0, 220)}…` : why}` : tr('Ocurrió un error'),
          { duration: 9000, action: { label: tr('Ver'), onClick: () => setView('downloads') } });
        log('danger', tr(ev.message));
      }
      break;
    }
    default:
      log(KIND_TONE[ev.kind] ?? 'muted', tr(ev.message));
  }
}

// One «done» toast per lane, adding up (0.13 review: a dozen small Wallhaven jobs
// stacked a dozen «1 fondo nuevo»): while jobs keep finishing (under a minute
// apart) the count grows in the same toast, or in a new one if it already left.
const DONE = {};
function doneToast(ev) {
  const now = Date.now();
  const prev = DONE[ev.lane];
  const fresh = !prev || now - prev.at > 60000;
  const d = DONE[ev.lane] = { at: now, count: (fresh ? 0 : prev.count) + (ev.count || 0), skipped: (fresh ? 0 : prev.skipped) + (ev.skipped || 0) };
  let text = !d.count && d.skipped ? tr('Listo: nada nuevo')
    : ev.lane === 'wallhaven' ? tn('Listo: {n} fondo nuevo', 'Listo: {n} fondos nuevos', d.count)
      : tn('Listo: {n} descarga lista', 'Listo: {n} descargas listas', d.count);
  if (d.skipped) text += tn(' · {n} ya estaba', ' · {n} ya estaban', d.skipped);
  if (!d.count && !d.skipped) text = tr(ev.message);
  // the same toast while it's on screen (glass-midas' `key`)
  toast(text, { key: `done-${ev.lane}`, duration: 7000, ...(ev.dir ? { action: { label: tr('Abrir carpeta'), onClick: () => call('open_dir') } } : {}) });
}

// Returns true when the job was accepted (started or queued). `quiet` leaves the
// toasts to the caller (several jobs at once).
async function startJob(mode, url, overrides = {}, { quiet = false } = {}) {
  try {
    const res = await call('start', { mode, url, options: { ...options(), ...overrides } });
    if (res.ok) {
      // downloads go on in the background (0.15.3): no jump to Descargas, the ring on top shows them
      if (!quiet) toast(res.waiting ? (res.waiting === 1 ? tr('En cola: empieza cuando termine la actual.') : tr('En cola: {n} por delante.', { n: res.waiting })) : tr('Bajando…'),
        { key: 'job-start', action: { label: tr('Ver'), onClick: () => setView('downloads') } });
      return true;
    }
    if (!quiet) toast(tr(res.error), { duration: 6000 });
    log('warning', tr(res.error));
    if (res.field === 'tools') openTools();
    return false;
  } catch (err) {
    if (!quiet) toast(tr(String(err)), { duration: 6000 });
    return false;
  }
}

async function startMany(mode, jobs) {
  let ok = 0;
  for (const [url, overrides] of jobs) {
    const r = await startJob(mode, url, overrides, { quiet: true });
    if (r === null) return;
    ok += r ? 1 : 0;
  }
  const skipped = jobs.length - ok;
  toast(skipped ? tr('{ok} en cola · {skipped} omitidos (ver Actividad).', { ok, skipped }) : tr('{ok} en cola.', { ok }), { duration: 5000 });
}

function wireJobs() {
  $('btn-queue-resume').addEventListener('click', () => call('resume_queue').catch((e) => toast(tr(String(e)))));
  $('btn-cancel').addEventListener('click', async () => {
    $('btn-cancel').disabled = true;
    $('status').textContent = queue.length ? tr('Cancelando y vaciando la cola… (lo descargado se conserva)')
      : tr('Cancelando… (lo descargado se conserva)');
    await call('cancel').catch(() => {});
  });
  $('btn-open').addEventListener('click', () => call('open_dir'));
}

// ---------------------------------------------------------- views
// Fotos and Música are two spaces of the same library (what Bakin opens on,
// the last one used); Descargas holds the downloader.
function setView(v) {
  if (v === 'library') v = 'photos';
  if ($('view').value !== v) $('view').value = v;
  syncTabs(v);
  const lib = v === 'photos' || v === 'music';
  for (const name of ['library', 'downloads', 'convert']) {
    const on = name === 'library' ? lib : name === v;
    $(`side-${name}`).hidden = !on;
    $(`view-${name}`).hidden = !on;
  }
  if (lib) {
    store.set('bakin.view', v);
    switchSpace(v);
  }
  if (v === 'downloads') renderDlSummary();
  if (v === 'convert') loadConvertFormats();
}

const SPACE_START = {
  photos: () => ({ scope: 'all', collection: null, kind: '', tags: new Set(), q: '', sort: 'new', mview: null, lowq: false }),
  music: () => ({ scope: 'all', collection: null, kind: 'audio', tags: new Set(), q: '', sort: 'new', lowq: false,
    mview: { view: 'songs', name: tr('Todo') } }),
};

function switchSpace(space) {
  const changed = L.space !== space;
  if (changed) {
    L.saved[L.space] = { scope: L.scope, collection: L.collection, kind: L.kind, tags: new Set(L.tags), q: L.q,
      sort: L.sort, mview: L.mview, lowq: L.lowq };
    exitSelecting();
    Object.assign(L, L.saved[space] || SPACE_START[space]());
    L.space = space;
  }
  document.body.dataset.space = space;
  if (space === 'photos') $('kind').value = L.kind;
  $('lib-q').value = L.q;
  $('lib-q').placeholder = space === 'music' ? tr('Buscar en Música') : tr('Buscar en Imágenes');
  $('lib-q').title = space === 'music' ? tr('Tema, artista o álbum') : tr('Nombre o tag');
  setSel($('lib-sort'), L.sort);
  syncScopes();
  syncMusicControls();
  if (L.dirty) { refreshLibrary(); return; }
  if (!changed) return;
  if (L.overview) { renderCollections(L.overview.collections); renderTags(L.overview.tags); }
  renderMusicCard();
  reloadItems();
}

// The tabs on top (2026-11-03): Música and Imágenes always; Descargas and Convertir only if
// chosen under «+» (Bakin is about music first). A view whose tab is hidden still opens
// («Descargar» goes to Descargas): its tab shows while it's open.
const EXTRA_TABS = ['downloads', 'convert'];
function shownTabs() {
  const chosen = store.get('bakin.tabs', '').split(',').filter((t) => EXTRA_TABS.includes(t));
  return new Set(['music', 'photos', ...chosen]);
}
function syncTabs(current = $('view').value) {
  const shown = shownTabs();
  for (const b of document.querySelectorAll('#view > button, #view .gm-segment')) {
    const v = b.value || b.dataset.value;
    if (v) b.hidden = !shown.has(v) && v !== current;
  }
  for (const b of $('view-menu').querySelectorAll('button')) {
    b.setAttribute('role', 'menuitemcheckbox');  // gm-menu calls them menuitem
    b.setAttribute('aria-checked', String(shown.has(b.value)));
  }
  for (const t of document.querySelectorAll('.tab-toggle')) t.checked = shown.has(t.value);  // Configuración › Apariencia
}
function wireViews() {
  $('view').addEventListener('gm-change', (e) => setView(e.detail.value));
  $('view-more').addEventListener('click', () => $('view-menu').open());
  const showTab = (tab, on) => {
    const shown = shownTabs();
    if (on) shown.add(tab); else shown.delete(tab);
    store.set('bakin.tabs', [...shown].filter((t) => EXTRA_TABS.includes(t)).join(','));
    syncTabs();
  };
  $('view-menu').addEventListener('gm-select', (e) => {
    e.preventDefault();  // stay open: tick several
    showTab(e.detail.value, !shownTabs().has(e.detail.value));
  });
  for (const t of document.querySelectorAll('.tab-toggle')) t.addEventListener('change', () => showTab(t.value, t.checked));
  customElements.whenDefined('gm-menu').then(() => syncTabs());
}

// ------------------------------------------------------- general library
// Everything Bakin downloaded plus the user's own folders (any file type).
// Files are shown through the asset protocol, limited by the shell to the
// library's folders (sync_media_scope); deleting goes to the Recycle Bin.
const L = {
  scope: 'all',         // all | favorites | deleted
  collection: null,     // a collection id, or null for all
  tags: new Set(),
  kind: '',
  q: '',
  sort: 'new',
  items: [],
  total: 0,
  seq: 0,               // drops answers to superseded requests
  loading: 0,  // the seq of the list whose page is on its way
  dirty: true,
  overview: null,
  mview: null,          // a Bakin Música view: {view, value?, artist?, name?, back?}
  space: 'photos',      // Fotos or Música: one library, two spaces with their own state
  saved: {},
  selecting: false,
  selected: new Set(),
  lastIndex: -1,
  view: -1,             // index shown in the viewer
};
const LIB_PAGE = 60;
const assetUrl = (p) => tauri.core.convertFileSrc(p);
// a preview: a data URI, or a file of the preview cache (the library list's, 0.17.1)
const thumbSrc = (t) => (!t || t.startsWith('data:') ? t : assetUrl(t));
const KIND_LABEL = { image: tr('Foto'), video: 'Video', audio: 'Audio', other: tr('Archivo') };
const ICONS = {
  video: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m10 9 5 3-5 3z"/></svg>',
  audio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/></svg>',
  other: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
  image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
};
const fmtBytes = (b) => (b >= 1073741824 ? `${(b / 1073741824).toFixed(1)} GB`
  : b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
const fmtDay = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString(LOCALE, { day: 'numeric', month: 'short', year: 'numeric' }) : '');

function libFilters() {
  const kinds = L.space === 'music' ? ['audio'] : (L.kind ? [L.kind] : ['image', 'video', 'other']);
  return {
    collections: L.collection ? [L.collection] : [],
    kinds,
    favorites: L.scope === 'favorites',
    tags: [...L.tags],
    low_quality: !!L.lowq && L.kind === 'audio',
    // «Fondos» filters (PLAN 0.13.3): photos only
    ...(wpFiltersShown() ? { orient: L.orient || '', min_px: L.minpx || 0, color: L.color || '' } : {}),
    q: L.q,
    sort: L.sort,
    music: musicFilter(),
  };
}

async function refreshLibrary(rescan = false, { inPlace = false } = {}) {
  L.dirty = false;
  await loadOverview(rescan);
  await (inPlace ? refreshItemsInPlace() : reloadItems());
}

async function loadOverview(rescan = false) {
  try {
    const ov = await call('library_overview', { refresh: rescan });
    L.overview = ov;
    $('count-all').textContent = ov.photos;
    $('count-fav').textContent = ov.photo_favorites;
    $('count-deleted').textContent = ov.deleted_photos;
    $('mc-deleted').textContent = ov.deleted_music || '';
    if (L.collection && !ov.collections.some((c) => c.id === L.collection)) L.collection = null;
    for (const t of [...L.tags]) if (!ov.tags.some((x) => x.name === t)) L.tags.delete(t);
    renderCollections(ov.collections);
    renderTags(ov.tags);
    loadMusicOverview();
    tauri.core.invoke('sync_media_scope').catch((e) => log('warning', tr('Vista previa de archivos: {e}', { e })));
  } catch (err) {
    log('danger', tr('No se pudo leer la biblioteca: {err}', { err }));
  }
}

function renderCollections(all) {
  const music = L.space === 'music';
  const cols = all.filter((c) => !!c.music === music);  // each space shows its own collections
  $('collections-title').textContent = music ? tr('Carpetas') : tr('Colecciones');
  $('collections-empty').textContent = music
    ? tr('Las carpetas con música aparecen acá. Agrega otra con el +.')
    : tr('Baja fondos de Wallhaven o agrega una carpeta tuya con el botón +.');
  const ul = $('collections');
  ul.replaceChildren();
  $('collections-empty').hidden = cols.length > 0;
  for (const c of cols) {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'collection';
    b.title = c.path;
    if (L.collection === c.id) b.setAttribute('aria-current', 'true');
    const art = document.createElement('span');
    art.className = 'collection-art';
    if (c.cover) { const img = new Image(); img.alt = ''; img.src = c.cover; art.append(img); } else art.innerHTML = ICONS.folder;
    const name = document.createElement('span');
    name.className = 'collection-name';
    name.textContent = c.name;
    const n = document.createElement('small');
    n.textContent = c.exists ? c.count : tr('no encontrada');
    b.append(art, name, n);
    b.addEventListener('click', () => selectCollection(L.collection === c.id ? null : c.id));
    li.append(b);
    if (c.kind === 'folder') {
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'gm-chip gm-chip-plain gm-chip-round collection-remove';
      x.title = tr('Quitar de la biblioteca (no borra archivos)');
      x.setAttribute('aria-label', tr('Quitar {name} de la biblioteca', { name: c.name }));
      x.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
      x.addEventListener('click', () => removeFolder(c));
      li.append(x);
    }
    ul.append(li);
  }
}

function renderTags(tags) {
  const box = $('tag-cloud');
  box.replaceChildren();
  $('tags-empty').hidden = tags.length > 0;
  $('btn-tags-clear').hidden = !L.tags.size;
  const opts = $('tag-options');
  opts.replaceChildren();
  for (const t of tags) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'gm-chip gm-chip-plain';
    b.setAttribute('aria-pressed', String(L.tags.has(t.name)));
    b.textContent = `${t.name} · ${t.count}`;
    b.addEventListener('click', () => {
      if (L.tags.has(t.name)) L.tags.delete(t.name); else L.tags.add(t.name);
      if (L.scope === 'deleted') setScope('all', false);
      renderTags(tags);
      reloadItems();
    });
    box.append(b);
    const o = document.createElement('option');
    o.value = t.name;
    opts.append(o);
  }
}

function selectCollection(id) {
  leaveMusicView();
  L.collection = id;
  if (L.scope === 'deleted') L.scope = 'all';
  syncScopes();
  if (L.overview) renderCollections(L.overview.collections);
  reloadItems();
}

function setScope(scope, reload = true) {
  leaveMusicView();
  L.scope = scope;
  syncScopes();
  if (reload) reloadItems();
}

// Each number once: the list's count shows only when the sidebar isn't
// already showing it (a search or filter is on, the entry has no number, or
// the sidebar is collapsed or closed).
function showCount(text) {
  if (text !== undefined) L.countText = text;
  // the sidebar's number counts only where it is seen (not folded, not a closed drawer)
  const sideSeen = getComputedStyle($('side-library')).visibility !== 'hidden';
  const cur = sideSeen ? [...document.querySelectorAll('#side-library [aria-current="true"] small')].find((s) => s.offsetParent) : null;
  const filtered = L.q || L.tags.size || L.lowq || (L.space === 'photos' && L.kind);
  $('lib-count').textContent = filtered || !cur?.textContent.trim() ? L.countText || '' : '';
}

function syncScopes() {
  for (const b of document.querySelectorAll('#scopes .scope')) {
    if (b.dataset.scope === L.scope) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
  }
  const trash = L.scope === 'deleted';
  $('lib-filters').hidden = trash;
  $('btn-select').hidden = trash;
  $('trash').hidden = !trash;
  $('lib-grid').hidden = trash;
  if (trash) exitSelecting();
}

function libTitle() {
  if (L.scope === 'deleted') return tr('Eliminadas');
  if (L.mview) return L.mview.name || MVIEWS[L.mview.view];
  const col = L.overview?.collections.find((c) => c.id === L.collection);
  const parts = [col ? col.name : (L.scope === 'favorites' ? tr('Favoritas') : tr('Todo'))];
  if (col && L.scope === 'favorites') parts.push('favoritas');
  if (L.tags.size) parts.push([...L.tags].map((t) => `#${t}`).join(' '));
  return parts.join(' · ');
}

async function reloadItems() {
  const seq = ++L.seq;
  refreshWallpaperShown();
  L.items = [];
  L.total = 0;
  L.lastIndex = -1;
  const grid = $('lib-grid');
  grid.replaceChildren();
  $('btn-lib-more').hidden = true;  // the old list's «Cargar más» goes with it
  Object.assign(VG, { first: -1, last: -1, rowH: 0 });
  VG.spare.clear();
  grid.classList.toggle('lib-rows', musicMode());
  grid.classList.toggle('gm-media-grid', !musicMode());
  if (musicMode()) grid.append(musicHeader());
  $('lib-title').textContent = libTitle();
  syncMusicHeader();
  const groups = groupView();
  const stats = L.mview?.view === 'stats';
  $('lib-groups').hidden = !groups || stats;
  $('lib-stats').hidden = !stats;
  grid.hidden = groups || L.scope === 'deleted';
  if (L.scope === 'deleted') { await loadTrash(seq); return; }
  if (stats) { $('lib-empty').hidden = true; showCount(''); await renderStats(seq); return; }
  if (groups) { await renderGroups(seq); return; }
  await loadMoreItems(seq);
}

function syncMusicHeader() {
  const v = L.mview?.view;
  const back = L.mview?.back;
  $('lib-back').hidden = !back;
  if (back) $('lib-back').textContent = `← ${MVIEWS[back]}`;
  $('music-actions').hidden = !L.mview || groupView();
  const playlist = v === 'playlist';
  const pl = playlist && MUSIC?.playlists.find((p) => p.id === L.mview.value);
  $('pl-edit').hidden = !playlist;
  $('pl-export').hidden = !playlist;
  $('pl-mirror').hidden = !playlist;
  const mirrored = playlist && MUSIC?.playlists.find((p) => p.id === L.mview?.value)?.mirror;
  $('pl-mirror').textContent = mirrored ? tr('Espejo ✓') : tr('Carpeta espejo…');
  $('pl-mirror').title = mirrored ? tr('Se mantiene igual a la playlist: {mirrored}', { mirrored }) : tr('Una carpeta (el auto, el celular, otro reproductor) que Bakin mantiene igual a esta playlist');
  $('pl-delete').hidden = !playlist;
  $('sel-rmpl').hidden = !(pl && pl.kind === 'list');
  const queue = v === 'queue';
  $('queue-clear').hidden = !queue;
  $('mus-play').hidden = queue;
  $('mus-shuffle').hidden = queue;
  $('lib-sort').closest('gm-select').hidden = groupView() || orderedView();
  $('btn-select').hidden = groupView() || L.scope === 'deleted';
}

async function loadMoreItems(seq = L.seq) {
  // L.loading is the list being loaded: a page of an older list still on its
  // way must not block the new one (0.9.2: switching lists while the engine
  // was busy left an empty list stuck with the old «Cargar más»)
  if (L.loading === seq || groupView()) return;
  L.loading = seq;
  const more = $('btn-lib-more');
  more.setAttribute('aria-busy', 'true');
  try {
    const res = await call('library_items', { filters: libFilters(), offset: L.items.length, limit: LIB_PAGE });
    if (seq !== L.seq) return;
    L.total = res.total;
    if (!L.items.length) L.square = res.items.length > 0 && res.items.every((i) => i.kind === 'audio');
    L.items.push(...res.items);
    renderWindow(true);
    showListState();
  } catch (err) {
    toast(tr(String(err)));
  } finally {
    if (L.loading === seq) L.loading = 0;
    more.removeAttribute('aria-busy');
  }
}

// «Cargar más», the count and the empty notice, for the list as it is now.
function showListState() {
  const more = $('btn-lib-more');
  const left = L.total - L.items.length;
  more.hidden = left <= 0;
  more.textContent = tr('Cargar más ({left})', { left });
  showCount(musicMode() ? tn('{n} tema', '{n} temas', L.total) : tn('{n} archivo', '{n} archivos', L.total));
  const empty = L.total === 0;
  $('lib-empty').hidden = !empty;
  const why = empty && !L.q && MUSIC_EMPTY[L.mview?.view];
  if (why) {
    [$('lib-empty-title').textContent, $('lib-empty-text').textContent] = why;
  } else if (empty && L.kind === 'audio' && !L.q && !L.tags.size && !L.lowq && L.scope !== 'favorites' && !L.collection) {
    const music = MEDIA && presetById(MEDIA.default_preset)?.folder;
    $('lib-empty-title').textContent = tr('Todavía no hay música en la biblioteca');
    $('lib-empty-text').textContent = tr('Copia tu música a {music} (con su archive.txt, si lo tienes), ', { music: music || tr('la carpeta de tu preset de música') })
      + tr('agrega otra carpeta con el + de Colecciones, o pega un enlace arriba.');
  } else if (empty) {
    const filtered = L.kind || L.q || L.tags.size || L.scope === 'favorites' || L.collection;
    $('lib-empty-title').textContent = filtered ? tr('Nada coincide con el filtro') : tr('Tu biblioteca está vacía');
    $('lib-empty-text').textContent = filtered ? tr('Prueba quitando algún filtro.')
      : tr('Baja fondos de Wallhaven («Descargar» arriba) o agrega una carpeta tuya con el + de Colecciones.');
  }
  // words that aren't in the library: the search in Bakin is of what you have (2026-11-06: a
  // friend typed an artist to find new music and saw an empty list); offer the web
  const web = $('lib-empty-web');
  web.hidden = !(empty && L.q && musicMode());
  if (!web.hidden) {
    $('lib-empty-title').textContent = tr('No tienes nada de «{q}»', { q: L.q });
    $('lib-empty-text').textContent = tr('Esta búsqueda es de tu biblioteca. Para bajar música nueva, búscala en YouTube Music y pega el enlace en «Descargar».');
    web.textContent = tr('Buscar «{q}» en YouTube Music', { q: L.q });
  }
}

// The same list read again where it stands (files came or went): the new rows replace the old
// ones once they're here, so the list neither blinks nor jumps back to the top (2026-11-06: it
// did after every download). As many rows as were loaded, so the place it was at still exists.
async function refreshItemsInPlace() {
  if (!L.items.length || groupView() || L.scope === 'deleted') return reloadItems();
  const seq = ++L.seq;
  const want = L.items.length;
  const items = [];
  let total = 0;
  try {
    while (items.length < want) {
      const res = await call('library_items', { filters: libFilters(), offset: items.length, limit: Math.min(120, want - items.length) });
      if (seq !== L.seq) return;
      total = res.total;
      items.push(...res.items);
      if (!res.items.length || items.length >= total) break;
    }
  } catch (err) {
    log('warning', tr('No se pudo releer la lista: {err}', { err }));
    return;
  }
  L.items = items;
  L.total = total;
  if (!items.length) { reloadItems(); return; }
  renderWindow(true);
  showListState();
}

// ------------------------------------------------------------ virtual grid
// Only the tiles near the visible part of the library are in the page; the
// rest is a spacer of the same height. With thousands of files, keeping every
// tile (and its decoded cover) made scrolling and memory heavy. Tiles still
// in range are kept as they are, so nothing blinks while scrolling.
const VG = { first: -1, last: -1, cols: 1, rowH: 0, gap: 0, raf: 0, spare: new Map() };  // spare: recently left tiles, reused

function vgSpacers(grid) {
  let top = grid.querySelector(':scope > .lib-spacer-top');
  let bottom = grid.querySelector(':scope > .lib-spacer-bottom');
  if (!top) {
    top = document.createElement('div');
    bottom = document.createElement('div');
    top.className = 'lib-spacer lib-spacer-top';
    bottom.className = 'lib-spacer lib-spacer-bottom';
    top.setAttribute('aria-hidden', 'true');
    bottom.setAttribute('aria-hidden', 'true');
    grid.append(top, bottom);
  }
  return [top, bottom];
}

function vgMeasure(grid) {
  const tile = grid.querySelector(':scope > .lib-tile, :scope > .lib-row:not(.lib-row-head)');
  if (!tile) return false;
  const cs = getComputedStyle(grid);
  VG.gap = parseFloat(cs.rowGap) || 0;
  VG.cols = musicMode() ? 1 : Math.max(1, cs.gridTemplateColumns.split(' ').filter(Boolean).length);
  const h = tile.getBoundingClientRect().height;
  VG.rowH = h ? h + VG.gap : 0;  // hidden view: measure again once it shows
  return VG.rowH > 0;
}

function vgSlice(first, last, rowsAbove, rowsBelow) {
  const grid = $('lib-grid');
  const [top, bottom] = vgSpacers(grid);
  const keep = new Map();
  for (let el = top.nextElementSibling; el && el !== bottom; el = el.nextElementSibling) keep.set(Number(el.dataset.vi), el);
  const nodes = [];
  for (let i = first; i <= last; i++) {
    let el = keep.get(i);
    if (el && el.dataset.id === L.items[i].id) keep.delete(i);
    else {
      el = VG.spare.get(i);
      if (el && el.dataset.id === L.items[i].id) VG.spare.delete(i);
      else { el = tileFor(L.items[i], i); el.dataset.vi = i; }
    }
    nodes.push(el);
  }
  for (const [i, el] of keep) {
    el.querySelectorAll('video').forEach((v) => lazyMedia.unobserve(v));
    el.remove();
    if (!el.querySelector('video')) VG.spare.set(i, el);  // already decoded: free to bring back
  }
  while (VG.spare.size > 180) VG.spare.delete(VG.spare.keys().next().value);
  top.after(...nodes);
  top.hidden = !rowsAbove;
  top.style.height = rowsAbove ? `${rowsAbove * VG.rowH - VG.gap}px` : '';
  bottom.hidden = !rowsBelow;
  bottom.style.height = rowsBelow ? `${rowsBelow * VG.rowH - VG.gap}px` : '';
  VG.first = first;
  VG.last = last;
}

function renderWindow(force = false) {
  if (L.scope === 'deleted') return;
  const grid = $('lib-grid');
  const n = L.items.length;
  if (!n) { vgSlice(0, -1, 0, 0); return; }
  if (!VG.rowH) {  // first page: draw it, then measure a tile
    vgSlice(0, Math.min(n, LIB_PAGE) - 1, 0, 0);
    if (!vgMeasure(grid)) return;
    force = true;
  }
  const scroller = $('view-library');
  const head = grid.querySelector(':scope > .lib-row-head');
  const gridTop = grid.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
    + (head ? head.offsetHeight + VG.gap : 0);
  const view = scroller.clientHeight;  // one screen above and one below stay ready
  const rows = Math.ceil(n / VG.cols);
  const firstRow = Math.min(rows - 1, Math.max(0, Math.floor((scroller.scrollTop - gridTop - view) / VG.rowH)));
  const lastRow = Math.max(firstRow, Math.min(rows - 1, Math.ceil((scroller.scrollTop - gridTop + 2 * view) / VG.rowH)));
  const first = firstRow * VG.cols;
  const last = Math.min(n - 1, (lastRow + 1) * VG.cols - 1);
  if (!force && first === VG.first && last === VG.last) return;
  vgSlice(first, last, firstRow, rows - 1 - lastRow);
}

function scheduleWindow(remeasure = false) {
  if (remeasure && VG.rowH) VG.remeasure = true;
  if (VG.raf) return;
  VG.raf = requestAnimationFrame(() => {
    VG.raf = 0;
    if (VG.remeasure) { VG.remeasure = false; if (vgMeasure($('lib-grid'))) { renderWindow(true); return; } }
    renderWindow();
  });
}

// Videos only start loading when their tile scrolls into view.
const lazyMedia = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const v = e.target;
    v.src = `${assetUrl(v.dataset.path)}#t=0.5`;
    lazyMedia.unobserve(v);
  }
}, { rootMargin: '300px' });

const autoMore = new IntersectionObserver((entries) => {
  if (entries.some((e) => e.isIntersecting) && !$('btn-lib-more').hidden && L.scope !== 'deleted') loadMoreItems();
}, { rootMargin: '2000px' });  // early: a fast flick shouldn't reach the end before the next page (0.13 review)

function tileFor(it, index) {
  if (musicMode()) return rowFor(it, index);
  const tile = document.createElement('button');
  tile.type = 'button';
  tile.className = 'gm-media-tile lib-tile';
  if (L.square) tile.dataset.ratio = '1/1';  // a list of songs: square, like their covers
  tile.dataset.id = it.id;
  tile.dataset.kind = it.kind;
  if (it.kind === 'image') tile.dataset.path = it.path;
  const dims = it.width ? ` · ${it.width}×${it.height}` : '';
  tile.title = `${it.name}\n${KIND_LABEL[it.kind]}${dims} · ${fmtBytes(it.size)}${it.tags.length ? '\n#' + it.tags.join(' #') : ''}`;
  if (it.quality === 'fallback_below_listed_max') tile.dataset.tone = 'warning';

  if ((it.kind === 'image' || it.kind === 'audio') && it.thumb) {
    const img = new Image();
    img.alt = '';
    img.decoding = 'sync';  // a small thumbnail: drawn at once, no empty frame while scrolling
    img.src = thumbSrc(it.thumb);
    tile.append(img);
    if (it.kind === 'audio') {  // the whole cover, over a blurred copy of itself
      img.className = 'cover-art';
      const bg = new Image();
      bg.alt = '';
      bg.className = 'cover-bg';
      bg.src = thumbSrc(it.thumb);
      img.before(bg);
      const cap = document.createElement('span');
      cap.className = 'cover-caption';
      cap.textContent = it.title || it.name.replace(/\.[^.]+$/, '');
      tile.append(cap);
    }
  } else if (it.kind === 'video') {
    const v = document.createElement('video');
    v.muted = true;
    v.preload = 'metadata';
    v.dataset.path = it.path;
    lazyMedia.observe(v);
    const play = document.createElement('span');
    play.className = 'play-glyph';
    play.innerHTML = ICONS.video;
    tile.append(v, play);
  } else {
    const f = document.createElement('span');
    f.className = 'file-face';
    f.innerHTML = ICONS[it.kind] || ICONS.other;
    const n = document.createElement('span');
    n.className = 'file-name';
    n.textContent = it.name;
    f.append(n);
    tile.append(f);
  }

  const badge = document.createElement('span');
  badge.className = 'gm-badge';
  badge.dataset.corner = 'bottom-start';
  badge.textContent = it.width ? `${it.width}×${it.height}` : it.ext.toUpperCase() || KIND_LABEL[it.kind];
  tile.append(badge);

  const heart = document.createElement('span');
  heart.className = 'fav-mark';
  heart.dataset.corner = 'top-start';
  heart.textContent = '♥';
  heart.hidden = !it.fav;
  tile.append(heart);

  const check = document.createElement('input');
  check.type = 'checkbox';
  check.className = 'gm-check sel-mark';
  check.tabIndex = -1;
  check.setAttribute('aria-hidden', 'true');
  check.checked = L.selected.has(it.id);
  tile.append(check);
  tile.setAttribute('aria-pressed', String(L.selecting && L.selected.has(it.id)));

  tile.addEventListener('click', (e) => {
    if (L.selecting || e.ctrlKey || e.shiftKey) {
      if (!L.selecting) enterSelecting();
      toggleSelect(index, e.shiftKey);
    } else if (it.kind === 'audio') {
      playFromLibrary(it.id);
    } else {
      openViewer(index);
    }
  });
  if (it.kind === 'image') {
    tile.addEventListener('contextmenu', (e) => { e.preventDefault(); openWallpaperMenu(it, tile); });
    markWallpaperTile(tile);
  }
  return tile;
}

// «Poner de fondo» (PLAN 0.13.1): one menu for a photo's right click and the
// viewer's button. gm-menu wires its items once, so a fresh one is built each
// time (the monitors can change), inside the viewer when it is open (a modal
// leaves everything outside it inert).
let wallpaperSeq = 0;
async function openWallpaperMenu(it, anchor) {
  let r;
  try { r = await tauri.core.invoke('wallpaper_monitors'); } catch (err) { toast(tr(String(err)), { duration: 6000 }); return; }
  if (!r.monitors.length) { toast(tr('Windows no informa ningún monitor.'), { duration: 6000 }); return; }
  $('wallpaper-menu')?.remove();
  const menu = document.createElement('gm-menu');
  menu.id = 'wallpaper-menu';
  const head = document.createElement('small');
  head.className = 'menu-head';
  head.textContent = tr('Poner de fondo en');
  menu.append(head);
  for (const m of r.monitors) {
    const b = document.createElement('button');
    b.type = 'button';
    b.value = String(m.number);
    b.textContent = tr('Monitor {number} · {w}×{h}', { number: m.number, w: m.w, h: m.h }) + (m.vertical ? tr(' (vertical)') : '');
    menu.append(b);
  }
  // see it on a monitor's shape and move the frame first (0.13 review)
  const framing = document.createElement('button');
  framing.type = 'button';
  framing.value = 'frame';
  framing.textContent = tr('Ajustar encuadre…');
  if (r.monitors.length > 1) {
    menu.append(document.createElement('hr'));
    const all = document.createElement('button');
    all.type = 'button';
    all.value = 'all';
    all.textContent = tr('Todos los monitores');
    menu.append(all);
  }
  // only where Windows says apps may change it (PLAN 0.13.6), and not again once it refused
  menu.append(document.createElement('hr'), framing);
  if (r.lock && store.get('bakin.lock', '') !== 'no') {
    menu.append(document.createElement('hr'));
    const lock = document.createElement('button');
    lock.type = 'button';
    lock.value = 'lock';
    lock.textContent = tr('Pantalla de bloqueo');
    menu.append(lock);
  }
  if (!anchor.id) anchor.id = `wp-anchor-${++wallpaperSeq}`;
  menu.setAttribute('anchor', anchor.id);
  menu.addEventListener('gm-select', async (e) => {
    if (e.detail.value === 'frame') { openFrame(it, r.monitors); return; }
    if (e.detail.value === 'lock') {
      const res = await tauri.core.invoke('lockscreen_set', { path: it.path }).catch((err) => ({ error: String(err) }));
      if (res.error) toast(tr(res.error), { duration: 6000 });
      else if (res.set) toast(tr('Listo: pantalla de bloqueo.'));
      else if (!res.supported) toast(tr('La pantalla de bloqueo no se puede cambiar desde una app en esta edición de Windows.'), { duration: 7000 });
      else if (!r.dry) {
        store.set('bakin.lock', 'no');  // it won't change its mind: stop offering it
        toast(tr('Windows no deja cambiar la pantalla de bloqueo desde Bakin (solo lo permite a apps de la Tienda). No se vuelve a ofrecer.'), { duration: 8000 });
      }
      return;
    }
    const number = e.detail.value === 'all' ? null : Number(e.detail.value);
    try {
      await tauri.core.invoke('wallpaper_set', { path: it.path, number });
      toast(number ? tr('Listo: fondo del monitor {number}.', { number }) : tr('Listo: fondo de todos los monitores.'));
      refreshWallpaperShown();
    } catch (err) {
      toast(tr(String(err)), { duration: 6000 });
    }
  });
  (anchor.closest('gm-dialog') || document.body).append(menu);
  await customElements.whenDefined('gm-menu');
  menu.open();
}

function tileOf(id) { return $('lib-grid').querySelector(`[data-id="${CSS.escape(id)}"]`); }

function markTile(id) {
  const t = tileOf(id);
  if (!t) return;
  const it = L.items.find((x) => x.id === id);
  const on = L.selected.has(id);
  t.setAttribute('aria-pressed', String(L.selecting && on));
  t.querySelector('.sel-mark').checked = on;
  if (!it) return;
  const heart = t.querySelector('.fav-mark');
  if (heart.classList.contains('row-fav')) {
    heart.dataset.on = String(!!it.fav);
    heart.title = it.fav ? tr('Quitar de favoritas') : tr('Agregar a favoritas');
    heart.setAttribute('aria-label', heart.title);
  } else heart.hidden = !it.fav;
}

// ------------------------------------------------------------- selection
function enterSelecting() {
  L.selecting = true;
  document.body.classList.add('selecting');
  $('selbar').hidden = false;
  $('btn-select').hidden = true;
  updateSelbar();
}

function exitSelecting() {
  L.selecting = false;
  const was = [...L.selected];
  L.selected.clear();
  document.body.classList.remove('selecting');
  $('selbar').hidden = true;
  $('btn-select').hidden = L.scope === 'deleted';
  was.forEach(markTile);
}

function toggleSelect(index, range) {
  const ids = range && L.lastIndex >= 0
    ? L.items.slice(Math.min(L.lastIndex, index), Math.max(L.lastIndex, index) + 1).map((i) => i.id)
    : [L.items[index].id];
  const on = range ? true : !L.selected.has(ids[0]);
  for (const id of ids) { if (on) L.selected.add(id); else L.selected.delete(id); markTile(id); }
  L.lastIndex = index;
  updateSelbar();
}

function updateSelbar() {
  const n = L.selected.size;
  $('sel-count').textContent = tn('{n} seleccionada', '{n} seleccionadas', n);
  for (const id of ['sel-fav', 'sel-unfav', 'sel-tag', 'sel-delete', 'sel-convert']) $(id).disabled = n === 0;
  $('sel-all').textContent = L.items.length < L.total ? tr('Seleccionar las {items} cargadas', { items: L.items.length }) : tr('Seleccionar todo');
}

// --------------------------------------------------------------- actions
async function setFavorite(ids, value) {
  try {
    await call('library_set_favorite', { ids, value });
  } catch (err) { toast(tr(String(err))); return; }
  for (const it of L.items) if (ids.includes(it.id)) it.fav = value;
  ids.forEach(markTile);
  loadOverview();
  if (L.scope === 'favorites' && !value) reloadItems();
}

async function applyTags(ids, add, remove) {
  try {
    await call('library_tag', { ids, add, remove });
  } catch (err) { toast(tr(String(err))); return false; }
  const drop = new Set(remove.map((t) => t.toLowerCase()));
  for (const it of L.items) {
    if (!ids.includes(it.id)) continue;
    it.tags = it.tags.filter((t) => !drop.has(t.toLowerCase()));
    for (const t of add) if (!it.tags.some((x) => x.toLowerCase() === t.toLowerCase())) it.tags.push(t);
  }
  loadOverview();
  return true;
}

function openTagDialog(ids) {
  const items = L.items.filter((i) => ids.includes(i.id));
  const existing = [...new Set(items.flatMap((i) => i.tags))].sort((a, b) => a.localeCompare(b));
  const removing = new Set();
  $('tag-dialog-meta').textContent = tn('{n} archivo seleccionado.', '{n} archivos seleccionados.', ids.length);
  $('tag-add').value = '';
  const box = $('tag-existing');
  box.replaceChildren();
  $('tag-existing-wrap').hidden = !existing.length;
  for (const t of existing) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'gm-chip gm-chip-plain';
    b.setAttribute('aria-pressed', 'false');
    b.textContent = t;
    b.title = tr('Marcar para quitar');
    b.addEventListener('click', () => {
      if (removing.has(t)) removing.delete(t); else removing.add(t);
      b.setAttribute('aria-pressed', String(removing.has(t)));
      b.classList.toggle('strike', removing.has(t));
    });
    box.append(b);
  }
  const dlg = $('tag-dialog');
  const apply = async () => {
    const add = $('tag-add').value.split(',').map((t) => t.trim()).filter(Boolean);
    if (!add.length && !removing.size) { dlg.close(); return; }
    if (await applyTags(ids, add, [...removing])) {
      dlg.close();
      toast(tr('Tags actualizados.'));
      if (L.tags.size) reloadItems();
    }
  };
  $('btn-tag-apply').onclick = apply;
  $('tag-add').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); apply(); } };
  $('btn-tag-cancel').onclick = () => dlg.close();
  dlg.show();
  setTimeout(() => $('tag-add').focus(), 50);
}

function confirmDelete(ids) {
  const n = ids.length;
  $('delete-text').textContent = n === 1
    ? tr('«{name}» se enviará a la Papelera de Windows.', { name: L.items.find((i) => i.id === ids[0])?.name || tr('este archivo') })
    : tr('{n} archivos se enviarán a la Papelera de Windows.', { n });
  const dlg = $('delete-dialog');
  return new Promise((resolve) => {
    dlg.addEventListener('gm-close', (e) => resolve(e.detail?.value === 'ok'), { once: true });
    dlg.show();
  });
}

async function deleteItems(ids) {
  if (!ids.length || !(await confirmDelete(ids))) return false;
  let res;
  try { res = await call('library_delete', { ids }); } catch (err) { toast(tr(String(err))); return false; }
  log(res.ok ? 'info' : 'warning', tr(res.message));
  const restoreIds = ids.filter((id) => !res.failed.some((f) => id.endsWith(`|${f.name}`)));
  toast(tr(res.message), {
    duration: 8000,
    ...(res.deleted ? { action: { label: tr('Deshacer'), onClick: () => restoreItems(restoreIds) } } : {}),
  });
  exitSelecting();
  await refreshLibrary(false, { inPlace: true });
  return res.deleted > 0;
}

async function restoreItems(ids) {
  let res;
  try { res = await call('library_restore', { ids }); } catch (err) { toast(tr(String(err))); return; }
  toast(tr(res.message), { duration: 6000 });
  log(res.ok ? 'success' : 'warning', tr(res.message));
  await refreshLibrary(false, { inPlace: true });
}

async function addFolder() {
  const dir = await tauri.dialog.open({ directory: true, title: tr('Agregar una carpeta a la biblioteca') });
  if (typeof dir !== 'string') return;
  const res = await call('library_add_folder', { path: dir }).catch((e) => ({ ok: false, error: String(e) }));
  if (!res.ok) { toast(tr(res.error), { duration: 6000 }); return; }
  toast(tr('Carpeta agregada a la biblioteca.'));
  L.collection = res.id;
  await refreshLibrary();
}

function removeFolder(c) {
  $('remove-folder-text').textContent = `${c.name} (${c.path})`;
  const dlg = $('remove-folder-dialog');
  dlg.addEventListener('gm-close', async (e) => {
    if (e.detail?.value !== 'ok') return;
    await call('library_remove_folder', { collection: c.id });
    if (L.collection === c.id) L.collection = null;
    refreshLibrary();
  }, { once: true });
  dlg.show();
}

// ------------------------------------------------------------------ trash
async function loadTrash(seq = L.seq) {
  let res;
  try { res = await call('library_deleted'); } catch (err) { toast(tr(String(err))); return; }
  if (seq !== L.seq) return;
  res.items = res.items.filter((d) => (d.kind === 'audio') === (L.space === 'music'));
  const ul = $('trash-list');
  ul.replaceChildren();
  showCount(tn('{n} archivo', '{n} archivos', res.items.length));
  $('lib-empty').hidden = res.items.length > 0;
  $('btn-lib-more').hidden = true;
  if (!res.items.length) {
    $('lib-empty-title').textContent = tr('No hay nada eliminado');
    $('lib-empty-text').textContent = tr('Lo que elimines desde la biblioteca aparecerá aquí para poder recuperarlo.');
  }
  for (const d of res.items) {
    const li = document.createElement('li');
    li.className = 'trash-item';
    const icon = document.createElement('span');
    icon.className = 'trash-icon';
    icon.innerHTML = ICONS[d.kind] || ICONS.other;
    const body = document.createElement('div');
    body.className = 'trash-body';
    const name = document.createElement('strong');
    name.textContent = d.name;
    const meta = document.createElement('small');
    const when = d.deleted_at ? new Date(d.deleted_at).toLocaleString(LOCALE, { dateStyle: 'medium', timeStyle: 'short' }) : '';
    meta.textContent = `${d.collection} · ${when}${d.size ? ' · ' + fmtBytes(d.size) : ''}`
      + (d.in_recycle_bin ? '' : tr(' · ya no está en la Papelera de Windows'));
    body.append(name, meta);
    const chips = document.createElement('div');
    chips.className = 'chips';
    const mk = (label, cls, fn, hint, disabled = false) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = `gm-chip ${cls}`;
      b.textContent = label;
      if (hint) b.title = hint;
      b.disabled = disabled;
      b.addEventListener('click', fn);
      chips.append(b);
    };
    mk(tr('Restaurar'), '', () => restoreItems([d.id]), tr('La devuelve a su lugar desde la Papelera de Windows'), !d.in_recycle_bin);
    mk(tr('Quitar de la lista'), 'gm-chip-plain', async () => {
      await call('library_forget', { ids: [d.id], redownload: false });
      refreshLibrary();
    }, d.blocks_redownload ? tr('Deja de mostrarla aquí; tampoco se volverá a descargar') : tr('Deja de mostrarla aquí'));
    li.append(icon, body, chips);
    ul.append(li);
  }
}

// ----------------------------------------------------------------- viewer
function openViewer(index) {
  L.view = index;
  toggleViewerInfo(false);
  renderViewer();
  $('viewer').show();
  wakeViewer();
}

let viewerSeq = 0;
function renderViewer() {
  const it = L.items[L.view];
  if (!it) return;
  const dlg = $('viewer');
  $('v-title').textContent = it.name;
  $('v-count').textContent = tr('{n} de {total}', { n: L.view + 1, total: L.total });
  const stage = $('viewer-stage');
  const src = assetUrl(it.path);
  const shown = ++viewerSeq;
  if (it.kind === 'image') {
    // the previous photo stays until this one can be drawn; both fill the same box
    const img = new Image();
    img.alt = it.name;
    const show = () => { if (shown === viewerSeq) stage.replaceChildren(img); };
    const full = new Image();
    full.onload = () => full.decode().catch(() => {}).then(() => { img.src = full.src; show(); });
    full.onerror = () => { if (!it.thumb && shown === viewerSeq) stage.innerHTML = `<div class="file-face big">${ICONS.image}<span class="file-name">${tr('Vista previa no disponible: usa «Abrir con…»')}</span></div>`; };
    if (it.thumb) { img.src = thumbSrc(it.thumb); img.decode().catch(() => {}).then(show); }  // quick, then the full file
    full.src = src;
  } else if (it.kind === 'video') {
    stage.replaceChildren();
    const v = document.createElement('video');
    v.controls = true;
    v.autoplay = true;
    v.src = src;
    v.onerror = () => { stage.innerHTML = `<div class="file-face big">${ICONS.video}<span class="file-name">${tr('Este formato no se puede reproducir aquí: usa «Abrir con…»')}</span></div>`; };
    stage.append(v);
  } else if (it.kind === 'audio') {
    stage.replaceChildren();
    const box = document.createElement('div');
    box.className = 'file-face big';
    box.innerHTML = ICONS.audio;
    const a = document.createElement('audio');
    a.controls = true;
    a.autoplay = true;
    a.addEventListener('play', () => { if (plPlaying()) pc({ cmd: 'pause' }); });
    a.src = src;
    box.append(a);
    stage.append(box);
  } else {
    stage.innerHTML = `<div class="file-face big">${ICONS.other}<span class="file-name">${tr('{ext}: usa «Abrir con…»', { ext: it.ext.toUpperCase() || tr('Archivo') })}</span></div>`;
  }

  const col = L.overview?.collections.find((c) => c.id === it.col);
  const lines = [
    col ? col.name : '',
    [KIND_LABEL[it.kind], it.width ? `${it.width}×${it.height}` : '', fmtBytes(it.size)].filter(Boolean).join(' · '),
    fmtDay(it.date),
    tr('{n} de {total}', { n: L.view + 1, total: L.total }),
  ].filter(Boolean);
  $('viewer-meta').textContent = lines.join('\n');
  syncViewerFav(it);
  renderViewerTags(it);
  $('v-wallpaper').hidden = it.kind !== 'image';
  $('v-prev').disabled = L.view <= 0;
  $('v-next').disabled = L.view >= L.total - 1;
}

function syncViewerFav(it) {
  $('v-fav').setAttribute('aria-pressed', String(it.fav));
  $('v-fav').title = it.fav ? tr('Quitar de favoritas (F)') : tr('Favorita (F)');
}

// The viewer's controls sit on the photo and fade out after a moment without
// the mouse moving; the details are a side panel, closed until asked for.
let viewerIdle = 0;
function wakeViewer() {
  const body = $('viewer-body');
  body.classList.remove('idle');
  clearTimeout(viewerIdle);
  viewerIdle = setTimeout(() => { if ($('viewer-side').hidden) body.classList.add('idle'); }, 2200);
}

function toggleViewerInfo(open = $('viewer-side').hidden) {
  $('viewer-side').hidden = !open;
  $('v-info').setAttribute('aria-pressed', String(open));
  wakeViewer();
}

function renderViewerTags(it) {
  const box = $('v-tags');
  box.replaceChildren();
  for (const t of it.tags) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'gm-chip gm-chip-plain';
    b.textContent = `${t} ×`;
    b.title = tr('Quitar tag');
    b.addEventListener('click', async () => { if (await applyTags([it.id], [], [t])) renderViewerTags(it); });
    box.append(b);
  }
}

async function stepViewer(d) {
  const next = L.view + d;
  if (next < 0 || next >= L.total) return;
  if (next >= L.items.length) await loadMoreItems();
  if (next < L.items.length) { L.view = next; renderViewer(); }
}

function wireViewer() {
  const dlg = $('viewer');
  dlg.addEventListener('gm-close', () => { $('viewer-stage').replaceChildren(); clearTimeout(viewerIdle); });  // stops playback
  $('viewer-body').addEventListener('pointermove', wakeViewer);
  $('v-info').addEventListener('click', () => toggleViewerInfo());
  $('v-close').addEventListener('click', () => dlg.close());
  $('v-prev').addEventListener('click', () => stepViewer(-1));
  $('v-next').addEventListener('click', () => stepViewer(1));
  $('v-fav').addEventListener('click', async () => {
    const it = L.items[L.view];
    await setFavorite([it.id], !it.fav);
    syncViewerFav(it);
  });
  $('v-open').addEventListener('click', () => call('library_open', { item_id: L.items[L.view].id }));
  $('v-reveal').addEventListener('click', () => call('library_reveal', { item_id: L.items[L.view].id }));
  $('v-wallpaper').addEventListener('click', (e) => openWallpaperMenu(L.items[L.view], e.currentTarget));
  $('v-delete').addEventListener('click', async () => {
    const it = L.items[L.view];
    dlg.close();
    await deleteItems([it.id]);
  });
  $('v-tag-input').addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const it = L.items[L.view];
    const add = e.target.value.split(',').map((t) => t.trim()).filter(Boolean);
    if (add.length && await applyTags([it.id], add, [])) { e.target.value = ''; renderViewerTags(it); }
  });
  document.addEventListener('keydown', (e) => {
    if (!dlg.hasAttribute('open') || e.target.closest('input, textarea')) return;
    wakeViewer();
    if (e.key === 'ArrowRight') stepViewer(1);
    if (e.key === 'ArrowLeft') stepViewer(-1);
    if (e.key === 'i' || e.key === 'I') toggleViewerInfo();
    if (e.key === 'f' || e.key === 'F') $('v-fav').click();
  });
}

function wireLibraryView() {
  for (const b of document.querySelectorAll('#scopes .scope')) {
    b.addEventListener('click', () => {
      if (b.dataset.scope !== 'deleted' && L.scope === 'deleted') L.collection = L.collection;  // keep
      setScope(b.dataset.scope);
    });
  }
  $('btn-add-folder').addEventListener('click', addFolder);
  $('btn-tags-clear').addEventListener('click', () => { L.tags.clear(); renderTags(L.overview?.tags || []); reloadItems(); });
  $('kind').addEventListener('gm-change', (e) => { L.kind = e.detail.value; if (L.kind !== 'audio') leaveMusicView(); syncMusicControls(); reloadItems(); });
  let typing;
  $('lib-q').addEventListener('input', (e) => {
    if ($('view-library').hidden) setView(L.space);
    clearTimeout(typing);
    typing = setTimeout(() => { L.q = e.target.value.trim(); reloadItems(); }, 250);
  });
  $('lib-sort').addEventListener('change', (e) => { L.sort = e.target.value; reloadItems(); });
  $('btn-lib-refresh').addEventListener('click', () => refreshLibrary(true));
  $('lib-empty-web').addEventListener('click', () => call('music_search_open', { q: L.q })
    .then((r) => { if (!r.ok) toast(tr(r.error)); }).catch((e) => toast(tr(String(e)))));
  $('btn-lib-more').addEventListener('click', () => loadMoreItems());
  autoMore.observe($('btn-lib-more'));
  $('view-library').addEventListener('scroll', () => scheduleWindow(), { passive: true });
  new ResizeObserver((entries) => {  // the width sets the columns; the height moves with every slice
    const w = entries[0].contentRect.width;
    if (w !== VG.width) { VG.width = w; scheduleWindow(true); }
  }).observe($('lib-grid'));

  $('btn-select').addEventListener('click', enterSelecting);
  // the bar can take one row or two: the list header sticks under it
  new ResizeObserver(() => {
    $('lib-grid').style.setProperty('--selbar-h', `${$('selbar').offsetHeight + 6}px`);
  }).observe($('selbar'));
  $('sel-done').addEventListener('click', exitSelecting);
  $('sel-more').addEventListener('click', () => $('sel-menu').open());  // the rarer actions
  $('sel-all').addEventListener('click', () => { L.items.forEach((i) => L.selected.add(i.id)); L.items.forEach((i) => markTile(i.id)); updateSelbar(); });
  $('sel-fav').addEventListener('click', () => setFavorite([...L.selected], true));
  $('sel-unfav').addEventListener('click', () => setFavorite([...L.selected], false));
  $('sel-tag').addEventListener('click', () => openTagDialog([...L.selected]));
  $('sel-delete').addEventListener('click', () => deleteItems([...L.selected]));
  document.addEventListener('keydown', (e) => {
    if ($('view-library').hidden || e.target.closest('input, textarea, gm-dialog')) return;
    if (e.key === 'Escape' && L.selecting) exitSelecting();
    if (e.key === 'Delete' && L.selecting && L.selected.size) deleteItems([...L.selected]);
    if ((e.key === 'a' || e.key === 'A') && e.ctrlKey && L.scope !== 'deleted') {
      e.preventDefault();
      if (!L.selecting) enterSelecting();
      $('sel-all').click();
    }
  });
  wireViewer();
}

// ------------------------------------------------- download folder change
// A new folder can take the downloads along (moved by the engine, verified),
// and the previous folder is remembered so the change can be undone.
let savedOutDir = '';

function setLastMove(lm) {
  $('previous-dir').hidden = !lm;
  if (!lm) return;
  $('previous-dir-text').textContent = tr('Antes: {from}', { from: lm.from }) + (lm.profiles ? tn(' · {n} carpeta movida', ' · {n} carpetas movidas', lm.profiles) : '') + '.';
  $('btn-undo-move').title = lm.profiles ? tr('Devuelve las descargas a la carpeta anterior y vuelve a usarla')
    : tr('Vuelve a usar la carpeta anterior');
}

function applyOutDir(dir, lastMove) {
  savedOutDir = dir;
  $('out-dir').value = dir;
  setLastMove(lastMove);
  libraryChanged();
}

// Files changed on disk (a download, a move): re-read now if visible, else when it shows.
// While a long list downloads, songs land every few seconds: at most one re-read every 4 s,
// and one more after the last.
const LIB_REFRESH_GAP = 4000;
let libRefreshAt = 0;
let libRefreshTimer = 0;
function libraryChanged() {
  L.dirty = true;
  if ($('view-library').hidden || libRefreshTimer) return;
  const wait = Math.max(0, libRefreshAt + LIB_REFRESH_GAP - Date.now());
  libRefreshTimer = setTimeout(() => {
    libRefreshTimer = 0;
    libRefreshAt = Date.now();
    if (L.dirty && !$('view-library').hidden) refreshLibrary(false, { inPlace: true });
  }, wait);
}

function setMoving(on) {
  $('move-progress').hidden = !on;
  $('out-dir').disabled = on;
  $('btn-folder').disabled = on;
  $('btn-undo-move').disabled = on;
  if (on) { $('move-status').textContent = tr('Preparando…'); setMeterOf($('move-meter'), 0, null); }
}

function setMeterOf(m, done, max) {
  if (!max) { m.setAttribute('aria-busy', 'true'); return; }
  m.removeAttribute('aria-busy');
  const v = Math.max(0, Math.min(1, done / max));
  m.style.setProperty('--value', v.toFixed(4));
  m.setAttribute('aria-valuenow', Math.round(v * 100));
}

const fmtSize = (b) => (b >= 1073741824 ? `${(b / 1073741824).toFixed(1)} GB` : `${(b / 1048576).toFixed(0)} MB`);

function askMove(res) {
  const n = res.profiles.length;
  $('move-summary').textContent = tn('En la carpeta actual hay {n} carpeta de Bakin ', 'En la carpeta actual hay {n} carpetas de Bakin ', n)
    + tr('({files} archivos, {bytes}). ¿Los llevas a la carpeta nueva?', { files: res.files, bytes: fmtSize(res.bytes) });
  $('move-from').textContent = res.from;
  $('move-to').textContent = res.to;
  const dlg = $('move-dialog');
  return new Promise((resolve) => {
    dlg.addEventListener('gm-close', (e) => resolve(e.detail?.value || 'cancel'), { once: true });
    dlg.show();
  });
}

async function changeOutDir(path, move = null) {
  path = String(path || '').trim();
  if (!path || path === savedOutDir) { $('out-dir').value = savedOutDir; return; }
  try {
    let res = await call('set_out_dir', { path, move });
    if (res.ask) {
      const choice = await askMove(res);
      if (choice === 'cancel') { $('out-dir').value = savedOutDir; return; }
      res = await call('set_out_dir', { path, move: choice === 'move' });
    }
    if (!res.ok) { toast(tr(res.error), { duration: 6000 }); $('out-dir').value = savedOutDir; return; }
    if (res.moving) { setMoving(true); return; }   // "move_done" finishes it
    if (res.changed !== false) {
      applyOutDir(res.out_dir, res.last_move);
      log('info', tr('Carpeta de descargas: {out_dir}', { out_dir: res.out_dir }));
    }
  } catch (err) {
    toast(tr(String(err)));
    $('out-dir').value = savedOutDir;
  }
}

async function undoMove() {
  try {
    const res = await call('undo_move');
    if (!res.ok) { toast(tr(res.error), { duration: 6000 }); return; }
    if (res.moving) { setMoving(true); return; }
    applyOutDir(res.out_dir, res.last_move);
    toast(tr('Volviste a la carpeta anterior.'));
  } catch (err) { toast(tr(String(err))); }
}

function onMoveEvent(ev) {
  if (ev.kind === 'move') {
    setMoving(true);
    setMeterOf($('move-meter'), ev.done, ev.total || null);
    const pct = ev.total ? ` · ${Math.round((100 * ev.done) / ev.total)} %` : '';
    $('move-status').textContent = (ev.undo ? tr('Devolviendo {folder} ({index} de {count})', { folder: ev.folder, index: ev.index, count: ev.count })
      : tr('Moviendo {folder} ({index} de {count})', { folder: ev.folder, index: ev.index, count: ev.count })) + pct;
    return;
  }
  setMoving(false);
  applyOutDir(ev.out_dir, ev.last_move);
  log(ev.ok ? 'success' : 'warning', tr(ev.message));
  const undoable = !ev.undo && ev.moved.length;
  toast(tr(ev.message), { duration: 9000, ...(undoable ? { action: { label: tr('Deshacer'), onClick: undoMove } } : {}) });
}

function wireFolder() {
  $('btn-undo-move').addEventListener('click', undoMove);
}

// ------------------------------------------- version, news and updates
// Releases come from the public repo BladeHoly/bakin, signed; the
// shell checks the signature (updater.rs). Never installs during a download.
let appVersion = '';
let pendingUpdate = null;

const semver = (v) => String(v).replace(/^v/, '').split('.').map((n) => parseInt(n, 10) || 0);
const newer = (a, b) => { const x = semver(a), y = semver(b); for (let i = 0; i < 3; i++) { if (x[i] !== y[i]) return x[i] > y[i]; } return false; };

// Same format as CHANGELOG.md ("## 0.2.0 — date" + "- item" lines).
function parseChangelog(md) {
  const out = [];
  for (const line of String(md || '').split(/\r?\n/)) {
    const h = /^##\s+v?(\d+\.\d+\.\d+)\s*(?:[—-]\s*(\S+))?/.exec(line.trim());
    if (h) out.push({ version: h[1], date: h[2] || '', items: [] });
    else if (out.length && line.startsWith('- ')) out[out.length - 1].items.push(line.slice(2).trim());
  }
  return out;
}

function renderChangelog(box, releases) {
  box.replaceChildren();
  for (const r of releases) {
    const sec = document.createElement('section');
    const h = document.createElement('h3');
    h.textContent = r.date ? `${r.version} · ${r.date}` : r.version;
    const ul = document.createElement('ul');
    for (const item of r.items) {
      const li = document.createElement('li');
      li.textContent = item;
      ul.append(li);
    }
    sec.append(h, ul);
    box.append(sec);
  }
}

async function openNews() {
  let releases = [];
  try { releases = await (await fetch('./changelog.json')).json(); } catch { /* shown empty */ }
  $('news-meta').textContent = tr('Tienes Bakin {appVersion}.', { appVersion });
  renderChangelog($('news-list'), releases);
  $('news-dialog').show();
}

function showUpdate(info) {
  pendingUpdate = info;
  $('update-pill').hidden = false;
  $('btn-update').textContent = tr('Actualizar a {version}', { version: info.version });
}

function openUpdate() {
  if (!pendingUpdate) return;
  const changes = parseChangelog(pendingUpdate.notes).filter((r) => newer(r.version, appVersion));
  $('update-lead').textContent = tr('Bakin {version} está disponible (tienes {appVersion}). Esto cambia:', { version: pendingUpdate.version, appVersion });
  renderChangelog($('update-list'), changes.length ? changes : [{ version: pendingUpdate.version, items: [tr('Mejoras y correcciones.')] }]);
  $('update-progress').hidden = true;
  $('btn-update-now').disabled = false;
  $('btn-update-later').disabled = false;
  $('update-dialog').show();
}

async function checkUpdates({ manual = false } = {}) {
  const btn = $('btn-check-update');
  if (manual) btn.setAttribute('aria-busy', 'true');
  try {
    const info = await tauri.core.invoke('check_update');
    if (!info) { if (manual) toast(tr('Bakin {appVersion} es la última versión.', { appVersion })); return; }
    showUpdate(info);
    if (manual) { $('news-dialog').close(); openUpdate(); } else {
      toast(tr('Bakin {version} está disponible.', { version: info.version }), { duration: 9000, action: { label: tr('Ver qué cambia'), onClick: openUpdate } });
    }
  } catch (err) {
    if (manual) toast(tr(String(err)), { duration: 6000 });
    else log('muted', tr('No se pudo buscar actualizaciones ({err}).', { err }));
  } finally {
    btn.removeAttribute('aria-busy');
  }
}

async function installUpdate() {
  // Only a download in progress blocks it: the queue is saved and comes back after the update.
  if (running) { toast(tr('Hay una descarga en curso: actualiza cuando termine o pausa la cola.'), { duration: 6000 }); return; }
  $('btn-update-now').disabled = true;
  $('btn-update-later').disabled = true;
  $('update-progress').hidden = false;
  $('update-status').textContent = tr('Descargando la actualización…');
  setMeterOf($('update-meter'), 0, null);
  const stop = await tauri.event.listen('update-progress', (e) => {
    const { downloaded, total } = e.payload;
    setMeterOf($('update-meter'), downloaded, total || null);
    $('update-status').textContent = (total ? tr('Descargando… {done} de {total}', { done: fmtSize(downloaded), total: fmtSize(total) }) : tr('Descargando… {done}', { done: fmtSize(downloaded) }));
  });
  try {
    await tauri.core.invoke('install_update');   // on success Bakin restarts
  } catch (err) {
    $('update-status').textContent = String(err);
    $('btn-update-now').disabled = false;
    $('btn-update-later').disabled = false;
  } finally {
    stop();
  }
}

async function wireVersion() {
  try { appVersion = await tauri.app.getVersion(); } catch { appVersion = '?'; }
  $('btn-version').textContent = `v${appVersion}`;
  $('btn-version').addEventListener('click', openNews);
  $('btn-news-close').addEventListener('click', () => $('news-dialog').close());
  $('btn-check-update').addEventListener('click', () => checkUpdates({ manual: true }));
  $('btn-update').addEventListener('click', openUpdate);
  $('btn-update-later').addEventListener('click', () => $('update-dialog').close());
  $('btn-update-now').addEventListener('click', installUpdate);
  // First start after an update: say so, once.
  const seen = store.get('bakin.seenVersion', '');
  if (seen && seen !== appVersion && newer(appVersion, seen)) {
    toast(tr('Bakin se actualizó a {appVersion}.', { appVersion }), { duration: 9000, action: { label: tr('Novedades'), onClick: openNews } });
  }
  store.set('bakin.seenVersion', appVersion);
  setTimeout(() => checkUpdates(), 4000);
  setInterval(() => checkUpdates(), 6 * 60 * 60 * 1000);   // long sessions too
}

// ------------------------------------------------- music & video (yt-dlp)
// Any link yt-dlp supports. Presets hold the defaults (the old script's
// wizard); the download dialog lets each download pick another format.
let MEDIA = null;          // settings from the engine (presets, tools...)
const presetById = (id) => MEDIA?.presets.find((p) => p.id === id);
const formatLabel = (p) => (p.mode === 'video'
  ? `Video${p.video_max_height ? ` ≤${p.video_max_height}p` : ''}`
  : (p.audio_format === 'best' ? tr('audio original') : p.audio_format.toUpperCase()));
const setSel = (sel, v) => { sel.value = String(v); sel.closest('gm-select')?.sync?.(); };
const TOOL_LABEL = { 'yt-dlp': 'yt-dlp', ffmpeg: 'ffmpeg + ffprobe', deno: tr('Deno (resuelve los desafíos de YouTube)') };

// Whether the presets' cookies still log in (Descargas > Música y video).
async function renderCookiesHealth() {
  const el = $('cookies-health');
  let res;
  try { res = await call('cookies_status'); } catch { el.hidden = true; return; }
  const name = (f) => f.split(/[\\/]/).pop();
  const lines = res.cookies.map((c) => {
    const who = c.presets.join(', ');
    if (c.browser) {
      // Chromium browsers encrypt their cookies for themselves on Windows (app-bound): yt-dlp can't read them.
      const locked = ['brave', 'chrome', 'chromium', 'edge', 'opera', 'vivaldi', 'whale'].includes(c.browser);
      return locked
        ? { warn: true, who, text: tr('{who}: usan cookies de {x}, que Windows no deja leer. Si un video pide tu cuenta va a fallar: cambia el preset a cookies.txt.', { who, x: c.browser[0].toUpperCase() + c.browser.slice(1) }) }
        : { warn: false, who, text: tr('Cookies de {browser}.', { browser: c.browser }) };
    }
    if (c.vault) {
      if (!c.exists) return { warn: true, who, text: tr('{who}: no hay cookies guardadas en Bakin. Impórtalas en Configuración › Cuentas.', { who }) };
      if (c.bad || c.expired) return { warn: true, who, text: tr('Las cookies guardadas en Bakin dejaron de servir: exporta un cookies.txt nuevo e impórtalo en Configuración › Cuentas.') };
      const until = c.expires ? tr(', válidas hasta el {x}', { x: new Date(c.expires * 1000).toLocaleDateString(LOCALE) }) : '';
      return { warn: false, who, text: tr('Cookies de YouTube: guardadas en Bakin, cifradas{until}.', { until }) };
    }
    if (!c.exists) return { warn: true, who, text: tr('No se encuentra {file}. Exporta un cookies.txt nuevo o cambia el archivo en el preset.', { file: c.file }) };
    if (c.bad) return { warn: true, who, text: tr('Las cookies de {file} dejaron de servir (YouTube las renovó). Exporta un cookies.txt nuevo desde una ventana privada y reemplaza el archivo: Bakin lo nota solo.', { file: name(c.file) }) };
    if (!c.youtube_session) return { warn: true, who, text: tr('{file} no tiene una sesión de YouTube: expórtalo con la sesión iniciada en music.youtube.com.', { file: name(c.file) }) };
    if (c.expired) return { warn: true, who, text: tr('Las cookies de {file} vencieron: exporta un cookies.txt nuevo.', { file: name(c.file) }) };
    const until = c.expires ? tr(', válidas hasta el {x}', { x: new Date(c.expires * 1000).toLocaleDateString(LOCALE) }) : '';
    return { warn: false, who, text: tr('Cookies de YouTube: {file}{until}.', { file: name(c.file), until }) };
  });
  el.hidden = !lines.length;
  el.replaceChildren(...lines.map((l) => {
    const line = document.createElement('span');
    line.className = l.warn ? 'warn' : '';
    line.textContent = l.text;
    line.title = tr('Lo usan: {who}', { who: l.who });
    return line;
  }));
}

// Configuración › Cuentas › YouTube (2026-11-07): the cookies.txt into Bakin's vault, encrypted
// for this Windows user; then the plain file can go.
// A yes/no question in Bakin's own dialog: resolves true on the main button.
function ask(heading, text, ok = tr('Aceptar')) {
  const dlg = $('ask-dialog');
  dlg.setAttribute('heading', heading);
  $('ask-text').textContent = text;
  $('ask-ok').textContent = ok;
  return new Promise((resolve) => {
    dlg.addEventListener('gm-close', (e) => resolve(e.detail?.value === 'ok'), { once: true });
    dlg.show();
  });
}

async function renderCookiesVault() {
  let r;
  try { r = await call('cookies_vault'); } catch (err) { $('cv-status').textContent = String(err); return; }
  const v = r.vault;
  const plain = r.files.filter((f) => f.exists);
  const until = v.expires ? tr(' hasta el {x}', { x: new Date(v.expires * 1000).toLocaleDateString(LOCALE) }) : '';
  $('cv-status').textContent = v.unreadable ? tr('No se pudieron leer las cookies guardadas (¿otro usuario de Windows?). Impórtalas de nuevo.')
    : v.exists ? (v.youtube_session && !v.expired ? tr('Guardadas en Bakin, cifradas · sesión de YouTube válida{until}.', { until }) : tr('Guardadas en Bakin, pero la sesión de YouTube venció: importa unas nuevas.'))
      : plain.length ? tr('Tus presets usan {x}, en texto plano: pásalo a Bakin y bórralo.', { x: plain.map((f) => f.file).join(', ') })
        : tr('No hay cookies guardadas.');
  $('cv-import').hidden = plain.length !== 1;
  $('cv-delete-file').hidden = !(v.exists && r.imported_from);
  if (r.imported_from) $('cv-delete-file').title = r.imported_from;
  $('cv-forget').hidden = !v.exists;
}

async function importCookies(path = '') {
  const r = await call('cookies_import', { path }).catch((e) => ({ ok: false, error: String(e) }));
  if (!r.ok) { toast(tr(r.error), { duration: 7000 }); return; }
  toast(r.presets.length ? tr('Cookies guardadas en Bakin (las usa: {presets}). Ya puedes borrar el archivo.', { presets: r.presets.join(', ') }) : tr('Cookies guardadas en Bakin. Ya puedes borrar el archivo.'), { duration: 7000 });
  await loadMedia();
  renderCookiesVault();
}

function wireCookiesVault() {
  $('cv-import').addEventListener('click', () => importCookies());
  $('cv-pick').addEventListener('click', async () => {
    const f = await tauri.dialog.open({ title: tr('Elegir el cookies.txt'), filters: [{ name: 'cookies.txt', extensions: ['txt'] }] });
    if (typeof f === 'string') importCookies(f);
  });
  $('cv-delete-file').addEventListener('click', async () => {
    if (!(await ask(tr('¿Borrar el archivo de cookies?'), tr('{file}: se sobrescribe antes de borrarlo (no va a la Papelera). Las cookies siguen guardadas en Bakin.', { file: $('cv-delete-file').title }), tr('Borrar')))) return;
    const r = await call('cookies_delete_original').catch((e) => ({ ok: false, error: String(e) }));
    toast(r.ok ? tr('Archivo de cookies borrado.') : tr(r.error), { duration: 6000 });
    renderCookiesVault();
  });
  $('cv-forget').addEventListener('click', async () => {
    if (!(await ask(tr('¿Quitar las cookies de Bakin?'), tr('Los presets que las usan quedan sin cookies hasta que importes otras.'), tr('Quitar')))) return;
    await call('cookies_forget');
    toast(tr('Cookies quitadas de Bakin.'));
    await loadMedia();
    renderCookiesVault();
  });
}

async function loadMedia() {
  try {
    MEDIA = await call('media_settings');
    renderCookiesHealth();
  } catch (err) { log('danger', tr('Música y video: {err}', { err })); return; }
  fillPresetSelect($('media-preset'), MEDIA.default_preset);
  $('media-limit').value = MEDIA.daily_limit || 0;
  $('hours-on').checked = !!MEDIA.hours?.enabled;
  setSel($('hours-from'), MEDIA.hours?.from ?? 1);
  setSel($('hours-to'), MEDIA.hours?.to ?? 8);
  $('hours-row').hidden = !$('hours-on').checked;
  renderToolsBadge();
}

function fillPresetSelect(sel, value) {
  sel.replaceChildren(...MEDIA.presets.map((p) => new Option(`${p.name} · ${formatLabel(p)}`, p.id)));
  setSel(sel, value);
}

function renderToolsBadge() {
  const missing = Object.entries(MEDIA.tools).filter(([, t]) => !t.installed).map(([n]) => n);
  const b = $('tools-badge');
  b.hidden = !missing.length;
  b.dataset.tone = 'warning';
  b.textContent = missing.length ? `Falta ${missing.join(', ')}` : '';
}

// ------------------------------------------------------- download dialog
let mdUrls = [];
let mdMode, mdFormat, mdQuality, mdHeight;

// The settings load at start-up; if a dialog is opened before they arrive, wait for them.
async function ensureMedia() {
  if (!MEDIA) await loadMedia();
  if (!MEDIA) toast(tr('No se pudo cargar la configuración de música y video (ver Actividad).'), { duration: 6000 });
  return !!MEDIA;
}

async function openMediaDialog(urls) {
  if (!(await ensureMedia())) return;
  mdUrls = urls;
  const ul = $('md-urls');
  ul.replaceChildren(...urls.slice(0, 8).map((u) => { const li = document.createElement('li'); li.textContent = u; return li; }));
  if (urls.length > 8) { const li = document.createElement('li'); li.textContent = tr('… y {x} más', { x: urls.length - 8 }); ul.append(li); }
  fillPresetSelect($('md-preset'), MEDIA.default_preset);
  applyPresetToDialog(presetById(MEDIA.default_preset));
  $('media-dialog').setAttribute('heading', urls.length > 1 ? tr('Descargar {urls} enlaces', { urls: urls.length }) : tr('Descargar música o video'));
  resetAnalysis();
  $('media-dialog').show();
  if (urls.length === 1) analyzeLink(urls[0]);
}

// ------------------------------------------------- analyze before download
let MD = { info: null, seq: 0 };

function resetAnalysis() {
  MD = { info: null, seq: MD.seq + 1, picked: false };
  for (const id of ['md-info', 'md-pl', 'md-extras', 'md-analyzing', 'md-clip-row', 'md-chapters-row', 'md-subs-row', 'md-detected']) $(id).hidden = true;
  $('md-clip').checked = false;
  $('md-chapters').checked = false;
  $('md-subs').checked = false;
  $('md-from').value = '';
  $('md-to').value = '';
  $('md-pl-list').replaceChildren();
  $('md-go').disabled = false;
}

async function analyzeLink(url) {
  const seq = MD.seq;
  $('md-analyzing').hidden = false;
  let r;
  try { r = await call('media_analyze', { url, preset_id: $('md-preset').value }); } catch (e) { r = { ok: false, error: String(e) }; }
  if (seq !== MD.seq) return;            // the dialog moved on
  $('md-analyzing').hidden = true;
  if (!r.ok) {
    $('md-info').hidden = false;
    $('md-thumb').hidden = true;
    $('md-title').textContent = tr('No se pudo analizar');
    $('md-meta').textContent = tr('{error} Igual puedes descargarlo.', { error: tr(r.error) });
    return;
  }
  MD.info = r;
  $('md-info').hidden = false;
  $('md-title').textContent = r.title || '';
  showDetected(r);
  if (r.type === 'playlist') {
    $('md-thumb').hidden = true;
    const have = r.entries.filter((e) => e.have).length;
    $('md-meta').textContent = (r.uploader ? tr('Playlist de {uploader} · {count} temas', { uploader: r.uploader, count: r.count }) : tr('Playlist · {count} temas', { count: r.count }))
      + (have ? tr(' · {have} ya los tienes', { have }) : '');
    renderPlaylist(r.entries);
  } else {
    $('md-thumb').hidden = !r.thumbnail;
    if (r.thumbnail) $('md-thumb').src = r.thumbnail;
    const bits = [r.uploader, fmtDur(r.duration), r.max_height ? tr('hasta {h}p', { h: r.max_height }) : '', r.audio,
      r.age_limit >= 18 ? tr('+18 (usa cookies)') : '', r.is_live ? tr('en vivo') : ''];
    $('md-meta').textContent = bits.filter(Boolean).join(' · ');
    $('md-extras').hidden = false;
    $('md-chapters-row').hidden = !r.chapters.length;
    $('md-chapters-n').textContent = tr('{chapters} capítulos: {chapters2}{x}', { chapters: r.chapters.length, chapters2: r.chapters.slice(0, 3).map((c) => c.title).join(', '), x: r.chapters.length > 3 ? '…' : '' });
    const langs = r.subtitles.length ? r.subtitles.slice(0, 8).join(', ') : (r.auto_subtitles ? tr('automáticos') : '');
    $('md-subs-row').hidden = !(r.subtitles.length || r.auto_subtitles);
    $('md-subs-n').textContent = tr('Disponibles: {langs}. Se incrustan español e inglés si hay (solo en modo video).', { langs });
  }
}

// Music or video, told by itself (a friend's request): the preset for what was
// detected, or the one the user chose for that site, unless they already picked one.
function showDetected(r) {
  const d = r.detected || {};
  const s = r.suggested || {};
  if (!d.kind || (d.kind === 'unknown' && !s.preset)) return;
  const p = s.preset && presetById(s.preset);
  if (p && !MD.picked && $('md-preset').value !== p.id) {
    setSel($('md-preset'), p.id);
    applyPresetToDialog(p);
  }
  const badge = $('md-detected-kind');
  badge.textContent = { audio: tr('Música'), video: 'Video', unknown: tr('Lista') }[d.kind];
  badge.dataset.tone = d.kind === 'audio' ? 'success' : 'info';
  $('md-detected-why').textContent = [s.why || d.why, p ? tr('se baja con «{name}»', { name: p.name }) : ''].filter(Boolean).join(' · ');
  $('md-detected').hidden = false;
}

// The user changed the preset of a single analysed link: that site starts with it from now on.
async function rememberSitePreset(presetId) {
  const url = MD.info?.url || (mdUrls.length === 1 ? mdUrls[0] : '');
  if (!url || !presetId) return;
  const res = await call('media_set_site_pref', { url, preset_id: presetId }).catch(() => null);
  if (!res?.ok) return;
  MEDIA.site_prefs = { ...(MEDIA.site_prefs || {}), [res.site]: presetId };
  const p = presetById(presetId);
  $('md-detected-why').textContent = tr('Listo: los enlaces de {site} van a empezar con «{x}».', { site: res.site, x: p ? p.name : presetId });
  $('md-detected').hidden = false;
}

function renderPlaylist(entries) {
  const ul = $('md-pl-list');
  ul.replaceChildren(...entries.map((e) => {
    const li = document.createElement('li');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'gm-check';
    cb.checked = true;
    cb.dataset.index = e.index;
    cb.dataset.have = e.have ? '1' : '';
    cb.addEventListener('change', updatePlCount);
    const n = document.createElement('span');
    n.className = 'md-pl-n';
    n.textContent = e.index;
    const art = document.createElement('span');
    art.className = 'md-pl-art';
    if (e.thumb) {
      const img = new Image();
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.onerror = () => img.remove();
      img.src = e.thumb;
      art.append(img);
    }
    const t = document.createElement('span');
    t.className = 'md-pl-t';
    t.textContent = e.title || e.id;
    const d = document.createElement('small');
    d.textContent = (e.have ? tr('ya lo tienes · ') : '') + fmtDur(e.duration);
    li.append(cb, n, art, t, d);
    li.addEventListener('click', (ev) => { if (ev.target !== cb) { cb.checked = !cb.checked; updatePlCount(); } });
    return li;
  }));
  $('md-pl').hidden = false;
  updatePlCount();
}

function plChecks() { return [...$('md-pl-list').querySelectorAll('input[type=checkbox]')]; }

function updatePlCount() {
  const all = plChecks();
  const on = all.filter((c) => c.checked).length;
  $('md-pl-count').textContent = tr('{on} de {all} seleccionados', { on, all: all.length });
  $('md-go').disabled = !on;
}

// [1,2,3,7,9,10] -> "1:3,7,9:10" (yt-dlp --playlist-items ranges)
function itemSpec(indexes) {
  const out = [];
  for (let i = 0; i < indexes.length; i++) {
    let j = i;
    while (j + 1 < indexes.length && indexes[j + 1] === indexes[j] + 1) j++;
    out.push(j > i ? `${indexes[i]}:${indexes[j]}` : `${indexes[i]}`);
    i = j;
  }
  return out.join(',');
}

function applyPresetToDialog(p) {
  mdMode.set(p.mode);
  mdFormat.set(p.audio_format);
  mdQuality.set(String(p.audio_quality));
  mdHeight.set(String(p.video_max_height || 0));
  syncMediaDialog();
}

function syncMediaDialog() {
  const video = mdMode.value === 'video';
  $('md-audio').hidden = video;
  $('md-video').hidden = !video;
  $('md-quality-wrap').hidden = ['flac', 'wav', 'best'].includes(mdFormat.value);
  const p = presetById($('md-preset').value);
  if (!p) return;
  const where = /^[A-Za-z]:[\\/]|^\\\\/.test(p.folder) ? p.folder : `${MEDIA.root}\\${p.folder}`;
  const from = { file: 'cookies.txt', vault: tr('Bakin (cifradas)') }[p.cookies_source] || p.cookies_browser;
  const cookies = { off: tr('sin cookies'), auto: tr('cookies de {from} solo si el sitio las pide', { from }), always: tr('con cookies de {from}', { from }) }[p.cookies_mode];
  $('md-summary').textContent = tr('Se guarda en {where} · {cookies}', { where, cookies }) + (p.sponsorblock ? ` · SponsorBlock: ${p.sponsorblock}` : '')
    + (p.archive ? tr(' · lo ya bajado se saltea') : '') + '.';
}

async function submitMediaDialog() {
  const preset = $('md-preset').value;
  const override = mdMode.value === 'video'
    ? { mode: 'video', video_max_height: Number(mdHeight.value) || 0 }
    : { mode: 'audio', audio_format: mdFormat.value, audio_quality: mdQuality.value };
  if (MD.info?.type === 'playlist') {
    const all = plChecks();
    const on = all.filter((c) => c.checked).map((c) => Number(c.dataset.index)).sort((a, b) => a - b);
    if (on.length < all.length) override.playlist_items = itemSpec(on);
  } else if (MD.info?.type === 'video') {
    if ($('md-clip').checked) {
      if (!$('md-from').value.trim() && !$('md-to').value.trim()) { toast(tr('Indica desde y/o hasta dónde.')); return; }
      override.section = `${$('md-from').value.trim() || '0'}-${$('md-to').value.trim()}`;
    }
    if ($('md-chapters').checked) override.split_chapters = true;
    if ($('md-subs').checked && mdMode.value === 'video') override.subtitles = 'es.*,en.*';
  }
  $('media-dialog').close();
  if (mdUrls.length === 1) await startJob('download', mdUrls[0], { preset, override });
  else await startMany('download', mdUrls.map((u) => [u, { preset, override }]));
}

// ----------------------------------------------------------- preset editor
let psCurrent = null;

async function openPresets(id) {
  if (!(await ensureMedia())) return;
  fillPresetSelect($('ps-pick'), id || MEDIA.default_preset);
  const cb = $('ps-cbrowser');
  if (!cb.options.length) cb.replaceChildren(...MEDIA.browsers.map((b) => new Option(b[0].toUpperCase() + b.slice(1), b)));
  loadPresetForm(presetById($('ps-pick').value));
  $('preset-dialog').show();
}

function loadPresetForm(p) {
  psCurrent = { ...p };
  const note = $('ps-builtin-note');
  note.hidden = !p.builtin;
  note.textContent = p.builtin ? tr('Preset de Bakin. {x} Puedes cambiarlo: tus cambios se conservan.', { x: p.description || '' }) : '';
  $('ps-name').value = p.name;
  $('ps-desc').value = p.description || '';
  $('ps-folder').value = p.folder;
  setSel($('ps-mode'), p.mode);
  setSel($('ps-format'), p.audio_format);
  setSel($('ps-quality'), String(p.audio_quality));
  setSel($('ps-height'), String(p.video_max_height || 0));
  const tKey = Object.entries(MEDIA.templates).find(([, v]) => v === p.template)?.[0] || 'custom';
  setSel($('ps-template'), tKey);
  $('ps-template-custom').value = p.template;
  const sbKey = Object.entries(MEDIA.sponsorblock).find(([, v]) => v === p.sponsorblock)?.[0] || 'custom';
  setSel($('ps-sb'), sbKey);
  $('ps-sb-custom').value = p.sponsorblock;
  $('ps-smin').value = p.sleep_min;
  $('ps-smax').value = p.sleep_max;
  $('ps-pdelay').value = p.playlist_delay;
  $('ps-rate').value = p.rate_limit;
  $('ps-items').value = p.playlist_items;
  $('ps-archive').checked = p.archive;
  $('ps-thumb').checked = p.embed_thumbnail;
  $('ps-meta').checked = p.embed_metadata;
  setSel($('ps-cmode'), p.cookies_mode);
  setSel($('ps-csource'), p.cookies_source);
  setSel($('ps-cbrowser'), p.cookies_browser);
  $('ps-cprofile').value = p.cookies_profile;
  $('ps-cfile').value = p.cookies_file;
  $('ps-cookies-box').open = p.cookies_mode !== 'off';
  syncPresetForm();
}

function syncPresetForm() {
  const audio = $('ps-mode').value === 'audio';
  $('ps-format').closest('.gm-field').hidden = !audio;
  $('ps-quality').closest('.gm-field').hidden = !audio || ['flac', 'wav', 'best'].includes($('ps-format').value);
  $('ps-height').closest('.gm-field').hidden = audio;
  $('ps-template-custom').hidden = $('ps-template').value !== 'custom';
  $('ps-sb-custom').hidden = $('ps-sb').value !== 'custom';
  const source = $('ps-csource').value;
  $('ps-cfile-wrap').hidden = source !== 'file';
  $('ps-cbrowser-wrap').hidden = source !== 'browser';
  $('ps-cprofile-wrap').hidden = source !== 'browser';
}

function readPresetForm() {
  const t = $('ps-template').value;
  const sb = $('ps-sb').value;
  return {
    ...psCurrent,
    name: $('ps-name').value.trim() || 'Preset',
    description: $('ps-desc').value.trim(),
    folder: $('ps-folder').value.trim(),
    mode: $('ps-mode').value,
    audio_format: $('ps-format').value,
    audio_quality: $('ps-quality').value,
    video_max_height: Number($('ps-height').value) || 0,
    template: t === 'custom' ? $('ps-template-custom').value.trim() : MEDIA.templates[t],
    sponsorblock: sb === 'custom' ? $('ps-sb-custom').value.trim() : MEDIA.sponsorblock[sb],
    sleep_min: Number($('ps-smin').value) || 0,
    sleep_max: Number($('ps-smax').value) || 0,
    playlist_delay: Number($('ps-pdelay').value) || 0,
    rate_limit: $('ps-rate').value.trim(),
    playlist_items: $('ps-items').value.trim(),
    archive: $('ps-archive').checked,
    embed_thumbnail: $('ps-thumb').checked,
    embed_metadata: $('ps-meta').checked,
    cookies_mode: $('ps-cmode').value,
    cookies_source: $('ps-csource').value,
    cookies_browser: $('ps-cbrowser').value,
    cookies_profile: $('ps-cprofile').value.trim(),
    cookies_file: $('ps-cfile').value.trim(),
  };
}

async function savePreset(p = readPresetForm()) {
  const res = await call('media_save_preset', { preset: p }).catch((e) => ({ ok: false, error: String(e) }));
  if (!res.ok) { toast(tr(res.error), { duration: 6000 }); return null; }
  await loadMedia();
  fillPresetSelect($('ps-pick'), res.preset.id);
  loadPresetForm(res.preset);
  return res.preset;
}

function wirePresets() {
  $('btn-presets').addEventListener('click', () => openPresets());
  $('ps-pick').addEventListener('change', (e) => loadPresetForm(presetById(e.target.value)));
  for (const id of ['ps-mode', 'ps-format', 'ps-template', 'ps-sb', 'ps-csource']) $(id).addEventListener('change', syncPresetForm);
  $('ps-cmode').addEventListener('change', () => { if ($('ps-cmode').value !== 'off') $('ps-cookies-box').open = true; });
  $('ps-save').addEventListener('click', async () => { if (await savePreset()) toast(tr('Preset guardado.')); });
  $('ps-close').addEventListener('click', () => $('preset-dialog').close());
  $('ps-new').addEventListener('click', async () => {
    const base = presetById(MEDIA.default_preset);
    await savePreset({ ...base, id: '', name: tr('Preset nuevo'), description: '' });
  });
  $('ps-dup').addEventListener('click', async () => { await savePreset({ ...readPresetForm(), id: '', name: `${$('ps-name').value} (copia)` }); });
  $('ps-del').addEventListener('click', async () => {
    const res = await call('media_delete_preset', { preset_id: psCurrent.id });
    if (!res.ok) { toast(tr(res.error)); return; }
    await loadMedia();
    openPresets();
  });
  $('ps-folder-pick').addEventListener('click', async () => {
    const dir = await tauri.dialog.open({ directory: true, title: tr('Carpeta del preset'), defaultPath: MEDIA.root });
    if (typeof dir === 'string') $('ps-folder').value = dir;
  });
  $('ps-cfile-pick').addEventListener('click', async () => {
    const f = await tauri.dialog.open({ title: tr('Archivo cookies.txt'), filters: [{ name: 'cookies.txt', extensions: ['txt'] }] });
    if (typeof f === 'string') $('ps-cfile').value = f;
  });
  $('ps-import').addEventListener('click', async () => {
    const f = await tauri.dialog.open({ title: tr('Presets de YT-Download.ps1'), filters: [{ name: 'JSON', extensions: ['json'] }] });
    if (typeof f !== 'string') return;
    const res = await call('media_import_presets', { path: f });
    if (!res.ok) { toast(tr(res.error), { duration: 6000 }); return; }
    toast(tn('{n} preset importado.', '{n} presets importados.', res.imported));
    await loadMedia();
    openPresets();
  });
  $('ps-restore').addEventListener('click', async () => {
    const res = await call('media_restore_builtins');
    toast(res.restored ? tn('{n} preset de Bakin restaurado.', '{n} presets de Bakin restaurados.', res.restored) : tr('Ya tienes todos los presets de Bakin.'));
    await loadMedia();
    openPresets(psCurrent?.id);
  });
  $('ps-scan').addEventListener('click', async () => {
    const p = await savePreset();
    if (!p) return;
    const btn = $('ps-scan');
    btn.setAttribute('aria-busy', 'true');
    try {
      const r = await call('media_scan_folder', { preset_id: p.id });
      if (!r.ok) { toast(tr(r.error)); return; }
      const msg = tr('{files} archivos de audio: {registered} registrados ahora, {already} ya estaban', { files: r.files, registered: r.registered, already: r.already })
        + (r.no_id.length ? tr(', {no_id} sin ID (no se pueden registrar)', { no_id: r.no_id.length }) : '') + '.';
      toast(msg, { duration: 8000 });
      log('info', tr('Registrar lo que ya tengo ({folder}): {msg}', { folder: r.folder, msg }));
    } finally { btn.removeAttribute('aria-busy'); }
  });
  $('media-preset').addEventListener('change', async (e) => {
    await call('media_set_option', { key: 'default_preset', value: e.target.value });
    MEDIA.default_preset = e.target.value;
  });
}

// ------------------------------------------------------------------ tools
const toolProgress = {};

function renderTools() {
  if (!MEDIA) return;
  const ul = $('tools-list');
  ul.replaceChildren();
  for (const [name, t] of Object.entries(MEDIA.tools)) {
    const li = document.createElement('li');
    li.className = 'tool-row';
    const title = document.createElement('strong');
    title.textContent = TOOL_LABEL[name] || name;
    const meta = document.createElement('small');
    const p = toolProgress[name];
    meta.textContent = p ? (p.total ? tr('Descargando… {done} de {total}', { done: fmtSize(p.done), total: fmtSize(p.total) }) : tr('Descargando…'))
      : t.installed ? `${t.version || '?'} · ${t.managed ? tr('administrada por Bakin') : tr('del sistema ({path})', { path: t.path })}`
        : tr('No instalada · {size}', { size: t.size });
    const badge = document.createElement('span');
    badge.className = 'gm-badge';
    badge.dataset.tone = t.installed ? 'success' : 'warning';
    badge.textContent = t.installed ? (t.update ? tr('Hay versión nueva') : tr('Lista')) : tr('Falta');
    if (t.update) badge.dataset.tone = 'info';
    const body = document.createElement('div');
    body.className = 'tool-body';
    body.append(title, meta);
    if (p) {
      const m = document.createElement('span');
      m.className = 'gm-meter';
      m.setAttribute('role', 'meter');
      setMeterOf(m, p.done, p.total || null);
      body.append(m);
    }
    li.append(body, badge);
    ul.append(li);
  }
  const missing = Object.values(MEDIA.tools).some((t) => !t.installed);
  const updates = Object.values(MEDIA.tools).some((t) => t.update);
  $('tl-install').textContent = missing ? tr('Instalar lo que falta') : updates ? tr('Actualizar') : tr('Reinstalar todo');
  $('tl-auto').checked = MEDIA.auto_update;
  $('tl-verbose').checked = MEDIA.verbose;
}

async function openTools() {
  const btn = $('btn-tools');
  btn.setAttribute('aria-busy', 'true');
  try {
    await loadMedia();
  } finally { btn.removeAttribute('aria-busy'); }
  if (!MEDIA) { toast(tr('No se pudo cargar el estado de las herramientas (ver Actividad).'), { duration: 6000 }); return; }
  renderTools();
  $('tools-dialog').show();
}

function wireTools() {
  $('btn-tools').addEventListener('click', openTools);
  $('tl-close').addEventListener('click', () => $('tools-dialog').close());
  $('tl-install').addEventListener('click', async () => {
    const names = Object.entries(MEDIA.tools).filter(([, t]) => !t.installed || t.update).map(([n]) => n);
    const res = await call('tools_install', { names: names.length ? names : null });
    if (!res.ok) toast(tr(res.error));
    else $('tl-install').disabled = true;
  });
  $('tl-check').addEventListener('click', async () => {
    const btn = $('tl-check');
    btn.setAttribute('aria-busy', 'true');
    try {
      const res = await call('tools_check');
      MEDIA.tools = res.tools;
      for (const [n, up] of Object.entries(res.updates)) if (MEDIA.tools[n]) MEDIA.tools[n].update = !!up;
      renderTools();
      toast(Object.values(res.updates).some(Boolean) ? tr('Hay actualizaciones: toca «Actualizar».') : tr('Todo al día.'));
    } catch (err) { toast(tr(String(err))); } finally { btn.removeAttribute('aria-busy'); }
  });
  for (const [id, key] of [['tl-auto', 'auto_update'], ['tl-verbose', 'verbose']]) {
    $(id).addEventListener('change', async (e) => {
      await call('media_set_option', { key, value: e.target.checked });
      MEDIA[key] = e.target.checked;
    });
  }
}

function onToolsEvent(ev) {
  if (ev.kind === 'tools_progress') {
    toolProgress[ev.name] = { done: ev.done, total: ev.total };
  } else {
    for (const k of Object.keys(toolProgress)) delete toolProgress[k];
    $('tl-install').disabled = false;
    if (MEDIA) { MEDIA.tools = ev.tools; renderToolsBadge(); }
    const msg = ev.ok ? tr('Herramientas listas: {installed}.', { installed: ev.installed.join(', ') }) : tr('No se pudo instalar: {failed}', { failed: ev.failed.join('; ') });
    toast(msg, { duration: 7000 });
    log(ev.ok ? 'success' : 'danger', msg);
  }
  renderTools();
}

// ------------------------------------------------------- leftover .mp4s
let leftovers = null;

function onLeftovers(ev) {
  leftovers = ev;
  toast(tn('{n} video quedó en MP4 en vez de audio.', '{n} videos quedaron en MP4 en vez de audio.', ev.count), {
    duration: 10000, action: { label: tr('Ver'), onClick: openLeftovers },
  });
}

function openLeftovers() {
  if (!leftovers) return;
  $('lo-text').textContent = tn('En {folder} hay {n} archivo .mp4 que deberían ser audio:', 'En {folder} hay {n} archivos .mp4 que deberían ser audio:', leftovers.count, { folder: leftovers.folder });
  $('lo-list').replaceChildren(...leftovers.items.map((x) => { const li = document.createElement('li'); li.textContent = x.name; return li; }));
  $('leftovers-dialog').show();
}

function wireLeftovers() {
  $('lo-close').addEventListener('click', () => $('leftovers-dialog').close());
  $('lo-go').addEventListener('click', async () => {
    $('leftovers-dialog').close();
    const res = await call('media_redownload', { preset_id: leftovers.preset, ids: leftovers.items.map((x) => x.id) });
    toast(res.ok ? tr('{queued} en cola para re-descargar.', { queued: res.queued }) : tr(res.error), { duration: 6000 });
  });
}

// ------------------------------------------------------ job card (media)
let mediaCount = 0;

function onMediaEvent(ev) {
  switch (ev.kind) {
    case 'media_url':
      $('status').textContent = (ev.count > 1 ? tr('Analizando el enlace {index} de {count}…', { index: ev.index, count: ev.count }) : tr('Analizando el enlace …'));
      setMeter(0, null);
      break;
    case 'media_item': {
      const pos = ev.n_entries ? tr(' ({index} de {count})', { index: ev.playlist_index || '?', count: ev.n_entries }) : '';
      $('job-title').textContent = ev.title || tr('Descargando…');
      $('job-badge').hidden = false;
      $('job-badge').dataset.tone = 'info';
      $('job-badge').textContent = `${ev.extractor_key || 'Media'}${ev.uploader ? ' · ' + ev.uploader : ''}`;
      $('status').textContent = tr('Descargando{pos}…', { pos });
      log('', `${ev.title}${pos}`);
      break;
    }
    case 'media_progress': {
      if (ev.total) setMeter(ev.downloaded || 0, ev.total); else setMeter(0, null);
      const pct = ev.total ? `${Math.round((100 * (ev.downloaded || 0)) / ev.total)} %` : fmtSize(ev.downloaded || 0);
      const speed = ev.speed ? ` · ${fmtSize(ev.speed)}/s` : '';
      const eta = ev.eta != null && ev.status === 'downloading' ? tr(' · faltan {eta} s', { eta: ev.eta }) : '';
      $('status').textContent = ev.status === 'finished' ? tr('Descarga terminada, procesando…') : `${pct}${speed}${eta}`;
      break;
    }
    case 'media_post': {
      const steps = { ExtractAudio: tr('Convirtiendo el audio'), Merger: tr('Uniendo video y audio'), EmbedThumbnail: tr('Incrustando la miniatura'),
        Metadata: tr('Escribiendo metadatos'), SponsorBlock: tr('Buscando segmentos de SponsorBlock'), ModifyChapters: tr('Cortando segmentos'), MoveFiles: tr('Guardando') };
      $('status').textContent = `${steps[ev.step] || ev.step}…`;
      break;
    }
    case 'media_file':
      mediaCount += 1;
      log('success', `Guardado: ${ev.path}`);
      libraryChanged();  // the song shows in the library now, not when the whole list ends
      break;
    case 'media_raw':
      log('muted', tr(ev.message));
      break;
    default:
  }
}

function wireMedia() {
  mdMode = chipGroup($('md-mode'), syncMediaDialog);
  mdFormat = chipGroup($('md-format'), syncMediaDialog);
  mdQuality = chipGroup($('md-quality'), () => {});
  mdHeight = chipGroup($('md-height'), () => {});
  $('md-preset').addEventListener('change', (e) => {
    applyPresetToDialog(presetById(e.target.value));
    MD.picked = true;
    if (MD.info) rememberSitePreset(e.target.value);
  });
  $('md-cancel').addEventListener('click', () => $('media-dialog').close());
  $('md-go').addEventListener('click', submitMediaDialog);
  $('md-clip').addEventListener('change', (e) => { $('md-clip-row').hidden = !e.target.checked; });
  $('md-pl-all').addEventListener('click', () => { plChecks().forEach((c) => { c.checked = true; }); updatePlCount(); });
  $('md-pl-none').addEventListener('click', () => { plChecks().forEach((c) => { c.checked = false; }); updatePlCount(); });
  $('md-pl-new').addEventListener('click', () => { plChecks().forEach((c) => { c.checked = !c.dataset.have; }); updatePlCount(); });
  wirePresets();
  wireTools();
  wireLeftovers();
}

// ------------------------------------------------------------ music list
// With the "Audio" filter the library is a sortable list with the songs' tags.
const fmtDur = (s) => (s == null ? '' : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`);
const MUSIC_COLS = [
  ['title', tr('Título')], ['artist', tr('Artista')], ['album', tr('Álbum')], ['duration', tr('Duración')], ['bitrate', tr('Formato')],
];
const musicMode = () => L.kind === 'audio' && L.scope !== 'deleted';

function musicHeader() {
  const head = document.createElement('div');
  head.className = 'lib-row lib-row-head';
  head.setAttribute('role', 'group');
  head.setAttribute('aria-label', tr('Ordenar por'));
  head.append(document.createElement('span'));
  for (const [key, label] of MUSIC_COLS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `c-${key}`;
    const active = L.sort === key || (key === 'bitrate' && L.sort === 'bitrate_asc');
    b.textContent = (key === 'bitrate' && L.mview?.view === 'top' ? tr('Escuchado') : label) + (active ? (L.sort === 'bitrate_asc' ? ' ↑' : ' ↓') : '');
    b.setAttribute('aria-pressed', String(active));
    b.disabled = orderedView();
    b.addEventListener('click', () => {
      L.sort = key === 'bitrate' && L.sort === 'bitrate' ? 'bitrate_asc' : key;
      setSel($('lib-sort'), L.sort);
      reloadItems();
    });
    head.append(b);
  }
  return head;
}

function rowFor(it, index) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'lib-row';
  row.dataset.id = it.id;
  row.title = it.path;
  const lead = document.createElement('span');
  lead.className = 'c-lead';
  const check = document.createElement('input');
  check.type = 'checkbox';
  check.className = 'gm-check sel-mark';
  check.tabIndex = -1;
  check.setAttribute('aria-hidden', 'true');
  check.checked = L.selected.has(it.id);
  // ♥ right in the list (0.13 review: it took opening the song and the player's heart):
  // filled when it's a favourite, an outline on hover otherwise
  const heart = document.createElement('span');
  heart.className = 'fav-mark row-fav';
  heart.textContent = '♥';
  heart.dataset.on = String(!!it.fav);
  heart.title = it.fav ? tr('Quitar de favoritas') : tr('Agregar a favoritas');
  heart.setAttribute('aria-hidden', 'true');  // a mouse shortcut inside the row's button (the row menu has it too)
  heart.addEventListener('click', (e) => {
    e.preventDefault();  // not the row's click (play / select)
    e.stopPropagation();
    setFavorite([it.id], !it.fav);
  });
  lead.append(check, heart);
  const cell = (cls, text) => { const s = document.createElement('span'); s.className = cls; s.textContent = text || ''; return s; };
  const title = cell('c-title', '');
  const art = document.createElement('span');
  art.className = 'c-cover';
  // async: a fast flick inserts dozens of rows a second, and decoding their covers on the spot cost frames (0.13 review)
  if (it.thumb) { const img = new Image(); img.alt = ''; img.decoding = 'async'; img.src = thumbSrc(it.thumb); art.append(img); } else art.innerHTML = ICONS.audio;
  const text = document.createElement('span');
  text.className = 'c-text';
  const t = document.createElement('strong');
  t.textContent = it.title || it.name.replace(/\.[^.]+$/, '');
  text.append(t);
  if (it.title) { const small = document.createElement('small'); small.textContent = it.name; text.append(small); }
  title.append(art, text);
  // «Más escuchadas» counts its plays where the format goes (2026-11-06, the user)
  const counting = L.mview?.view === 'top';
  const fmt = counting ? cell('c-bitrate c-plays', `${it.plays || 0} ${it.plays === 1 ? tr('vez') : tr('veces')}`)
    : cell('c-bitrate', `${(it.codec || it.ext || '').toUpperCase()}${it.bitrate ? ` · ${it.bitrate} kbps` : ''}`);
  if (!counting && it.low_quality) { fmt.classList.add('low'); fmt.title = tr('Baja calidad: conviene volver a bajarla'); }
  if (!counting && it.lossless) fmt.classList.add('lossless');
  row.append(lead, title, cell('c-artist', it.artist), cell('c-album', it.album), cell('c-duration', fmtDur(it.duration)), fmt);
  row.setAttribute('aria-pressed', String(L.selecting && L.selected.has(it.id)));
  if (plCur()?.id === it.id) row.classList.add('playing');
  row.addEventListener('contextmenu', (e) => { e.preventDefault(); openRowMenu(it, row); });
  row.addEventListener('click', (e) => {
    if (L.selecting || e.ctrlKey || e.shiftKey) {
      if (!L.selecting) enterSelecting();
      toggleSelect(index, e.shiftKey);
    } else if (L.mview?.view === 'queue') {
      pc({ cmd: 'jump', pos: (P.queuePos || 0) + index });
    } else {
      playFromLibrary(it.id);
    }
  });
  return row;
}

const WP_PALETTE = ['660000', '990000', 'cc0000', 'cc3333', 'ea4c88', '993399', '663399', '333399', '0066cc', '0099cc', '66cccc', '77cc33', '669900', '336600', '666600', '999900', 'cccc33', 'ffff00', 'ffcc33', 'ff9900', 'ff6600', 'cc6633', '996633', '663300', '000000', '999999', 'cccccc', 'ffffff', '424153'];
const wpFiltersShown = () => L.space !== 'music' && (L.kind === '' || L.kind === 'image' || !L.kind);

function syncWallpaperFilters() {
  $('lib-wp').hidden = !wpFiltersShown();
  $('lib-color-swatch').style.background = L.color ? `#${L.color}` : '';
  $('lib-color-swatch').classList.toggle('empty', !L.color);
  $('lib-color-label').textContent = L.color ? '' : 'Color';
  $('lib-color-clear').hidden = !L.color;
  $('lib-color-btn').setAttribute('aria-pressed', String(!!L.color));
  for (const b of $('lib-color-grid').children) b.setAttribute('aria-pressed', String((b.value || '') === (L.color || '')));
}

function wireWallpaperFilters() {
  const grid = $('lib-color-grid');
  const any = document.createElement('button');
  any.type = 'button';
  any.value = '';
  any.className = 'color-any';
  any.textContent = tr('Cualquiera');
  grid.append(any);
  for (const c of WP_PALETTE) {
    const b = document.createElement('button');
    b.type = 'button';
    b.value = c;
    b.className = 'color-dot';
    b.style.background = `#${c}`;
    b.title = `#${c}`;
    b.setAttribute('aria-label', tr('Color #{c}', { c }));
    grid.append(b);
  }
  grid.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    // the colour already chosen, again: back to any colour (0.13 review: no way back)
    L.color = (b.value || '') === L.color ? '' : (b.value || '');
    $('lib-color-pop').close();
    syncWallpaperFilters();
    reloadItems();
  });
  // the × on the chip clears the colour without opening the palette
  $('lib-color-clear').addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    L.color = '';
    syncWallpaperFilters();
    reloadItems();
  });
  $('lib-orient').addEventListener('change', (e) => { L.orient = e.target.value; reloadItems(); });
  $('lib-minpx').addEventListener('change', (e) => { L.minpx = Number(e.target.value) || 0; reloadItems(); });
  syncWallpaperFilters();
}

// «Fondo · monitor N» on the photo each monitor shows (rotated or set by hand)
let wpShown = new Map();
async function refreshWallpaperShown() {
  const shown = await tauri.core.invoke('wallpaper_shown').catch(() => ({}));
  wpShown = new Map();
  for (const [n, path] of Object.entries(shown || {})) wpShown.set(path, [...(wpShown.get(path) || []), Number(n)]);
  for (const tile of $('lib-grid').querySelectorAll('.lib-tile')) markWallpaperTile(tile);
}
function markWallpaperTile(tile) {
  const ns = wpShown.get(tile.dataset.path);
  let badge = tile.querySelector('.wp-badge');
  if (!ns) { badge?.remove(); return; }
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'gm-badge wp-badge';
    badge.dataset.corner = 'top-end';  // top-start: ♥, bottom-start: its size
    tile.append(badge);
  }
  ns.sort();
  badge.textContent = tr('Fondo {ns}', { ns: ns.join(tr(' y ')) });
  badge.title = ns.length > 1 ? tr('Es el fondo de los monitores {ns}', { ns: ns.join(tr(' y ')) }) : tr('Es el fondo del monitor {x}', { x: ns[0] });
}

function syncMusicControls() {
  const music = L.kind === 'audio';
  syncWallpaperFilters();
  $('lib-lowq').hidden = !music;
  $('sel-upgrade').hidden = !music;
  $('sel-play').hidden = !music;
  $('sel-queue').hidden = !music;
  $('sel-addpl').hidden = !music;
  $('lib-lowq').setAttribute('aria-pressed', String(!!L.lowq));
}

// ------------------------------------------------------- better copies
let upFormat;

async function openUpgrade() {
  const ids = [...L.selected];
  if (!ids.length) return;
  if (!(await ensureMedia())) return;
  const items = L.items.filter((i) => ids.includes(i.id));
  const withId = items.filter((i) => i.yt_id && !i.lossless).length;
  $('up-lead').textContent = tn('{n} seleccionado', '{n} seleccionados', ids.length) + tn('; {n} tiene su ID de YouTube', '; {n} tienen su ID de YouTube', withId)
    + (withId < ids.length ? tr(' (los demás se saltean: sin ID o ya sin pérdida).') : '.');
  fillPresetSelect($('up-preset'), MEDIA.default_preset);
  upFormat.set('opus');
  $('upgrade-dialog').show();
}

async function submitUpgrade() {
  const ids = [...L.selected];
  $('upgrade-dialog').close();
  const res = await call('library_upgrade_audio', { ids, preset_id: $('up-preset').value, audio_format: upFormat.value })
    .catch((e) => ({ ok: false, error: String(e) }));
  if (!res.ok) {
    toast(tr(res.error), { duration: 6000 });
    if (res.field === 'tools') openTools();
    return;
  }
  exitSelecting();
  const parts = [tr('{queued} en cola para bajar de nuevo', { queued: res.queued })];
  if (res.already.length) parts.push(tr('{already} ya estaban en ese formato o sin pérdida', { already: res.already.length }));
  if (res.no_id.length) parts.push(tr('{n} sin ID de YouTube (ver Actividad)', { n: res.no_id.length }));
  if (res.refused?.length) { parts.push(tr('{refused} no entraron en la cola (ver Actividad)', { refused: res.refused.length })); log('warning', res.refused.join(' · ')); }
  toast(parts.join(' · ') + '.', { duration: 9000, ...(res.queued ? { action: { label: tr('Ver cola'), onClick: () => setView('downloads') } } : {}) });
  if (res.no_id.length) log('warning', tr('Sin ID de YouTube, no se pueden volver a bajar: {no_id}', { no_id: res.no_id.join(', ') }));
}

// ------------------------------------------- links from outside the window
// bakin://download?url=... (browser extension, bookmarklet) and, optionally,
// links copied to the clipboard. Both only open the download flow: nothing
// is queued without the user confirming.
const BOOKMARKLET = "javascript:location.href='bakin://download?url='+encodeURIComponent(location.href)";

function offerLink(url, how) {
  const t = classifyAny(url);
  if (!t) return false;
  if (t.kind === 'web') { openMediaDialog([t.value]); return true; }
  toast(`${how}: ${labelFor(t)}`, { duration: 12000, action: { label: tr('Descargar'), onClick: () => startJob('download', urlFor(t)) } });
  return true;
}

function handleBakinLinks(urls) {
  for (const raw of urls || []) {
    try {
      const u = new URL(raw);
      if (u.protocol !== 'bakin:') continue;
      const target = u.searchParams.get('url');
      if (target && !offerLink(target, tr('Enlace recibido'))) toast(tr('Bakin recibió un enlace que no puede descargar.'));
    } catch { /* not a URL */ }
  }
}

let lastClip = '';
function onClipboardLink(url) {
  if (url === lastClip) return;
  lastClip = url;
  const t = classifyAny(url);
  if (!t) return;
  toast(`Copiaste ${labelFor(t)}`, {
    duration: 10000,
    action: { label: tr('Descargar'), onClick: () => (t.kind === 'web' ? openMediaDialog([t.value]) : startJob('download', urlFor(t))) },
  });
  notifyIfAway(tr('Copiaste un enlace'), tr('{t}: abre Bakin para descargarlo.', { t: labelFor(t) }));
}

async function wireLinks() {
  $('btn-extension').addEventListener('click', () => { $('ext-bookmarklet').value = BOOKMARKLET; $('extension-dialog').show(); });
  $('ext-close').addEventListener('click', () => $('extension-dialog').close());
  $('ext-open').addEventListener('click', async () => {
    try { await tauri.core.invoke('open_extension_folder'); } catch (e) { toast(tr(String(e)), { duration: 6000 }); }
  });
  const copy = async (text, what) => {
    try { await navigator.clipboard.writeText(text); toast(`${what} copiado.`); } catch { toast(tr('No se pudo copiar.')); }
  };
  $('ext-copy-url').addEventListener('click', () => copy('brave://extensions', 'brave://extensions'));
  $('ext-copy-bm').addEventListener('click', () => copy(BOOKMARKLET, tr('Marcador')));
  const clipOn = store.get('bakin.clipboard', 'off') === 'on';
  $('clip-watch').checked = clipOn;
  tauri.core.invoke('set_clipboard_watch', { value: clipOn }).catch(() => {});
  $('clip-watch').addEventListener('change', (e) => {
    store.set('bakin.clipboard', e.target.checked ? 'on' : 'off');
    tauri.core.invoke('set_clipboard_watch', { value: e.target.checked }).catch(() => {});
  });
  await tauri.event.listen('bakin-links', (e) => handleBakinLinks(e.payload?.urls));
  await tauri.event.listen('clipboard-link', (e) => onClipboardLink(e.payload?.url));
  try { handleBakinLinks(await tauri.core.invoke('pending_links')); } catch { /* none */ }
}

// ---------------------------------------------------------------- follows
let fwEvery;
let fwTarget = null;
const everyLabel = (h) => ({ 24: tr('cada día'), 168: tr('cada semana') }[h] || tr('cada {h} h', { h }));
const ago = (ts) => {
  if (!ts) return tr('todavía no');
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 60 ? tr('hace {n} min', { n: m }) : m < 1440 ? tr('hace {n} h', { n: Math.round(m / 60) }) : tr('hace {n} d', { n: Math.round(m / 1440) });
};

async function loadFollows() {
  let res;
  try { res = await call('follows_list'); } catch { return; }
  const ul = $('follows');
  ul.replaceChildren();
  $('follows-empty').hidden = res.follows.length > 0;
  $('btn-follows-all').hidden = !res.follows.length;
  $('follows-count').textContent = res.follows.length ? `${res.follows.length}` : '';
  for (const f of res.follows) {
    const li = document.createElement('li');
    li.className = 'follow-item';
    const body = document.createElement('div');
    body.className = 'follow-body';
    const name = document.createElement('strong');
    name.textContent = f.title || f.url;
    name.title = f.kind === 'wallhaven' ? tr('Imágenes › Wallhaven › {x}', { x: f.job?.label || '' }) : f.url;
    const meta = document.createElement('small');
    const p = f.kind === 'media' ? presetById(f.preset) : null;
    const what = f.kind === 'wallhaven' ? tr('fondos') : (p ? p.name : 'preset');
    meta.textContent = tr('{what} · {every_hours} · última revisión {last_check}', { what, every_hours: everyLabel(f.every_hours), last_check: ago(f.last_check) });
    // what the last check brought, and why something didn't come (PLAN 0.20.2)
    const h = (f.history || []).at(-1);
    let last = null;
    if (h) {
      const bits = [h.new ? tn('{n} nuevo', '{n} nuevos', h.new) : tr('nada nuevo'), h.skipped ? tr('{skipped} ya los tenías', { skipped: h.skipped }) : '',
        h.failed?.length ? `${h.failed.length} ${h.failed.length === 1 ? tr('falló') : tr('fallaron')}: ${h.failed[0]}` : ''].filter(Boolean);
      last = document.createElement('small');
      last.className = 'follow-last';
      last.textContent = bits.join(' · ');
      last.title = (f.history || []).slice(-10).reverse().map((x) => `${new Date(x.at * 1000).toLocaleString(LOCALE)}: ${tr('{new} nuevos, {skipped} ya estaban', { new: x.new, skipped: x.skipped })}${x.failed?.length ? tr(', {failed} con error', { failed: x.failed.length }) : ''}`).join('\n');
    }
    body.append(name, meta, ...(last ? [last] : []));
    const chips = document.createElement('div');
    chips.className = 'chips';
    const now = document.createElement('button');
    now.className = 'gm-chip gm-chip-plain';
    now.textContent = tr('Revisar ahora');
    now.addEventListener('click', async () => {
      const r = await call('follow_check_now', { follow_id: f.id });
      toast(r.ok ? tr('En cola: se baja lo nuevo.') : tr(r.error), { duration: 5000 });
      loadFollows();
    });
    const rm = document.createElement('button');
    rm.className = 'gm-chip gm-chip-plain gm-chip-round';
    rm.title = tr('Dejar de seguir');
    rm.setAttribute('aria-label', tr('Dejar de seguir {title}', { title: f.title }));
    rm.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    rm.addEventListener('click', async () => { await call('follow_remove', { follow_id: f.id }); loadFollows(); });
    chips.append(now, rm);
    li.append(body, chips);
    ul.append(li);
  }
}

async function openFollow(t) {
  if (!(await ensureMedia())) return;
  fwTarget = t;
  const web = t.kind === 'web';
  $('fw-lead').textContent = tr('Seguir {t}: Bakin lo revisa solo y baja lo que se publique.', { t: labelFor(t) });
  $('fw-preset-row').hidden = !web;
  if (web) fillPresetSelect($('fw-preset'), MEDIA.default_preset);
  $('fw-new').checked = true;
  $('fw-new-hint').textContent = tr('Lo que ya publicó se marca como visto y no se baja (se lee la lista una vez).');
  $('fw-status').textContent = '';
  fwEvery.set('24');
  $('fw-go').disabled = false;
  $('follow-dialog').show();
}

async function submitFollow() {
  const btn = $('fw-go');
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  if ($('fw-new').checked && fwTarget.kind === 'web') $('fw-status').textContent = tr('Leyendo lo que ya publicó para marcarlo como visto…');
  try {
    const r = await call('follow_add', { url: urlFor(fwTarget), preset_id: $('fw-preset').value,
      every_hours: Number(fwEvery.value) || 24, only_new: $('fw-new').checked });
    if (!r.ok) { $('fw-status').textContent = tr(r.error); return; }
    $('follow-dialog').close();
    const seen = r.follow.marked_seen;
    toast(seen ? tr('Siguiendo {title} · {seen} publicaciones marcadas como vistas.', { title: r.follow.title, seen }) : tr('Siguiendo {title}.', { title: r.follow.title }),
      { duration: 7000, action: { label: tr('Ver'), onClick: () => setView('downloads') } });
    loadFollows();
  } catch (e) {
    $('fw-status').textContent = String(e);
  } finally {
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
  }
}

function wireFollows() {
  fwEvery = chipGroup($('fw-every'), () => {});
  $('fw-cancel').addEventListener('click', () => $('follow-dialog').close());
  $('fw-go').addEventListener('click', submitFollow);
  $('btn-follows-all').addEventListener('click', async () => {
    const { follows } = await call('follows_list');
    for (const f of follows) await call('follow_check_now', { follow_id: f.id });
    toast(tr('Revisando todos: lo nuevo se suma a la cola.'));
    loadFollows();
  });
  // hours
  const sync = () => { $('hours-row').hidden = !$('hours-on').checked; };
  const save = async () => {
    sync();
    const r = await call('set_hours', { enabled: $('hours-on').checked, start: Number($('hours-from').value), end: Number($('hours-to').value) });
    if (!r.ok) toast(tr(r.error));
  };
  $('hours-on').addEventListener('change', save);
  $('hours-from').addEventListener('change', save);
  $('hours-to').addEventListener('change', save);
  loadFollows();
  setInterval(loadFollows, 5 * 60 * 1000);
}

function wireMusic() {
  upFormat = chipGroup($('up-format'), () => {});
  $('up-cancel').addEventListener('click', () => $('upgrade-dialog').close());
  $('up-go').addEventListener('click', submitUpgrade);
  $('sel-upgrade').addEventListener('click', openUpgrade);
  $('lib-lowq').addEventListener('click', () => { L.lowq = !L.lowq; syncMusicControls(); reloadItems(); });
  wireWallpaperFilters();
}

// ---------------------------------------------------------------- convert
// Images (Pillow), audio and video (ffmpeg) to another format, as new files.
// Files come from the picker, drag & drop from Explorer, or the library.
const CV = { files: [], formats: null, running: false };
const CV_LABEL = { jpg: 'JPG', png: 'PNG', webp: 'WebP', avif: 'AVIF', mp3: 'MP3', m4a: 'M4A', opus: 'OPUS', flac: 'FLAC',
  ogg: 'OGG', wav: 'WAV', mp4: 'MP4', webm: 'WebM', mkv: 'MKV', gif: 'GIF' };
const CV_HINT = {
  jpg: tr('Compatible con todo; sin transparencia (se rellena con blanco).'), png: tr('Sin pérdida; archivos más grandes.'),
  webp: tr('Mucho más liviano que JPG con la misma calidad.'), avif: tr('El más liviano; algunos programas viejos no lo abren.'),
  mp3: 'Universal.', m4a: tr('AAC: mejor que MP3 al mismo bitrate.'), opus: tr('La mejor relación calidad/tamaño.'),
  flac: tr('Sin pérdida.'), ogg: tr('Vorbis, open source.'), wav: tr('Sin comprimir: muy grande.'),
  mp4: tr('H.264 + AAC: se reproduce en todos lados.'), webm: tr('VP9 + Opus: liviano, ideal para la web.'),
  mkv: tr('Sin cambiar la resolución, solo cambia el contenedor: instantáneo y sin pérdida.'), gif: tr('Animación sin sonido (12 fps).'),
};
let cvKind, cvFormat, cvAq, cvVq, cvWhere;

async function loadConvertFormats() {
  if (CV.formats) return;
  try {
    CV.formats = await call('convert_formats');
  } catch (err) { log('danger', `Convertir: ${err}`); return; }
  renderCvFormats();
}

function renderCvFormats(prefer) {
  const kind = cvKind.value;
  const list = CV.formats?.[kind] || [];
  const box = $('cv-format');
  box.replaceChildren(...list.map((f) => {
    const b = document.createElement('button');
    b.className = 'gm-chip gm-chip-plain';
    b.value = f;
    b.textContent = CV_LABEL[f] || f.toUpperCase();
    return b;
  }));
  cvFormat = chipGroup(box, syncCvOptions);
  cvFormat.set(prefer && list.includes(prefer) ? prefer : list[0]);
  syncCvOptions();
}

function syncCvOptions() {
  const kind = cvKind.value;
  const f = cvFormat?.value;
  $('cv-opts-image').hidden = kind !== 'image';
  $('cv-opts-audio').hidden = kind !== 'audio';
  $('cv-opts-video').hidden = kind !== 'video';
  $('cv-quality-row').hidden = f === 'png';
  $('cv-aq').hidden = ['flac', 'wav'].includes(f);
  $('cv-aq-hint').textContent = ['flac', 'wav'].includes(f) ? tr('{x} no pierde calidad: no hay nada que elegir.', { x: CV_LABEL[f] }) : tr('kbps. «Máxima» usa calidad variable (VBR), como al descargar.');
  $('cv-format-hint').textContent = CV_HINT[f] || '';
  $('cv-video-hint').textContent = f === 'mkv' ? tr('Elige una resolución para recomprimir a H.264.') : f === 'gif' ? tr('Sin resolución elegida, se reduce a 480p.') : '';
  if (kind !== 'image' && CV.formats && !CV.formats.ffmpeg) $('cv-format-hint').textContent = tr('Necesita ffmpeg: instálalo desde Descargas › Herramientas.');
  renderCvList();
}

// Which kinds each output accepts (audio out of a video is allowed).
function cvAccepts(fileKind) {
  const out = cvKind.value;
  return out === fileKind || (out === 'audio' && fileKind === 'video');
}

async function addConvertFiles(items) {
  if (!items.length) return;
  let res;
  try { res = await call('convert_inspect', { items }); } catch (err) { toast(tr(String(err))); return; }
  const known = new Set(CV.files.map((f) => f.path));
  let added = 0;
  for (const f of res.files) {
    if (known.has(f.path)) continue;
    CV.files.push({ ...f, status: 'pending' });
    known.add(f.path);
    added += 1;
  }
  // Switch the output type to what was added, if everything is of one kind.
  const kinds = new Set(CV.files.map((f) => f.kind));
  if (kinds.size === 1 && !CV.running) {
    const k = [...kinds][0];
    if (['image', 'audio', 'video'].includes(k) && cvKind.value !== k) { cvKind.set(k); renderCvFormats(); }
  }
  renderCvList();
  if (res.skipped) toast(tr('{skipped} no se pudieron agregar (no existen o no se permiten).', { skipped: res.skipped }));
  return added;
}

function renderCvList() {
  const ul = $('cv-list');
  ul.replaceChildren();
  const target = cvFormat?.value;
  let ready = 0;
  for (const f of CV.files) {
    const li = document.createElement('li');
    li.className = 'cv-item';
    li.dataset.status = f.status;
    const art = document.createElement('span');
    art.className = 'cv-art';
    if (f.thumb) { const img = new Image(); img.alt = ''; img.src = f.thumb; art.append(img); } else art.innerHTML = ICONS[f.kind] || ICONS.other;
    const body = document.createElement('div');
    body.className = 'cv-body';
    const name = document.createElement('strong');
    name.textContent = f.name;
    const meta = document.createElement('small');
    const ok = cvAccepts(f.kind) && f.ext !== target && !(f.ext === 'jpeg' && target === 'jpg');
    if (f.status === 'pending' && ok) ready += 1;
    meta.textContent = f.status === 'done' ? tr('Listo → {out_name} · {out_size}', { out_name: f.out_name, out_size: fmtBytes(f.out_size) })
      : f.status === 'error' ? tr(f.message)
        : f.status === 'working' ? tr('Convirtiendo… {x} %', { x: Math.round((f.progress || 0) * 100) })
          : ok ? `${fmtBytes(f.size)} · ${f.ext.toUpperCase()} → ${CV_LABEL[target] || ''}`
            : tr('{size} · no se puede convertir a {x}', { size: fmtBytes(f.size), x: CV_LABEL[target] || '' });
    body.append(name, meta);
    if (f.status === 'working') {
      const m = document.createElement('span');
      m.className = 'gm-meter';
      m.setAttribute('role', 'meter');
      setMeterOf(m, f.progress || 0, 1);
      body.append(m);
    }
    const chips = document.createElement('div');
    chips.className = 'chips';
    const mk = (label, fn, title) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'gm-chip gm-chip-plain';
      b.textContent = label;
      if (title) b.title = title;
      b.addEventListener('click', fn);
      chips.append(b);
    };
    if (f.status === 'done') {
      mk(tr('Abrir'), () => call('convert_open', { path: f.out }));
      mk(tr('Mostrar'), () => call('convert_open', { path: f.out, reveal: true }), tr('Mostrar en la carpeta'));
    } else if (f.status !== 'working') {
      mk('×', () => { CV.files = CV.files.filter((x) => x !== f); renderCvList(); }, tr('Quitar de la lista'));
    }
    li.classList.toggle('dim', f.status === 'pending' && !ok);
    li.append(art, body, chips);
    ul.append(li);
  }
  const n = CV.files.length;
  $('cv-empty').hidden = n > 0;
  $('cv-clear').hidden = !n || CV.running;
  $('cv-count').textContent = n ? tn('{n} archivo', '{n} archivos', n) : '';
  $('cv-go').disabled = !ready || CV.running;
  $('cv-go').textContent = CV.running ? tr('Convirtiendo…') : ready ? tr('Convertir {ready} a {x}', { ready, x: CV_LABEL[target] || '' }) : 'Convertir';
  const skip = CV.files.filter((f) => f.status === 'pending').length - ready;
  $('cv-summary').textContent = skip > 0 ? tr('{skip} se saltean (otro tipo o ya están en ese formato).', { skip }) : '';
}

function cvOptions() {
  return {
    target: cvFormat.value,
    quality: Number($('cv-quality').value) || 90,
    max_size: Number($('cv-size').value) || 0,
    audio_quality: cvAq.value,
    video_quality: cvVq.value,
    video_max_height: Number($('cv-height').value) || 0,
    keep_metadata: $('cv-meta').checked,
    out_dir: cvWhere.value === 'folder' ? $('cv-outdir').value : '',
  };
}

async function startConvert() {
  const target = cvFormat.value;
  const todo = CV.files.filter((f) => f.status === 'pending' && cvAccepts(f.kind) && f.ext !== target);
  if (!todo.length) return;
  if (cvWhere.value === 'folder' && !$('cv-outdir').value) { toast(tr('Elige la carpeta de salida.')); return; }
  const res = await call('convert_start', { paths: todo.map((f) => f.path), options: cvOptions() }).catch((e) => ({ ok: false, error: String(e) }));
  if (!res.ok) {
    toast(tr(res.error), { duration: 6000 });
    if (res.field === 'tools') openTools();
    return;
  }
  for (const f of todo) { f.status = 'queued'; }
  CV.running = true;
  renderCvList();
  if (res.waiting) toast(tr('En cola: empieza cuando termine lo que se está descargando.'));
}

function onConvertEvent(ev) {
  const f = CV.files.find((x) => x.path === ev.src);
  if (!f) return;
  f.status = ev.status;
  f.progress = ev.progress;
  if (ev.status === 'done') { f.out = ev.out; f.out_name = ev.out_name; f.out_size = ev.size; }
  if (ev.status === 'error') { f.message = ev.message; log('danger', `${ev.name}: ${ev.message}`); }
  renderCvList();
}

function onConvertFinished() {
  CV.running = false;
  for (const f of CV.files) if (f.status === 'queued') f.status = 'pending';
  renderCvList();
}

function wireConvert() {
  cvKind = chipGroup($('cv-kind'), () => renderCvFormats());
  cvAq = chipGroup($('cv-aq'), () => {});
  cvVq = chipGroup($('cv-vq'), () => {});
  cvWhere = chipGroup($('cv-where'), (v) => { $('cv-outdir-row').hidden = v !== 'folder'; });
  $('cv-add').addEventListener('click', async () => {
    const picked = await tauri.dialog.open({ multiple: true, title: tr('Archivos para convertir') });
    const list = Array.isArray(picked) ? picked : picked ? [picked] : [];
    await addConvertFiles(list.map((path) => ({ path })));
  });
  $('cv-clear').addEventListener('click', () => { CV.files = []; renderCvList(); });
  $('cv-go').addEventListener('click', startConvert);
  $('cv-outdir-pick').addEventListener('click', async () => {
    const dir = await tauri.dialog.open({ directory: true, title: tr('Carpeta para los archivos convertidos') });
    if (typeof dir === 'string') $('cv-outdir').value = dir;
  });
  $('sel-convert').addEventListener('click', async () => {
    const ids = [...L.selected];
    exitSelecting();
    setView('convert');
    await loadConvertFormats();
    const n = await addConvertFiles(ids.map((id) => ({ id })));
    if (n) toast(tn('{n} archivo listo para convertir.', '{n} archivos listos para convertir.', n));
  });
  // Files dropped from Explorer onto the window (Tauri gives their paths).
  tauri.event.listen('tauri://drag-enter', () => { if (!$('view-convert').hidden) $('cv-drop').classList.add('drop-over'); });
  tauri.event.listen('tauri://drag-leave', () => $('cv-drop').classList.remove('drop-over'));
  tauri.event.listen('tauri://drag-drop', async (e) => {
    $('cv-drop').classList.remove('drop-over');
    const paths = e.payload?.paths || [];
    if (!paths.length) return;
    if ($('view-convert').hidden) setView('convert');
    await loadConvertFormats();
    await addConvertFiles(paths.map((path) => ({ path })));
  });
}

// ------------------------------------------- command palette (gm-search)
// Several links pasted at once (spaces, commas or line breaks between them; a
// single-line box may also glue them together, so a new "http" starts one too).
const isWeb = (v) => /^https?:\/\/[^\s/$.?#][^\s]*$/i.test(String(v).trim());
// Wallhaven first; any other http(s) link goes to yt-dlp.
const isWallhaven = (v) => /^(?:https?:\/\/)?(?:www\.)?(?:wallhaven\.cc\/(?:w|search|toplist|latest|hot|random|user\/[\w.-]+\/favorites)|whvn\.cc\/|w\.wallhaven\.cc\/full\/)/i.test(String(v).trim());
const classifyAny = (raw) => (isWallhaven(raw) ? { kind: 'wallhaven', value: raw.trim() }
  : isWeb(raw) ? { kind: 'web', value: raw.trim() } : null);

function classifyMany(raw) {
  const seen = new Set();
  const out = [];
  for (const part of raw.split(/[\s,;]+|(?=https?:\/\/)/)) {
    const t = part && classifyAny(part);
    if (!t) continue;
    const key = `${t.kind}:${t.value}`;
    if (!seen.has(key)) { seen.add(key); out.push(t); }
  }
  return out;
}

const urlFor = (t) => t.value;
// The site a link is from, as preferences are kept (mediadl.site_of).
const siteOf = (u) => { try { return new URL(u).hostname.toLowerCase().replace(/^(www|m)\./, ''); } catch { return ''; } };
const labelFor = (t) => t.value.replace(/^https?:\/\/(www\.)?/, '').slice(0, 60);

const recents = () => { try { return JSON.parse(store.get('bakin.recent', '[]')).filter((t) => t.kind === 'web' || t.kind === 'wallhaven'); } catch { return []; } };
function remember(t) {
  const list = [t, ...recents().filter((r) => !(r.kind === t.kind && r.value === t.value))].slice(0, 6);
  store.set('bakin.recent', JSON.stringify(list));
}

const encode = (mode, t) => `bakin:${mode}:${encodeURIComponent(JSON.stringify(t))}`;

function actionsFor(t) {
  if (!t) return [];
  if (t.kind === 'wallhaven') {
    const one = /\/w\/|whvn\.cc|w\.wallhaven\.cc/i.test(t.value);
    return [{ label: one ? tr('Bajar este fondo de Wallhaven') : tr('Bajar de Wallhaven (los 24 primeros)'),
              hint: one ? tr('En máxima calidad, con sus tags · queda en Imágenes › Wallhaven') : tr('Una búsqueda o una colección pública · solo contenido apto'),
              href: encode('download', t) }];
  }
  if (t.kind === 'web' && /open\.spotify\.com\/|^spotify:/.test(t.value)) {
    return [{ label: tr('Buscar estos temas en YouTube Music'), hint: tr('De Spotify se toman solo los nombres; cada tema se baja de YouTube Music'), href: encode('spotify', t) }];
  }
  if (t.kind === 'web' && /[?&]list=LM(&|$)/.test(t.value) && presetById('biliked')) {
    return [
      { label: tr('Descargar tus «Me gusta» de YouTube Music'), hint: tr('Con el preset «Me gusta» (usa cookies: cierra Brave antes)'), href: encode('liked', t) },
      { label: tr('Elegir formato y preset…'), hint: tr('Recuerda que esta lista necesita cookies'), href: encode('pick', t) },
    ];
  }
  if (t.kind === 'web') {
    const p = presetById(MEDIA?.site_prefs?.[siteOf(t.value)] || MEDIA?.default_preset);
    return [
      { label: `Descargar ${labelFor(t)}`, hint: p ? tr('Con «{name}» · {p} · yt-dlp', { name: p.name, p: formatLabel(p) }) : tr('Música o video con yt-dlp'), href: encode('download', t) },
      { label: tr('Elegir formato y preset…'), hint: tr('MP3, M4A, OPUS, FLAC… o video, antes de bajar'), href: encode('pick', t) },
      { label: tr('Seguir…'), hint: tr('Canal o playlist: Bakin revisa cada tanto y baja solo lo nuevo'), href: encode('follow', t) },
    ];
  }
  return [];
}

// The link box's modes: what the «Descargar» panel's link sections ask for.
const LINK_MODES = {
  link: [tr('Descargar'), tr('Pega un enlace: un fondo, una búsqueda o una colección de Wallhaven, o cualquier enlace que soporte yt-dlp (YouTube, SoundCloud, Twitch…). Varios a la vez también.'), tr('Pega un enlace (Wallhaven, YouTube…)')],
  media: [tr('Música y video'), tr('Pegá el enlace de un tema, un video, una playlist o un canal.'), tr('Enlace de YouTube, SoundCloud, Twitch…')],
  follow: [tr('Seguir'), tr('Un canal o una playlist: Bakin lo revisa cada tanto y baja solo lo nuevo.'), tr('Enlace del canal o la playlist')],
};
let linkMode = 'link';

// «Descargar»: one panel, every way to download in it (Wallhaven, a link,
// music or video, the YouTube Music likes, following a channel).
function openHub(sec = store.get('bakin.hub', 'wallhaven'), mode = 'link') {
  if (sec === 'liked') sec = 'link';  // «Me gusta» is a card in «Música y video» now
  if (!['catalog', 'wallhaven', 'link'].includes(sec)) sec = 'catalog';
  store.set('bakin.hub', sec);
  for (const b of document.querySelectorAll('#hub-nav .scope')) {
    if (b.dataset.sec === sec) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
  }
  for (const el of document.querySelectorAll('#download-hub section[data-sec]')) el.hidden = el.dataset.sec !== sec;
  if (sec === 'link') {
    const ok = !!presetById('biliked');
    $('hub-liked-go').disabled = !ok;
    $('hub-liked-hint').textContent = ok ? tr('Tu lista privada, leída una vez con tus cookies; cada tema se baja como público.')
      : tr('Falta el preset «Me gusta»: restáuralo en Configuración › Descargas.');
  }
  if (!$('download-hub').open) $('download-hub').show();
}

function openLinkDialog(mode = 'link') {
  linkMode = mode;
  const [, lead, placeholder] = LINK_MODES[mode];
  $('link-lead').textContent = lead;
  const input = $('go').querySelector('input');
  input.placeholder = placeholder;
  input.value = '';
  openHub('link', mode);
  requestAnimationFrame(() => input.focus());
}

// The first action is the one Enter takes: put the one the menu asked for first.
function linkActions(list) {
  const want = { media: 'pick', follow: 'follow' }[linkMode];
  if (!want) return list;
  const first = list.filter((a) => a.href.startsWith(`bakin:${want}:`));
  return [...first, ...list.filter((a) => !first.includes(a))];
}

// --------------------------------------------------------------- wallhaven
// Wallpapers from wallhaven.cc: search, pick from the thumbnails, download.
// The engine does the downloading (bakin/wallhaven.py): one request every
// 1.5 s, SFW by default, «sketchy» only if asked, never NSFW.
const WH = { page: 1, last: 1, seed: null, items: [], picked: new Set(), busy: false, filters: null };
const WH_RATIOS = [[16, 9], [16, 10], [21, 9], [32, 9], [48, 9], [9, 16], [10, 16], [9, 18], [1, 1], [3, 2], [4, 3], [5, 4]];

function nearestRatio(w, h) {
  let best = WH_RATIOS[0];
  for (const r of WH_RATIOS) if (Math.abs(w / h - r[0] / r[1]) < Math.abs(w / h - best[0] / best[1])) best = r;
  return `${best[0]}x${best[1]}`;
}

async function fillMonitors() {
  const sel = $('wh-for');
  let mons = [];
  try { mons = await tauri.window.availableMonitors(); } catch { /* not allowed or not available */ }
  const keep = store.get('bakin.wh.for', '');
  sel.replaceChildren(new Option(tr('Cualquier pantalla'), ''));
  mons.forEach((m, i) => {
    const { width: w, height: h } = m.size;
    const ratio = nearestRatio(w, h);
    sel.append(new Option(tr('Monitor {x} · {w}×{h} ({x2})', { x: i + 1, w, h, x2: ratio.replace('x', ':') }), `${w}x${h}|${ratio}`));
  });
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  sel.closest('gm-select')?.sync?.();
}

function whFilters() {
  const cats = [...document.querySelectorAll('#wh-cats button')].filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.value);
  const [sorting, topRange] = $('wh-sort').value.split(':');
  const [atleast, ratio] = ($('wh-for').value || '').split('|');
  return { q: $('wh-q').value.trim(), categories: cats, sorting, topRange, sketchy: $('wh-sketchy').checked,
           atleast: atleast || undefined, ratios: ratio ? [ratio] : undefined };
}

function whLabel() {
  return $('wh-q').value.trim() || $('wh-sort').selectedOptions[0]?.textContent || 'Wallhaven';
}

// «Filtros»: folded by default; the button says how many differ from the defaults
function whFiltersSync() {
  const cats = [...document.querySelectorAll('#wh-cats button')].filter((b) => b.getAttribute('aria-pressed') === 'true').length;
  const n = (cats < 3) + ($('wh-sort').value !== 'toplist:1M') + !!$('wh-for').value + $('wh-sketchy').checked;
  $('wh-filters-label').textContent = n ? tr('Filtros · {n}', { n }) : tr('Filtros');
  const open = !$('wh-filters').hidden;
  $('wh-filters-btn').setAttribute('aria-expanded', String(open));
}

function whSync() {
  const n = WH.picked.size;
  const bytes = WH.items.filter((i) => WH.picked.has(i.id)).reduce((a, i) => a + (i.file_size || 0), 0);
  $('wh-download').disabled = !n;
  $('wh-download').textContent = n ? tr('Bajar {n} · {bytes}', { n, bytes: fmtSize(bytes) }) : tr('Bajar');
  const free = WH.items.filter((i) => !i.have);
  $('wh-all').textContent = free.length && free.every((i) => WH.picked.has(i.id)) ? tr('Ninguno') : tr('Elegir todos');
}

function whTile(it) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'wh-tile';
  b.dataset.id = it.id;
  b.setAttribute('aria-pressed', String(WH.picked.has(it.id)));
  b.title = `${it.resolution} · ${fmtSize(it.file_size || 0)}${it.purity === 'sketchy' ? ' · sketchy' : ''}`;
  // the wallpaper's own shape (tall ones for a vertical monitor), within reason
  if (it.width && it.height) b.style.aspectRatio = `${Math.min(2.4, Math.max(0.5, it.width / it.height))}`;
  const img = new Image();
  img.alt = '';
  img.loading = 'lazy';
  img.decoding = 'async';  // decoded off the main thread: no hitch while scrolling
  img.referrerPolicy = 'no-referrer';
  // the small thumbnail (300 px) is enough for a ~200 px tile; the large one only on dense screens
  if (it.small && it.small !== it.thumb) {
    img.srcset = `${it.small} 300w, ${it.thumb} 600w`;
    img.sizes = '220px';
  }
  img.src = it.small || it.thumb;
  const res = document.createElement('span');
  res.className = 'wh-res';
  res.textContent = it.resolution;
  b.append(img, res);
  if (it.have) {
    b.disabled = true;
    const have = document.createElement('span');
    have.className = 'wh-have';
    have.textContent = tr('Ya lo tenés');
    b.append(have);
  }
  const zoom = document.createElement('span');
  zoom.className = 'wh-zoom';
  zoom.title = tr('Ver grande (o doble clic)');
  zoom.textContent = '⤢';
  zoom.addEventListener('click', (e) => { e.stopPropagation(); whPreview(it); });
  b.append(zoom);
  b.addEventListener('click', (e) => {
    if (e.detail > 1) return;  // the second click of a double click
    whToggle(it.id);
  });
  b.addEventListener('dblclick', () => { whToggle(it.id); whPreview(it); });  // undo the first click's toggle, then look
  return b;
}

function whToggle(id) {
  if (WH.picked.has(id)) WH.picked.delete(id); else WH.picked.add(id);
  const t = document.querySelector(`#wh-grid .wh-tile[data-id="${CSS.escape(id)}"]`);
  t?.setAttribute('aria-pressed', String(WH.picked.has(id)));
  whSync();
}

// A wallpaper in big, over the grid: the large thumbnail at once, the full file when it arrives.
function whPreview(it) {
  WH.previewing = it;
  const img = $('wh-preview-img');
  img.decoding = 'async';
  img.src = it.thumb;
  // the full file (often 4K, several MB) only if they stay on it: flicking through stays light
  clearTimeout(WH.fullTimer);
  WH.fullTimer = setTimeout(() => {
    if (WH.previewing !== it) return;
    const full = new Image();
    full.referrerPolicy = 'no-referrer';
    full.decoding = 'async';
    full.onload = () => full.decode().catch(() => {}).then(() => { if (WH.previewing === it) img.src = it.path; });
    full.src = it.path;
  }, 600);
  $('wh-preview-info').textContent = `${it.resolution} · ${fmtSize(it.file_size || 0)}${it.purity === 'sketchy' ? ' · sketchy' : ''}`;
  $('wh-preview-pick').hidden = !!it.have;
  $('wh-preview-pick').textContent = WH.picked.has(it.id) ? tr('Quitar') : tr('Elegir');
  $('wh-preview').hidden = false;
}

function whPreviewClose() {
  WH.previewing = null;
  clearTimeout(WH.fullTimer);
  $('wh-preview').hidden = true;
  $('wh-preview-img').removeAttribute('src');
}

// «Ajustar encuadre…» (0.13 review): the photo with a frame of the chosen monitor's shape,
// dragged (or moved with the arrows) to what should show; kept per photo and monitor shape,
// so the rotation uses it too.
const FR = { it: null, mons: [], m: null, focus: {}, fx: 0.5, fy: 0.5 };

async function openFrame(it, monitors) {
  FR.it = it;
  FR.mons = monitors;
  FR.focus = await tauri.core.invoke('wallpaper_focus_get', { path: it.path }).catch(() => ({}));
  const img = $('frame-img');
  img.src = assetUrl(it.path);
  await img.decode().catch(() => {});
  const wide = img.naturalWidth >= img.naturalHeight;
  // start on the monitor that cuts the most (a landscape photo: the vertical one)
  const first = monitors.find((m) => m.vertical === wide) || monitors[0];
  $('frame-monitors').replaceChildren(...monitors.map((m) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'gm-chip gm-chip-plain';
    b.textContent = `Monitor ${m.number}${m.vertical ? ' · vertical' : ''}`;
    b.addEventListener('click', () => frameMonitor(m));
    b.dataset.n = String(m.number);
    return b;
  }));
  $('frame-dialog').show();
  requestAnimationFrame(() => frameMonitor(first));
}

function frameMonitor(m) {
  FR.m = m;
  const f = FR.focus[`${m.w}x${m.h}`] || [0.5, 0.5];
  [FR.fx, FR.fy] = f;
  for (const b of $('frame-monitors').children) b.setAttribute('aria-pressed', String(b.dataset.n === String(m.number)));
  $('frame-go').textContent = tr('Poner en el monitor {number}', { number: m.number });
  frameLayout();
}

function frameBox() {
  const img = $('frame-img');
  const W = img.clientWidth;
  const H = img.clientHeight;
  const ar = FR.m.w / FR.m.h;
  return W / H > ar ? { W, H, w: H * ar, h: H } : { W, H, w: W, h: W / ar };
}

function frameLayout() {
  if (!FR.m) return;
  const { W, H, w, h } = frameBox();
  const box = $('frame-box');
  Object.assign(box.style, { width: `${w}px`, height: `${h}px`, left: `${(W - w) * FR.fx}px`, top: `${(H - h) * FR.fy}px` });
  // only the side that is cut moves
  box.dataset.axis = W - w > 1 ? 'x' : H - h > 1 ? 'y' : '';
  $('frame-hint').textContent = box.dataset.axis ? tr('Arrastrá el recuadro: es lo que va a verse en ese monitor.') : tr('La foto ya tiene la forma de ese monitor: se ve entera.');
}

function wireFrame() {
  const box = $('frame-box');
  let drag = null;
  box.addEventListener('pointerdown', (e) => {
    box.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, fx: FR.fx, fy: FR.fy };
  });
  box.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const { W, H, w, h } = frameBox();
    if (W - w > 1) FR.fx = Math.max(0, Math.min(1, drag.fx + (e.clientX - drag.x) / (W - w)));
    if (H - h > 1) FR.fy = Math.max(0, Math.min(1, drag.fy + (e.clientY - drag.y) / (H - h)));
    frameLayout();
  });
  box.addEventListener('pointerup', () => { drag = null; });
  box.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 0.1 : 0.02;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (!moves[e.key]) return;
    e.preventDefault();
    FR.fx = Math.max(0, Math.min(1, FR.fx + moves[e.key][0]));
    FR.fy = Math.max(0, Math.min(1, FR.fy + moves[e.key][1]));
    frameLayout();
  });
  addEventListener('resize', () => { if ($('frame-dialog').hasAttribute('open')) frameLayout(); });
  $('frame-reset').addEventListener('click', () => { FR.fx = 0.5; FR.fy = 0.5; frameLayout(); });
  $('frame-cancel').addEventListener('click', () => $('frame-dialog').close());
  $('frame-go').addEventListener('click', async () => {
    const { m, it } = FR;
    try {
      await tauri.core.invoke('wallpaper_focus_set', { path: it.path, w: m.w, h: m.h, x: FR.fx, y: FR.fy });
      FR.focus[`${m.w}x${m.h}`] = [FR.fx, FR.fy];
      await tauri.core.invoke('wallpaper_set', { path: it.path, number: m.number });
      toast(tr('Listo: fondo del monitor {number}, con ese encuadre.', { number: m.number }));
      refreshWallpaperShown();
      $('frame-dialog').close();
    } catch (err) {
      toast(tr(String(err)), { duration: 6000 });
    }
  });
}

// What the grid shows: a search, or one of the user's collections (PLAN 0.13.4)
function whSource() {
  return WH.collection ? { type: 'collection', user: WH.collection.user, id: WH.collection.id, label: WH.collection.label, sketchy: $('wh-sketchy').checked }
    : { type: 'search', filters: whFilters(), label: whLabel() };
}

async function whMine() {
  const box = $('wh-mine');
  const acc = await call('wallhaven_account').catch(() => null);
  if (!acc?.key || !acc.user) { box.hidden = true; return; }
  const res = await call('wallhaven_my_collections').catch(() => null);
  if (!res?.ok || !res.collections.length) { box.hidden = true; return; }
  const chip = (label, on, click) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'gm-chip gm-chip-plain';
    b.textContent = label;
    b.setAttribute('aria-pressed', String(on));
    b.addEventListener('click', click);
    return b;
  };
  const head = Object.assign(document.createElement('span'), { className: 'hint', textContent: tr('Mis colecciones:') });
  box.replaceChildren(head, ...res.collections.map((c) => chip(`${c.label} · ${c.count}${c.public ? '' : ' 🔒'}`, WH.collection?.id === c.id, () => {
    WH.collection = WH.collection?.id === c.id ? null : { user: res.user, id: c.id, label: c.label };
    whMine();
    whSearch();
  })));
  box.hidden = false;
}

async function whSearch(more = false) {
  whFiltersSync();
  if (WH.busy) return;
  WH.busy = true;
  if (!more) {
    WH.page = 1; WH.seed = null; WH.items = []; WH.picked.clear();
    WH.filters = whFilters();
    $('wh-grid').replaceChildren();
    store.set('bakin.wh.sort', $('wh-sort').value);
    store.set('bakin.wh.for', $('wh-for').value);
    store.set('bakin.wh.sketchy', $('wh-sketchy').checked ? 'on' : 'off');
    store.set('bakin.wh.cats', WH.filters.categories.join(','));
    store.set('bakin.wh.q', WH.filters.q);
  }
  $('wh-status').textContent = tr('Buscando en Wallhaven…');
  $('wh-more').hidden = true;
  const ghosts = Array.from({ length: more ? 5 : 15 }, () => Object.assign(document.createElement('div'), { className: 'wh-ghost' }));
  $('wh-grid').append(...ghosts);
  try {
    const page = more ? WH.page + 1 : 1;
    const res = WH.collection
      ? await call('wallhaven_collection', { user: WH.collection.user, collection_id: WH.collection.id, page, sketchy: $('wh-sketchy').checked })
      : await call('wallhaven_search', { filters: { ...WH.filters, seed: WH.seed || undefined }, page });
    if (!res.ok) { $('wh-status').textContent = tr(res.error); return; }
    WH.page = res.page; WH.last = res.last_page; WH.seed = res.seed || WH.seed;
    WH.items.push(...res.items);
    $('wh-grid').append(...res.items.map(whTile));
    $('wh-status').textContent = res.total
      ? tr('{total} fondos. Tocá los que quieras bajar.', { total: res.total.toLocaleString(LOCALE) })
      : tr('No hay fondos con esos filtros. Probá con otros tags o «Cualquier pantalla».');
    $('wh-more').hidden = WH.page >= WH.last;
  } catch (err) {
    $('wh-status').textContent = String(err);
  } finally {
    for (const g of document.querySelectorAll('#wh-grid .wh-ghost')) g.remove();
    WH.busy = false;
    whSync();
  }
}

async function openWallhaven() {
  $('wh-sort').value = store.get('bakin.wh.sort', 'toplist:1M');
  $('wh-sort').closest('gm-select')?.sync?.();
  $('wh-sketchy').checked = store.get('bakin.wh.sketchy', 'off') === 'on';
  if (!WH.items.length) $('wh-q').value = store.get('bakin.wh.q', '');
  const cats = store.get('bakin.wh.cats', 'general,anime,people').split(',');
  for (const b of document.querySelectorAll('#wh-cats button')) b.setAttribute('aria-pressed', String(cats.includes(b.value)));
  openHub('wallhaven');
  await fillMonitors();
  whMine();  // the user's collections, if they saved their key
  if (!WH.items.length) whSearch();
  requestAnimationFrame(() => $('wh-q').focus());
}

function wireWallhaven() {
  // typing a search leaves the collection shown
  const searchNow = () => { if (WH.collection) { WH.collection = null; whMine(); } whSearch(); };
  $('wh-go').addEventListener('click', searchNow);
  $('wh-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); searchNow(); } });
  for (const id of ['wh-sort', 'wh-for']) $(id).addEventListener('change', () => whSearch());
  $('wh-sketchy').addEventListener('change', () => whSearch());
  for (const b of document.querySelectorAll('#wh-cats button')) {
    b.addEventListener('click', () => {
      const on = b.getAttribute('aria-pressed') !== 'true';
      const others = [...document.querySelectorAll('#wh-cats button')].filter((x) => x !== b && x.getAttribute('aria-pressed') === 'true');
      if (!on && !others.length) return;  // at least one category
      b.setAttribute('aria-pressed', String(on));
      whSearch();
    });
  }
  $('wh-more').addEventListener('click', () => whSearch(true));
  $('wh-scroll').addEventListener('scroll', (e) => {
    const s = e.currentTarget;
    if (!WH.busy && WH.page < WH.last && s.scrollTop + s.clientHeight > s.scrollHeight - 240) whSearch(true);
  }, { passive: true });
  $('wh-preview-close').addEventListener('click', whPreviewClose);
  $('wh-preview').addEventListener('click', (e) => { if (e.target === e.currentTarget || e.target.id === 'wh-preview-img') whPreviewClose(); });
  $('wh-preview-pick').addEventListener('click', () => {
    const it = WH.previewing;
    if (!it) return;
    whToggle(it.id);
    whPreviewClose();
  });
  $('wh-preview-open').addEventListener('click', () => {
    const it = WH.previewing;
    if (it) call('wallhaven_open', { wallpaper_id: it.id }).catch(() => {});
  });
  $('download-hub').addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('wh-preview').hidden) { e.preventDefault(); e.stopPropagation(); whPreviewClose(); }
  }, true);
  $('download-hub').addEventListener('gm-close', whPreviewClose);
  $('wh-follow').addEventListener('click', () => $('wh-follow-menu').open());
  $('wh-filters').hidden = store.get('bakin.wh.filters', 'closed') !== 'open';
  $('wh-filters-btn').addEventListener('click', () => {
    $('wh-filters').hidden = !$('wh-filters').hidden;
    store.set('bakin.wh.filters', $('wh-filters').hidden ? 'closed' : 'open');
    whFiltersSync();
  });
  $('wh-follow-menu').addEventListener('gm-select', async (e) => {
    if (e.detail.value === 'all') return;  // «Elegir todos»: its own click handler
    const src = whSource();
    const res = await call('wallhaven_follow', { job: src, every_hours: Number(e.detail.value) }).catch((err) => ({ ok: false, error: String(err) }));
    if (!res.ok) { toast(tr(res.error), { duration: 6000 }); return; }
    toast(tr('Siguiendo «{label}»: lo nuevo va a Imágenes › Wallhaven › {label}.', { label: res.follow.job.label }));
  });
  $('wh-all').addEventListener('click', () => {
    const free = WH.items.filter((i) => !i.have);
    const all = free.length && free.every((i) => WH.picked.has(i.id));
    for (const i of free) if (all) WH.picked.delete(i.id); else WH.picked.add(i.id);
    for (const t of document.querySelectorAll('#wh-grid .wh-tile')) t.setAttribute('aria-pressed', String(WH.picked.has(t.dataset.id)));
    whSync();
  });
  $('wh-download').addEventListener('click', async () => {
    const ids = WH.items.filter((i) => WH.picked.has(i.id)).map((i) => i.id);
    const res = await call('wallhaven_download', { job: { type: 'ids', ids, label: WH.collection?.label || whLabel() } }).catch((err) => ({ ok: false, error: String(err) }));
    if (!res.ok) { toast(tr(res.error), { duration: 6000 }); return; }
    toast(tn('{n} fondo en cola. Quedan en Imágenes, en la colección «Wallhaven».', '{n} fondos en cola. Quedan en Imágenes, en la colección «Wallhaven».', ids.length));
    for (const i of WH.items) if (WH.picked.has(i.id)) i.have = true;
    WH.picked.clear();
    $('download-hub').close();
    WH.items = [];  // a fresh search next time (marks what is already downloaded)
  });
}

function wireSkeleton() {
  // the sidebar: collapsed to icons (wide window) or a drawer (narrow window)
  const narrow = matchMedia('(max-width: 900px)');
  const sync = () => {
    const collapsed = store.get('bakin.side', 'open') === 'collapsed';
    document.body.classList.toggle('side-collapsed', collapsed && !narrow.matches);
    $('btn-side').setAttribute('aria-expanded', String(narrow.matches ? document.body.classList.contains('side-open') : !collapsed));
    showCount();
  };
  const toggle = () => {
    if (narrow.matches) document.body.classList.toggle('side-open');
    else store.set('bakin.side', store.get('bakin.side', 'open') === 'collapsed' ? 'open' : 'collapsed');
    sync();
  };
  $('btn-side').addEventListener('click', toggle);
  // the folded sidebar shows the tags as one button: it opens the sidebar on them
  $('tags-fold').addEventListener('click', () => {
    toggle();
    requestAnimationFrame(() => $('tags-card').scrollIntoView({ block: 'nearest' }));
  });
  narrow.addEventListener('change', () => { document.body.classList.remove('side-open'); sync(); });
  // in the drawer, picking something closes it
  for (const side of document.querySelectorAll('.sidebar')) {
    side.addEventListener('click', (e) => {
      if (narrow.matches && e.target.closest('.scope, .collection, .gm-chip:not(.side-keep)')) { document.body.classList.remove('side-open'); sync(); }
    });
  }
  document.addEventListener('pointerdown', (e) => {
    if (document.body.classList.contains('side-open') && !e.target.closest('.sidebar, #btn-side')) { document.body.classList.remove('side-open'); sync(); }
  });
  // icons alone need their names on hover
  for (const b of document.querySelectorAll('.sidebar .scope')) b.title ||= b.querySelector('span')?.textContent || '';
  sync();

  const showSec = (sec, mode) => (sec === 'wallhaven' ? openWallhaven() : sec === 'link' ? openLinkDialog(mode)
    : sec === 'catalog' ? openCatalog() : openHub(sec));
  $('btn-download').addEventListener('click', () => {
    const sec = store.get('bakin.hub', 'catalog');  // music first (0.15)
    showSec(sec, linkMode);
  });
  for (const b of document.querySelectorAll('#hub-nav .scope')) b.addEventListener('click', () => showSec(b.dataset.sec, b.dataset.mode));
  $('hub-liked-go').addEventListener('click', () => {
    $('download-hub').close();
    startJob('download', MEDIA.liked_url, { preset: 'biliked' });
  });
  $('go').addEventListener('gm-select', () => $('download-hub').close());

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.body.classList.contains('side-open')) { document.body.classList.remove('side-open'); sync(); return; }
    if (!e.ctrlKey || e.altKey || e.metaKey || document.querySelector('dialog[open]')) return;
    const k = e.key.toLowerCase();
    if (k === 'b') { e.preventDefault(); toggle(); }
    else if (k === 'k' || k === 'f') { e.preventDefault(); $('lib-q').focus(); $('lib-q').select(); }
    else if (k === 'd') { e.preventDefault(); openLinkDialog(); }
  });
  // pasting a link into the library search offers to download it
  $('lib-q').addEventListener('paste', (e) => {
    const text = e.clipboardData?.getData('text') || '';
    if (!classifyMany(text).length) return;
    e.preventDefault();
    openLinkDialog();
    const input = $('go').querySelector('input');
    input.value = text.trim();
    input.dispatchEvent(new Event('input'));
  });
}

function wireSearch() {
  const go = $('go');
  go.source = (q) => {
    if (!q) {
      const items = recents().map((t) => ({ label: labelFor(t),
                                            hint: tr('Reciente · Enter para descargar'), href: encode('download', t) }));
      return items;  // «Me gusta» is the card under the field
    }
    const many = classifyMany(q);
    if (many.length > 1) {
      const names = many.map(labelFor);
      const acts = [{ label: tr('Descargar los {many} enlaces', { many: many.length }),
                      hint: tr('En cola, uno tras otro · {names}{x}', { names: names.slice(0, 3).join(', '), x: names.length > 3 ? '…' : '' }),
                      href: encode('download', many) }];
      if (many.some((t) => t.kind === 'web')) {
        acts.push({ label: tr('Elegir formato para la música y los videos…'), hint: tr('Los de Wallhaven se bajan como siempre'), href: encode('pick', many) });
      }
      return acts;
    }
    return linkActions(actionsFor(classifyAny(q)));
  };
  go.addEventListener('gm-select', (e) => {
    e.preventDefault();
    const [, mode, payload] = e.detail.href.split(':');
    if (!payload) return;
    const t = JSON.parse(decodeURIComponent(payload));
    const list = Array.isArray(t) ? t : [t];
    if (mode === 'liked') { startJob('download', urlFor(list[0]), { preset: 'biliked' }); return; }
    if (mode === 'follow') { openFollow(list[0]); return; }
    if (mode === 'spotify') { openSpotify(urlFor(list[0])); return; }
    list.forEach(remember);
    if (mode === 'pick') {
      const ig = list.filter((x) => x.kind !== 'web');
      if (ig.length) startMany('download', ig.map((x) => [urlFor(x), {}]));
      openMediaDialog(list.filter((x) => x.kind === 'web').map(urlFor));
      return;
    }
    if (list.length > 1) startMany(mode, list.map((x) => [urlFor(x), {}]));
    else startJob(mode, urlFor(list[0]));
  });
}

// ------------------------------------------------------------------- start
// ----------------------------------------------------------- music views
// Bakin Música in the library: views over the songs (artists, albums, most
// played…), mixes made from the play history, and playlists, normal or by
// rules. Each view is a library list, so search, selection and playing work
// the same everywhere.
const MVIEWS = {
  queue: tr('Cola'), songs: tr('Todo'), favorites: tr('Favoritas'), artists: tr('Artistas'), albums: tr('Álbumes'), top: tr('Más escuchadas'),
  recent: tr('Escuchadas hace poco'), added: tr('Agregadas hace poco'), never: tr('Nunca escuchadas'),
  artist: tr('Artista'), album: tr('Álbum'), playlist: 'Playlist', mix: 'Mix', stats: tr('Estadísticas'), radio: 'Radio',
};
const MUSIC_EMPTY = {
  top: [tr('Todavía no escuchaste nada en Bakin'), tr('Un tema cuenta cuando escuchas la mitad o cuatro minutos. Lo que más suena aparece acá.')],
  recent: [tr('Todavía no escuchaste nada en Bakin'), tr('Lo último que escuches aparece acá.')],
  favorites: [tr('Todavía no tienes temas favoritos'), tr('Toca el ♥ del reproductor mientras suena un tema (o la tecla F), o selecciona temas y usa «♥ Favorita».')],
  playlist: [tr('La playlist está vacía'), tr('Selecciona temas en la biblioteca y usa «Agregar a playlist…». Si es inteligente, revisa sus reglas con «Editar».')],
  mix: [tr('Este mix ya no tiene temas'), tr('Los mixes se arman con lo que escuchas: vuelve mañana.')],
};
const ORDERED = new Set(['top', 'recent', 'added', 'playlist', 'mix', 'queue', 'radio']);  // the view sets the order
let MUSIC = null;  // music_overview

const groupView = () => ['artists', 'albums', 'stats'].includes(L.mview?.view);  // not a list of songs
const orderedView = () => ORDERED.has(L.mview?.view);

function musicFilter() {
  if (!L.mview || groupView()) return undefined;
  const { view, value, artist } = L.mview;
  if (view === 'queue') return { view, ids: P.queueIds };
  return { view, value, artist };
}

// Cola: the player's queue as a list, from the playing song on.
async function openQueueView(refresh = false) {
  const q = await tauri.core.invoke('player_call', { cmd: { cmd: 'queue' } }).catch(() => null);
  if (!q) return;
  P.queueIds = q.tracks.slice(q.pos).map((t) => t.id);
  P.queuePos = q.pos;
  $('mc-queue').textContent = P.queueIds.length || '';
  if (refresh) { if (L.mview?.view === 'queue') await reloadItems(); return; }
  await selectMusicView('queue', { name: q.source ? tr('Cola · desde {source}', { source: q.source }) : tr('Cola') });
}

// The cover and title in the player: the song in the list on screen, or in Cola.
async function locatePlaying() {
  const t = P.state.track;
  if (!t) return;
  if (L.space !== 'music') setView('music');
  if (groupView()) await selectMusicView('songs', { name: MVIEWS.songs });
  let i = L.items.findIndex((x) => x.id === t.id);
  // further down this list: load pages until it shows up (lists come in pages)
  for (let tries = 0; i < 0 && L.scope !== 'deleted' && L.items.length < L.total && tries < 200; tries++) {
    if (L.loading) await new Promise((r) => setTimeout(r, 50));  // a page already on its way
    else await loadMoreItems();
    i = L.items.findIndex((x) => x.id === t.id);
  }
  if (i < 0 || L.scope === 'deleted') {  // not in this list: Cola has it first
    await openQueueView();
    i = 0;
  }
  requestAnimationFrame(() => scrollToItem(i, t.id));
}

function scrollToItem(i, id) {
  const c = $('view-library');
  const grid = $('lib-grid');
  if (!VG.rowH) return;
  const head = grid.querySelector(':scope > .lib-row-head');
  const top = c.scrollTop + grid.getBoundingClientRect().top - c.getBoundingClientRect().top + (head ? head.offsetHeight + VG.gap : 0);
  // straight there (a smooth scroll across thousands of rows takes too long), then
  // light the row up as soon as the virtual list has drawn it
  c.scrollTop = Math.max(0, top + Math.floor(i / VG.cols) * VG.rowH - c.clientHeight / 2);
  scheduleWindow();
  let tries = 0;
  const flash = () => {
    const el = grid.querySelector(`[data-id="${CSS.escape(id)}"]`);
    if (!el) { if (++tries < 30) requestAnimationFrame(flash); return; }
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
  };
  requestAnimationFrame(flash);
}

async function loadMusicOverview() {
  try { MUSIC = await call('music_overview'); } catch { return; }
  renderMusicCard();
}

function renderMusicCard() {
  if (!MUSIC) return;
  $('mc-songs').textContent = MUSIC.songs;
  $('mc-favorites').textContent = MUSIC.favorites || '';
  $('mc-artists').textContent = MUSIC.artists;
  $('mc-albums').textContent = MUSIC.albums;
  $('mc-played').textContent = MUSIC.played || '';
  $('mc-never').textContent = MUSIC.never;
  const current = L.scope === 'deleted' ? 'deleted' : (L.mview?.view || (L.collection ? '' : 'songs'));
  for (const b of document.querySelectorAll('#music-views .scope')) {
    const on = b.dataset.mview === current || (b.dataset.mview === 'artists' && current === 'artist')
      || (b.dataset.mview === 'albums' && current === 'album');
    if (on) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
  }
  const entry = (icon, name, count, title, on, click) => {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'collection';
    if (title) b.title = title;
    if (on) b.setAttribute('aria-current', 'true');
    const art = document.createElement('span');
    art.className = 'collection-art';
    art.innerHTML = icon;
    const n = document.createElement('span');
    n.className = 'collection-name';
    n.textContent = name;
    const c = document.createElement('small');
    c.textContent = count;
    b.append(art, n, c);
    b.addEventListener('click', click);
    li.append(b);
    return li;
  };
  const MIX = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l2.5 5.5L20 9l-4 4 1 6-5-3-5 3 1-6-4-4 5.5-.5z"/></svg>';
  const LIST = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h12M4 12h12M4 18h8M18 14v6M15 17h6"/></svg>';
  const SMART = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h8M4 12h6M4 18h8M15 5l2 2 4-4M15 13l2 2 4-4"/></svg>';
  $('mixes-head').hidden = !MUSIC.mixes.length;
  $('music-mixes').replaceChildren(...MUSIC.mixes.map((m) => entry(MIX, m.name, m.count, m.desc,
    current === 'mix' && L.mview.value === m.id, () => selectMusicView('mix', { value: m.id, name: m.name }))));
  $('music-playlists').replaceChildren(...MUSIC.playlists.map((p) => entry(p.kind === 'smart' ? SMART : LIST, p.name, p.count,
    p.kind === 'smart' ? tr('Inteligente: se llena sola según sus reglas') : '',
    current === 'playlist' && L.mview.value === p.id, () => selectMusicView('playlist', { value: p.id, name: p.name }))));
  $('playlists-empty').hidden = MUSIC.playlists.length > 0;
}

function selectMusicView(view, extra = {}) {
  if (L.space !== 'music') setView('music');
  L.collection = null;
  L.kind = 'audio';
  if (view === 'deleted') {  // the trash, songs only
    L.mview = null;
    L.scope = 'deleted';
  } else {
    L.mview = { view, ...extra };
    L.scope = 'all';
  }
  if (L.overview) renderCollections(L.overview.collections);
  syncScopes();
  syncMusicControls();
  renderMusicCard();
  return reloadItems();
}

// ------------------------------------------- a clean library (PLAN 0.14)
// A song's right click: radio from it, clean its title. The menu is made again each time
// (its anchor is that row).
function openRowMenu(it, row) {
  $('row-menu')?.remove();
  const menu = document.createElement('gm-menu');
  menu.id = 'row-menu';
  row.id ||= `row-${Math.random().toString(36).slice(2, 9)}`;
  menu.setAttribute('anchor', row.id);
  const item = (value, label) => { const b = document.createElement('button'); b.type = 'button'; b.value = value; b.textContent = label; return b; };
  menu.append(item('radio', tr('Radio desde este tema')), item('clean', tr('Limpiar título y artista…')));
  menu.addEventListener('gm-select', (e) => {
    const name = it.title || it.name.replace(/\.[^.]+$/, '');
    if (e.detail.value === 'radio') selectMusicView('radio', { value: it.id, name: tr('Radio de «{name}»', { name: name.length > 40 ? `${name.slice(0, 38)}…` : name }) });
    if (e.detail.value === 'clean') openClean([it.id]);
  });
  document.body.append(menu);
  customElements.whenDefined('gm-menu').then(() => menu.open());
}

// Música › Estadísticas
let statsSpan = 'month';
async function renderStats(seq) {
  let st;
  try { st = await call('music_stats', { span: statsSpan }); } catch (err) { toast(tr(String(err))); return; }
  if (seq !== L.seq) return;
  $('st-plays').textContent = st.plays.toLocaleString(LOCALE);
  $('st-minutes').textContent = st.minutes.toLocaleString(LOCALE);
  $('st-songs').textContent = st.songs.toLocaleString(LOCALE);
  const li = (main, sub, n, click) => {
    const el = document.createElement('li');
    const s = document.createElement('span');
    s.textContent = main;
    if (sub) { const sm = document.createElement('small'); sm.textContent = ` · ${sub}`; s.append(sm); }
    const b = document.createElement('b');
    b.textContent = n;
    el.append(s, b);
    el.addEventListener('click', click);
    return el;
  };
  const none = (text) => { const el = document.createElement('li'); el.className = 'hint'; el.textContent = text; el.style.cursor = 'default'; return el; };
  $('st-top-songs').replaceChildren(...(st.top_songs.length ? st.top_songs.map((s) => li(s.title, s.artist, s.plays, () => playFromLibrary(s.id)))
    : [none(tr('Nada todavía en este período.'))]));
  $('st-top-artists').replaceChildren(...(st.top_artists.length ? st.top_artists.map((a) => li(a.artist, '', a.plays, () => selectMusicView('artist', { value: a.artist, name: a.artist, back: 'stats' })))
    : [none(tr('Nada todavía en este período.'))]));
  const max = Math.max(1, ...st.hours);
  $('st-hours').replaceChildren(...st.hours.map((n, h) => { const i = document.createElement('i'); i.style.height = `${(n / max) * 100}%`; i.title = `${h} h: ${n}`; return i; }));
  $('st-new').textContent = st.new_artists.length ? tr('Artistas nuevos para ti: {new_artists}{x}.', { new_artists: st.new_artists.slice(0, 12).join(', '), x: st.new_artists.length > 12 ? '…' : '' }) : '';
  $('st-since').textContent = st.since_log ? tr('Cuenta desde el {x}, cuando Bakin empezó a guardar la hora de cada tema.', { x: new Date(st.since_log * 1000).toLocaleDateString(LOCALE) })
    : tr('Bakin empieza a contar desde ahora: escucha algo y vuelve.');
}

// «Limpiar títulos y artistas…»: Bakin's proposals (and MusicBrainz's, if allowed), ticked by
// the user; «Aplicar» writes them, «Deshacer lo último» puts them back.
const CL = { rows: new Map() };
async function openClean(ids) {
  let r;
  try { r = await call('music_clean_propose', ids ? { ids } : {}); } catch (err) { toast(tr(String(err))); return; }
  CL.ids = ids || null;
  CL.rows.clear();
  $('cl-list').replaceChildren(...r.items.map(cleanRow));
  $('cl-mb').hidden = !L.musicbrainz;
  $('cl-mb-status').textContent = '';
  if (!r.items.length) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = L.musicbrainz ? tr('Nada que limpiar a ojo. Puedes probar «Buscar en MusicBrainz» con los temas seleccionados.') : tr('Nada que limpiar: los títulos y artistas ya están bien.');
    $('cl-list').append(p);
  }
  syncClean(r.undo);
  $('clean-dialog').show();
}

function cleanRow(p) {
  const row = document.createElement('label');
  row.className = 'cl-row';
  row.setAttribute('role', 'listitem');
  const check = document.createElement('input');
  check.type = 'checkbox';
  check.className = 'gm-check';
  check.checked = p.confidence === 'high';
  check.addEventListener('change', () => syncClean());
  const before = document.createElement('span');
  before.className = 'cl-before';
  before.textContent = `${p.before.artist || '—'} · ${p.before.title || p.name}`;
  before.title = p.name;
  const after = document.createElement('span');
  after.className = 'cl-after';
  row.append(check, before, after);
  const data = { id: p.id, after: { ...p.after }, release: null, check, after_el: after, row };
  drawAfter(data, p.why, p.confidence === 'high' ? '' : 'revisar', p.featuring);
  CL.rows.set(p.id, data);
  return row;
}

function drawAfter(d, why, badge, featuring = []) {
  const strong = document.createElement('strong');
  strong.textContent = d.after.title;
  const span = document.createElement('span');
  span.textContent = [d.after.artist, d.after.album, d.after.year].filter(Boolean).join(' · ');
  const small = document.createElement('small');
  small.textContent = [tr(why), featuring.length ? tr('con {names}', { names: featuring.join(', ') }) : ''].filter(Boolean).join(' · ');
  d.after_el.replaceChildren(strong, span, small);
  if (badge) {
    const b = document.createElement('span');
    b.className = 'gm-badge';
    b.dataset.tone = badge === 'revisar' ? 'warning' : 'info';
    b.textContent = tr(badge);
    d.after_el.append(b);
  }
}

function syncClean(undo) {
  const n = [...CL.rows.values()].filter((d) => d.check.checked).length;
  $('cl-count').textContent = CL.rows.size ? tr('{n} de {total} tildadas', { n, total: CL.rows.size }) : '';
  $('cl-apply').disabled = !n;
  $('cl-apply').textContent = n ? tr('Aplicar ({n})', { n }) : tr('Aplicar');
  if (undo !== undefined) {
    $('cl-undo').hidden = !undo;
    if (undo) $('cl-undo').textContent = tr('Deshacer lo último ({count})', { count: undo.count });
  }
}

// MusicBrainz answers song by song (engine events)
function onMusicBrainz(ev) {
  if (ev.kind === 'mb_done') {
    $('cl-mb').disabled = false;
    $('cl-mb').textContent = tr('Buscar en MusicBrainz');
    $('cl-mb-status').textContent = tr(ev.note) || tr('MusicBrainz: {found} de {count} encontrados.', { found: ev.found, count: ev.count });
    syncClean();
    return;
  }
  $('cl-mb-status').textContent = tr('MusicBrainz: {index} de {count}…', { index: ev.index, count: ev.count });
  if (!ev.after) return;
  let d = CL.rows.get(ev.id);
  if (!d) {  // a song without a proposal of Bakin's own: add it
    const row = cleanRow({ id: ev.id, name: '', before: ev.before, after: ev.after, featuring: [], confidence: 'low', why: '' });
    $('cl-list').querySelector(':scope > p.hint')?.remove();
    $('cl-list').prepend(row);
    d = CL.rows.get(ev.id);
  }
  d.after = { artist: ev.after.artist, title: ev.after.title, album: ev.after.album || '', year: ev.after.year ? String(ev.after.year) : null };
  d.release = ev.cover ? ev.release : null;
  d.check.checked = true;
  drawAfter(d, ev.cover ? tr('con la portada oficial') : '', 'MusicBrainz');
  syncClean();
}

function wireClean() {
  $('mus-more').addEventListener('click', () => $('mus-menu').open());
  $('mus-menu').addEventListener('gm-select', (e) => {
    const ids = L.selecting && L.selected.size ? [...L.selected] : undefined;
    if (e.detail.value === 'clean') openClean(ids);
    if (e.detail.value === 'dupes') openDupes();
  });
  $('cl-all').addEventListener('click', () => { for (const d of CL.rows.values()) d.check.checked = true; syncClean(); });
  $('cl-none').addEventListener('click', () => { for (const d of CL.rows.values()) d.check.checked = false; syncClean(); });
  $('cl-close').addEventListener('click', () => { call('music_mb_cancel').catch(() => {}); $('clean-dialog').close(); });
  $('cl-mb').addEventListener('click', async () => {
    const ids = CL.ids || [...CL.rows.values()].filter((d) => d.check.checked).map((d) => d.id);
    const r = await call('music_mb_start', { ids }).catch((e) => ({ ok: false, error: String(e) }));
    if (!r.ok) { toast(tr(r.error), { duration: 6000 }); return; }
    $('cl-mb').disabled = true;
    $('cl-mb').textContent = tr('Buscando…');
    $('cl-mb-status').textContent = tr('MusicBrainz: 0 de {count} (uno por segundo)…', { count: r.count });
  });
  $('cl-apply').addEventListener('click', async () => {
    const changes = [...CL.rows.values()].filter((d) => d.check.checked)
      .map((d) => ({ id: d.id, ...d.after, release: d.release }));
    $('cl-apply').disabled = true;
    const r = await call('music_clean_apply', { changes, playing: plCur()?.id || '' }).catch((e) => ({ ok: false, message: String(e) }));
    toast(tr(r.message), { duration: 7000, ...(r.done?.length ? { action: { label: tr('Deshacer'), onClick: undoTags } } : {}) });
    $('clean-dialog').close();
    refreshLibrary(false, { inPlace: true });
  });
  $('cl-undo').addEventListener('click', async () => { await undoTags(); $('clean-dialog').close(); });
  $('mb-on').addEventListener('change', async () => {
    L.musicbrainz = $('mb-on').checked;
    await call('music_mb_enable', { on: L.musicbrainz }).catch(() => {});
  });
  // Duplicados
  $('du-close').addEventListener('click', () => $('dupes-dialog').close());
  $('du-apply').addEventListener('click', resolveDupes);
}

async function undoTags() {
  const r = await call('music_tags_undo').catch((e) => ({ ok: false, message: String(e) }));
  toast(tr(r.message));
  refreshLibrary(false, { inPlace: true });
}

// «Buscar duplicados…»: one copy of each stays (the best marked), the rest to the Recycle Bin;
// their plays, favourite and playlist places go to the one kept.
let DU = [];
async function openDupes() {
  let r;
  try { r = await call('music_dupes'); } catch (err) { toast(tr(String(err))); return; }
  DU = r.groups;
  $('du-lead').textContent = DU.length ? tn('{n} grupo de temas repetidos. Queda el marcado (el de mejor calidad); los demás van a la Papelera, y sus reproducciones, favorita y playlists pasan al que queda.',
       '{n} grupos de temas repetidos. Queda el marcado (el de mejor calidad); los demás van a la Papelera, y sus reproducciones, favorita y playlists pasan al que queda.', DU.length)
    : tr('No hay temas repetidos.');
  $('du-apply').hidden = !DU.length;
  const fmt = (i) => `${(i.codec || '').toUpperCase()}${i.bitrate ? ` ${i.bitrate} kbps` : ''}${i.plays ? ` · ${i.plays} ${i.plays === 1 ? tr('vez') : tr('veces')}` : ''}${i.fav ? ' · ♥' : ''}`;
  $('du-list').replaceChildren(...DU.map((g, n) => {
    const box = document.createElement('div');
    box.className = 'du-group';
    const head = document.createElement('strong');
    head.textContent = `${g.items[0].artist || '—'} · ${g.items[0].title || g.items[0].name}`;
    head.title = g.why;
    box.append(head);
    for (const it of g.items) {
      const lab = document.createElement('label');
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = `du-${n}`;
      radio.value = it.id;
      radio.checked = it.id === g.best;
      const name = document.createElement('span');
      name.textContent = it.name;
      name.title = it.path;
      const info = document.createElement('small');
      info.textContent = fmt(it);
      lab.append(radio, name, info);
      box.append(lab);
    }
    return box;
  }));
  $('dupes-dialog').show();
}

async function resolveDupes() {
  $('du-apply').disabled = true;
  let kept = 0;
  const gone = [];
  for (const [n, g] of DU.entries()) {
    const keep = $('du-list').querySelector(`input[name="du-${n}"]:checked`)?.value || g.best;
    const drop = g.items.map((i) => i.id).filter((id) => id !== keep);
    const r = await call('music_dupes_resolve', { keep, drop }).catch(() => null);
    if (r?.deleted) { kept += 1; gone.push(...drop); }
  }
  $('du-apply').disabled = false;
  $('dupes-dialog').close();
  toast(tn('{n} grupo resuelto', '{n} grupos resueltos', kept) + tn(': {n} copia a la Papelera.', ': {n} copias a la Papelera.', gone.length),
    { duration: 8000, ...(gone.length ? { action: { label: tr('Deshacer'), onClick: () => restoreItems(gone) } } : {}) });
  refreshLibrary(false, { inPlace: true });
}

// ------------------------------- «Descargar» › «Buscar música» (PLAN 0.15.1)
// YouTube Music's catalogue, browsed like Wallhaven: songs as rows to tick, albums, artists and
// playlists as tiles that open their page. Covers come from the engine (no remote images here).
const CAT = { kind: 'songs', picked: new Map(), results: null, page: null };
const BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';  // until the cover comes

function openCatalog() {
  openHub('catalog');
  requestAnimationFrame(() => $('cat-q').focus());
}

async function catalogSearch() {
  const q = $('cat-q').value.trim();
  if (!q) return;
  CAT.page = null;
  $('cat-page-head').hidden = true;
  $('cat-status').textContent = tr('Buscando…');
  const r = await call('catalog_search', { q, kind: CAT.kind }).catch((e) => ({ ok: false, error: String(e) }));
  if (!r.ok) { $('cat-status').textContent = tr(r.error); return; }
  CAT.results = { kind: CAT.kind, items: r.items };
  drawCatalog(CAT.kind === 'songs' ? r.items : [], CAT.kind === 'songs' ? [] : r.items);
  $('cat-status').textContent = r.items.length ? '' : tr('Nada con esas palabras.');
}

async function catalogOpen(card) {
  $('cat-status').textContent = tr('Abriendo…');
  const r = await call('catalog_page', { kind: card.kind, id: card.id }).catch((e) => ({ ok: false, error: String(e) }));
  if (!r.ok) { $('cat-status').textContent = tr(r.error); return; }
  const p = r.page;
  CAT.page = p;
  $('cat-page-head').hidden = false;
  $('cat-page-title').textContent = p.title;
  $('cat-page-sub').textContent = p.kind === 'artist' ? (p.subscribers ? `${p.subscribers} suscriptores` : tr('Artista'))
    : [p.type ? tr(p.type) : p.kind === 'playlist' ? 'Playlist' : '', p.artist, p.year, tr('{n} temas', { n: p.tracks.length })].filter(Boolean).join(' · ');
  $('cat-page-art').src = BLANK;
  delete $('cat-page-art').dataset.loaded;
  $('cat-page-art').dataset.thumb = p.thumb || '';
  $('cat-page-all').hidden = p.kind === 'artist';
  $('cat-status').textContent = '';
  drawCatalog(p.kind === 'artist' ? p.songs : p.tracks, p.kind === 'artist' ? p.albums : []);
}

function catKey(c) { return `${c.kind}:${c.id}`; }

function drawCatalog(songs, tiles) {
  const dur = (s) => (s ? fmtDur(s) : '');
  $('cat-songs').replaceChildren(...songs.map((c) => {
    const row = document.createElement('label');
    row.className = 'cat-song';
    row.setAttribute('role', 'listitem');
    row.dataset.have = String(!!c.have);
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.className = 'gm-check';
    check.checked = CAT.picked.has(catKey(c));
    check.disabled = !c.available;
    check.addEventListener('change', () => { if (check.checked) CAT.picked.set(catKey(c), c); else CAT.picked.delete(catKey(c)); syncCatalog(); });
    const img = document.createElement('img');
    img.alt = '';
    img.src = BLANK;
    img.dataset.thumb = c.thumb || '';
    const text = document.createElement('span');
    text.className = 'cat-text';
    const t = document.createElement('strong');
    t.textContent = c.title;
    const sub = document.createElement('small');
    sub.textContent = [c.artist, c.album].filter(Boolean).join(' · ');
    text.append(t, sub);
    const badges = document.createElement('span');
    if (c.have) { const b = document.createElement('span'); b.className = 'gm-badge'; b.dataset.tone = 'success'; b.textContent = tr('ya la tienes'); badges.append(b); }
    if (c.explicit) { const b = document.createElement('span'); b.className = 'gm-badge'; b.textContent = 'E'; b.title = tr('Explícito'); badges.append(b); }
    const d = document.createElement('span');
    d.className = 'cat-dur';
    d.textContent = dur(c.duration);
    row.append(check, img, text, badges, d);
    return row;
  }));
  $('cat-albums-title').hidden = !(CAT.page?.kind === 'artist' && tiles.length);
  $('cat-tiles').replaceChildren(...tiles.map((c) => {
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'cat-tile';
    tile.dataset.kind = c.kind;
    tile.setAttribute('role', 'listitem');
    const img = document.createElement('img');
    img.alt = '';
    img.src = BLANK;
    img.dataset.thumb = c.thumb || '';
    const t = document.createElement('strong');
    t.textContent = c.title;
    const sub = document.createElement('small');
    sub.textContent = c.kind === 'artist' ? tr('Artista') : [c.type && tr(c.type), c.artist, c.year, c.count ? tr('{n} temas', { n: c.count }) : ''].filter(Boolean).join(' · ');
    tile.append(img, t, sub);
    tile.addEventListener('click', () => catalogOpen(c));
    return tile;
  }));
  $('cat-scroll').scrollTop = 0;
  syncCatalog();
  loadCatalogThumbs();
}

// the covers, from the engine in one go
async function loadCatalogThumbs() {
  if (L.catalogCovers === false) return;  // Configuración › Rendimiento
  const imgs = [...document.querySelectorAll('#download-hub img[data-thumb]:not([data-loaded])')].filter((i) => i.dataset.thumb);
  if (!imgs.length) return;
  const r = await call('catalog_thumbs', { urls: [...new Set(imgs.map((i) => i.dataset.thumb))] }).catch(() => null);
  for (const i of imgs) if (r?.thumbs?.[i.dataset.thumb]) { i.src = r.thumbs[i.dataset.thumb]; i.dataset.loaded = ''; }
}

function syncCatalog() {
  const n = CAT.picked.size;
  $('cat-picked').textContent = n ? tn('{n} elegido', '{n} elegidos', n) : '';
  $('cat-download').disabled = !n;
  $('cat-download').textContent = n ? tr('Bajar ({n})', { n }) : tr('Bajar');
}

async function catalogDownload(items) {
  const r = await call('catalog_download', { items }).catch((e) => ({ ok: false, message: String(e) }));
  toast(tr(r.message), { duration: 6000, ...(r.ok ? { action: { label: tr('Ver'), onClick: () => { $('download-hub').close(); setView('downloads'); } } } : {}) });
  if (r.ok) { CAT.picked.clear(); for (const c of document.querySelectorAll('#cat-songs input')) c.checked = false; syncCatalog(); }
}

function wireCatalog() {
  chipGroup($('cat-kind'), (v) => { CAT.kind = v; if ($('cat-q').value.trim()) catalogSearch(); });
  $('cat-go').addEventListener('click', catalogSearch);
  $('cat-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); catalogSearch(); } });
  $('cat-back').addEventListener('click', () => {
    CAT.page = null;
    $('cat-page-head').hidden = true;
    const r = CAT.results;
    if (r) drawCatalog(r.kind === 'songs' ? r.items : [], r.kind === 'songs' ? [] : r.items);
  });
  $('cat-page-all').addEventListener('click', () => CAT.page && catalogDownload([{ kind: CAT.page.kind, id: CAT.page.id, playlist: CAT.page.playlist }]));
  $('cat-download').addEventListener('click', () => catalogDownload([...CAT.picked.values()]));
}

// ---------------------------------------- Spotify → YouTube Music (0.15.2)
// The songs of a Spotify link, each found on YouTube Music: the sure ones ticked, the doubtful
// ones shown with what else came up, the ones not found say so.
const SP = { rows: [] };
async function openSpotify(url, csvText = '') {
  const r = await call('spotify_resolve', csvText ? { csv_text: csvText } : { url }).catch((e) => ({ ok: false, error: String(e) }));
  if (!r.ok) { toast(tr(r.error), { duration: 7000 }); return; }
  $('download-hub').close();
  SP.rows = r.tracks.map((t) => ({ track: t, result: null, pick: null }));
  $('sp-lead').textContent = (r.name ? `«${r.name}»: ` : '') + tn('{n} tema. De Spotify se toman solo los nombres; cada uno se busca y se baja de YouTube Music.',
                                        '{n} temas. De Spotify se toman solo los nombres; cada uno se busca y se baja de YouTube Music.', r.count)
    + (r.limited ? tr(' Spotify muestra solo los primeros 100 de una playlist: para el resto, «Importar CSV…».') : '');
  $('sp-list').replaceChildren(...SP.rows.map(spRow));
  $('sp-status').textContent = tr('Buscando…');
  syncSpotify();
  $('spotify-dialog').show();
}

function spRow(row, n) {
  const el = document.createElement('label');
  el.className = 'sp-row';
  el.setAttribute('role', 'listitem');
  const check = document.createElement('input');
  check.type = 'checkbox';
  check.className = 'gm-check';
  check.disabled = true;
  check.addEventListener('change', syncSpotify);
  const from = document.createElement('span');
  from.className = 'sp-from';
  const ft = document.createElement('span');
  ft.textContent = row.track.title;
  const fa = document.createElement('small');
  fa.textContent = [row.track.artists.join(', '), row.track.duration ? fmtDur(row.track.duration) : ''].filter(Boolean).join(' · ');
  from.append(ft, fa);
  const to = document.createElement('span');
  to.className = 'sp-to pending';
  to.textContent = '…';
  const extra = document.createElement('span');
  el.append(check, from, to, extra);
  Object.assign(row, { el, check, to, extra, n });
  return el;
}

function drawSpMatch(row) {
  const r = row.result;
  const c = row.pick;
  row.to.classList.remove('pending');
  row.extra.replaceChildren();
  if (!c) {
    row.to.textContent = tr(r.error) || tr('No está en YouTube Music');
    row.check.checked = false;
    row.check.disabled = true;
  } else {
    const t = document.createElement('span');
    t.textContent = c.title;
    const s = document.createElement('small');
    s.textContent = [c.artist, c.duration ? fmtDur(c.duration) : '', row.pick === r.match ? tr(r.why) : tr('elegido a mano')].filter(Boolean).join(' · ');
    row.to.replaceChildren(t, s);
    row.check.disabled = false;
  }
  const options = [r.match, ...(r.alternatives || [])].filter(Boolean);
  if (options.length > 1 || (!r.match && options.length)) {
    const sel = document.createElement('select');
    sel.className = 'gm-input';
    sel.title = tr('Elegir otro resultado');
    sel.append(new Option(r.match ? tr('Otro…') : tr('Elegir…'), ''));
    options.forEach((o, i) => sel.append(new Option(`${o.title} · ${o.artist}${o.duration ? ` · ${fmtDur(o.duration)}` : ''}`, String(i))));
    sel.addEventListener('change', () => {
      if (sel.value === '') return;
      row.pick = options[Number(sel.value)];
      row.check.checked = true;
      drawSpMatch(row);
      syncSpotify();
    });
    row.extra.append(sel);
  }
  if (r.verdict === 'doubt' && row.pick === r.match) {
    const b = document.createElement('span');
    b.className = 'gm-badge';
    b.dataset.tone = 'warning';
    b.textContent = tr('revisar');
    row.extra.prepend(b);
  }
  if (c?.have) {
    const b = document.createElement('span');
    b.className = 'gm-badge';
    b.dataset.tone = 'success';
    b.textContent = tr('ya la tienes');
    row.extra.prepend(b);
  }
}

function onSpotify(ev) {
  if (ev.kind === 'sp_done') {
    $('sp-status').textContent = (ev.note ? tr(ev.note) : tr('{found} de {count} encontrados.', { found: ev.found, count: ev.count }));
    syncSpotify();
    return;
  }
  const row = SP.rows[ev.index];
  if (!row) return;
  row.result = ev;
  row.pick = ev.match || null;
  row.check.checked = ev.verdict === 'sure' && !ev.match?.have;
  drawSpMatch(row);
  $('sp-status').textContent = tr('Buscando {x} de {count}…', { x: ev.index + 1, count: ev.count });
  syncSpotify();
}

function syncSpotify() {
  const n = SP.rows.filter((r) => r.check?.checked && r.pick).length;
  $('sp-count').textContent = tr('{n} de {rows} para bajar', { n, rows: SP.rows.length });
  $('sp-download').disabled = !n;
  $('sp-download').textContent = n ? tr('Bajar ({n})', { n }) : tr('Bajar');
}

function wireSpotify() {
  $('sp-close').addEventListener('click', () => { call('spotify_cancel').catch(() => {}); $('spotify-dialog').close(); });
  $('sp-download').addEventListener('click', async () => {
    const items = SP.rows.filter((r) => r.check?.checked && r.pick).map((r) => r.pick);
    await catalogDownload(items);
    $('spotify-dialog').close();
  });
  $('sp-csv').addEventListener('click', async () => {
    const f = await tauri.dialog.open({ title: tr('CSV exportado de Spotify'), filters: [{ name: 'CSV', extensions: ['csv'] }] });
    if (typeof f !== 'string') return;
    const r = await call('read_text_file', { path: f }).catch((e) => ({ ok: false, error: String(e) }));
    if (!r.ok) { toast(tr(r.error)); return; }
    call('spotify_cancel').catch(() => {});
    $('spotify-dialog').close();
    setTimeout(() => openSpotify('', r.text), 700);
  });
}

// ------------------------------------------------------------ sound (0.16)
// Even volume: the engine measures each song once (ffmpeg, EBU R128) and gives the player a gain
// per song; the EQ, the output device and A–B live in the player.
const EQ_PRESETS = { flat: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0], bass: [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
  vocal: [-2, -2, -1, 0, 2, 4, 4, 2, 0, -1], treble: [0, 0, 0, 0, 0, 1, 2, 4, 5, 6] };
let ldMode;

async function pushGains() {
  // the songs the player has now get the gains of the mode chosen
  const q = await tauri.core.invoke('player_call', { cmd: { cmd: 'queue' } }).catch(() => null);
  const ids = q?.tracks?.map((t) => t.id) || [];
  const r = ids.length ? await call('music_gains', { ids }).catch(() => null) : null;
  await pc({ cmd: 'gains', gains: Object.fromEntries(ids.map((id) => [id, r?.gains?.[id] ?? null])) });
  await pc({ cmd: 'set', loudness: (L.sound?.mode || 'off') !== 'off' });
}

function syncSoundSettings() {
  ldMode.set(L.sound?.mode || 'off');
  $('ld-preamp').value = String(L.sound?.preamp || 0);
  $('ld-preamp').closest('gm-slider')?.sync?.();
  $('ld-preamp-label').textContent = `+${$('ld-preamp').value} dB`;
  const eq = P.state.eq || { on: false, gains: EQ_PRESETS.flat };
  $('eq-on').checked = !!eq.on;
  for (const inp of document.querySelectorAll('#eq input')) { inp.value = String(eq.gains[Number(inp.dataset.band)] || 0); inp.closest('gm-slider')?.sync?.(); }
  pc({ cmd: 'devices' }).then((r) => {
    const sel = $('out-device');
    sel.replaceChildren(new Option(tr('Predeterminado de Windows'), ''), ...(r?.devices || []).map((d) => new Option(d, d)));
    setSel(sel, r?.device || '');
  });
}

function sendEq() {
  const gains = [...document.querySelectorAll('#eq input')].map((i) => Number(i.value));
  pc({ cmd: 'set', eq: { on: $('eq-on').checked, gains } });
}

// the lyrics panel: the line that plays, lit and in view
const LY = { id: null, lines: [], at: -1 };
async function loadLyrics() {
  const t = plCur();
  if ($('lyrics-panel').hidden) return;
  if (!t) { showLyrics(null, tr('Nada sonando.')); return; }
  if (LY.id === t.id) return;
  LY.id = t.id;
  $('ly-title').textContent = t.title || t.name;
  const r = await call('music_lyrics', { id: t.id }).catch(() => null);
  if (LY.id !== t.id) return;
  if (r?.found) showLyrics(r);
  else showLyrics(null, r?.online ? tr('Este tema no tiene letra guardada.') : tr('Este tema no tiene letra guardada. Para buscarlas en internet: Configuración › Música › «Buscar letras en LRCLIB».'), !!r?.online);
}

function showLyrics(r, empty = '', canFetch = false) {
  LY.lines = r?.synced || [];
  LY.at = -1;
  const box = $('ly-lines');
  box.classList.toggle('plain', !LY.lines.length);
  const lines = LY.lines.length ? LY.lines.map(([t, text]) => [t, text]) : (r?.plain || '').split('\n').map((text) => [null, text]);
  box.replaceChildren(...lines.map(([t, text]) => {
    const p = document.createElement('p');
    p.textContent = text || '♪';
    if (t !== null) p.addEventListener('click', () => pc({ cmd: 'seek', time: t }));
    return p;
  }));
  const none = !r || (!LY.lines.length && !(r.plain || '').trim());
  box.hidden = none;
  $('ly-empty').hidden = !none;
  $('ly-empty-text').textContent = r?.instrumental ? tr('Instrumental: sin letra.') : empty;
  $('ly-fetch').hidden = !canFetch;
  syncLyricsTime();
}

function syncLyricsTime() {
  if ($('lyrics-panel').hidden || !LY.lines.length) return;
  const t = (P.state.time || 0) + 0.25;
  let i = -1;
  for (let k = 0; k < LY.lines.length && LY.lines[k][0] <= t; k++) i = k;
  if (i === LY.at) return;
  LY.at = i;
  const ps = $('ly-lines').children;
  for (const p of ps) p.classList.remove('now');
  if (ps[i]) { ps[i].classList.add('now'); ps[i].scrollIntoView({ block: 'center', behavior: 'smooth' }); }
}

function wireSound() {
  ldMode = chipGroup($('ld-mode'), async (v) => {
    L.sound = { ...(L.sound || {}), mode: v };
    await call('settings_set', { changes: { loudness_mode: v } });
    if (v !== 'off') call('music_loudness_scan').catch(() => {});
    pushGains();
  });
  $('ld-preamp').addEventListener('input', () => { $('ld-preamp-label').textContent = `+${$('ld-preamp').value} dB`; });
  $('ld-preamp').addEventListener('change', async () => {
    L.sound = { ...(L.sound || {}), preamp: Number($('ld-preamp').value) };
    await call('settings_set', { changes: { loudness_preamp: L.sound.preamp } });
    pushGains();
  });
  $('eq-on').addEventListener('change', sendEq);
  for (const inp of document.querySelectorAll('#eq input')) inp.addEventListener('change', sendEq);
  for (const b of document.querySelectorAll('#eq-presets button')) {
    b.addEventListener('click', () => {
      EQ_PRESETS[b.value].forEach((g, i) => { const inp = document.querySelector(`#eq input[data-band="${i}"]`); inp.value = String(g); inp.closest('gm-slider')?.sync?.(); });
      $('eq-on').checked = b.value !== 'flat';
      sendEq();
    });
  }
  $('out-device').addEventListener('change', () => pc({ cmd: 'set', device: $('out-device').value }));
  $('ly-online').addEventListener('change', () => call('settings_set', { changes: { lyrics_online: $('ly-online').checked } }));
  $('ly-beside').addEventListener('change', () => call('settings_set', { changes: { lyrics_beside: $('ly-beside').checked } }));
  // lyrics
  $('pl-lyrics').addEventListener('click', () => {
    const show = $('lyrics-panel').hidden;
    $('lyrics-panel').hidden = !show;
    $('pl-lyrics').setAttribute('aria-pressed', String(show));
    LY.id = null;
    if (show) loadLyrics();
  });
  $('ly-close').addEventListener('click', () => $('pl-lyrics').click());
  $('ly-fetch').addEventListener('click', async () => {
    const t = plCur();
    if (!t) return;
    $('ly-fetch').disabled = true;
    const r = await call('music_lyrics_fetch', { id: t.id }).catch((e) => ({ ok: false, error: String(e) }));
    $('ly-fetch').disabled = false;
    if (!r.ok) { toast(tr(r.error), { duration: 6000 }); return; }
    if (r.found) showLyrics(r); else showLyrics(null, tr('LRCLIB tampoco la tiene.'));
  });
  // A–B
  let abA = null;
  const abLabel = () => {
    const ab = P.state.ab;
    $('ab-off').hidden = !ab && abA === null;
    $('ab-b').disabled = abA === null;
    $('ab-label').textContent = ab ? `${fmtDur(ab[0])} → ${fmtDur(ab[1])}` : abA !== null ? `desde ${fmtDur(abA)}` : '';
  };
  $('ab-a').addEventListener('click', () => { abA = P.state.time || 0; abLabel(); });
  $('ab-b').addEventListener('click', async () => {
    const b = P.state.time || 0;
    if (abA !== null && b > abA + 1) { await pc({ cmd: 'set', ab: [abA, b] }); abA = null; }
    abLabel();
  });
  $('ab-off').addEventListener('click', async () => { abA = null; await pc({ cmd: 'set', ab: null }); abLabel(); });
  $('pl-more').addEventListener('click', abLabel);
}

function leaveMusicView() {
  if (!L.mview) return;
  L.mview = null;
  renderMusicCard();
}

// Artists and albums: a grid of cards, covers loaded as they scroll in.
const groupCovers = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const box = e.target;
    groupCovers.unobserve(box);
    call('music_cover', { item_id: box.dataset.cover, size: 320 }).then((r) => {
      if (!r.cover) return;
      const img = new Image();
      img.alt = '';
      img.decoding = 'async';
      img.src = r.cover;
      box.replaceChildren(img);
    }).catch(() => {});
  }
}, { rootMargin: '400px' });

async function renderGroups(seq) {
  const by = L.mview.view === 'artists' ? 'artist' : 'album';
  let res;
  try { res = await call('music_groups', { by, filters: { q: L.q } }); } catch (err) { toast(tr(String(err))); return; }
  if (seq !== L.seq) return;
  const box = $('lib-groups');
  box.replaceChildren(...res.groups.map((g) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'group-card';
    b.setAttribute('role', 'listitem');
    const art = document.createElement('span');
    art.className = 'group-art';
    art.dataset.cover = g.cover_id;
    art.innerHTML = ICONS.audio;
    groupCovers.observe(art);
    const name = document.createElement('strong');
    name.textContent = g.name || (by === 'artist' ? tr('Sin artista') : tr('Sin álbum'));
    const sub = document.createElement('small');
    sub.textContent = [by === 'album' ? g.artist : '', tn('{n} tema', '{n} temas', g.count),
      g.plays ? `${g.plays} reproducciones` : ''].filter(Boolean).join(' · ');
    b.append(art, name, sub);
    b.addEventListener('click', () => selectMusicView(by, {
      value: g.name, artist: by === 'album' ? g.artist : '', back: L.mview.view,
      name: g.name || (by === 'artist' ? tr('Sin artista') : tr('Sin álbum')),
    }));
    return b;
  }));
  showCount(`${res.groups.length} ${by === 'artist' ? tr('artistas') : tr('álbumes')}`);
  $('lib-empty').hidden = res.groups.length > 0;
  $('btn-lib-more').hidden = true;
}

async function playList(shuffle) {
  let res;
  try { res = await call('music_tracks', { filters: libFilters() }); } catch (err) { toast(tr(String(err))); return; }
  if (!res.tracks.length) { toast(tr('No hay temas en esta lista.')); return; }
  if (!!P.state.shuffle !== shuffle) await pc({ cmd: 'set', shuffle });
  plSetQueue(res.tracks, shuffle ? Math.floor(Math.random() * res.tracks.length) : 0, libTitle());
}

// -------------------------------------------------------- playlists
const RULE_FIELDS = [
  ['artist', tr('Artista'), 'text'], ['title', tr('Título'), 'text'], ['album', tr('Álbum'), 'text'], ['name', tr('Nombre de archivo'), 'text'],
  ['codec', tr('Formato'), 'text'], ['plays', tr('Reproducciones'), 'number'], ['minutes', tr('Duración (minutos)'), 'number'],
  ['bitrate', tr('Bitrate (kbps)'), 'number'], ['added_days', tr('Agregada'), 'days'], ['played_days', tr('Escuchada'), 'days'],
  ['fav', tr('Favorita'), 'bool'],
];
const RULE_OPS = {
  text: [['contains', tr('contiene')], ['not_contains', tr('no contiene')], ['is', tr('es exactamente')]],
  number: [['gte', tr('al menos')], ['lte', tr('como mucho')]],
  days: [['within', tr('en los últimos … días')], ['not_within', tr('no en los últimos … días')]],
  bool: [['is', tr('es')]],
};
let pdEditing = null;
let pdKind;
let pdMatch;
let pdPreviewTimer;

function ruleRow(rule = { field: 'artist', op: 'contains', value: '' }) {
  const row = document.createElement('div');
  row.className = 'pd-rule';
  const field = document.createElement('select');
  field.className = 'gm-input';
  field.setAttribute('aria-label', tr('Campo'));
  for (const [v, label] of RULE_FIELDS) field.add(new Option(label, v));
  field.value = rule.field;
  const op = document.createElement('select');
  op.className = 'gm-input';
  op.setAttribute('aria-label', tr('Condición'));
  const value = document.createElement('span');
  value.className = 'pd-value';
  const drop = document.createElement('button');
  drop.type = 'button';
  drop.className = 'gm-chip gm-chip-plain gm-chip-round';
  drop.setAttribute('aria-label', tr('Quitar regla'));
  drop.textContent = '✕';
  drop.addEventListener('click', () => { row.remove(); pdPreview(); });
  const fill = (keep) => {
    const kind = RULE_FIELDS.find((f) => f[0] === field.value)[2];
    op.replaceChildren(...RULE_OPS[kind].map(([v, label]) => new Option(label, v)));
    if (keep && RULE_OPS[kind].some(([v]) => v === rule.op)) op.value = rule.op;
    let input;
    if (kind === 'bool') {
      input = document.createElement('select');
      input.className = 'gm-input';
      input.add(new Option(tr('sí'), 'true'));
      input.add(new Option(tr('no'), 'false'));
      input.value = keep && rule.value === false ? 'false' : 'true';
    } else {
      input = document.createElement('input');
      input.className = 'gm-input';
      input.type = kind === 'text' ? 'text' : 'number';
      input.spellcheck = false;
      if (kind !== 'text') input.min = '0';
      input.value = keep ? String(rule.value ?? '') : (kind === 'days' ? '30' : kind === 'number' ? '1' : '');
    }
    input.setAttribute('aria-label', tr('Valor'));
    input.addEventListener('input', pdPreview);
    value.replaceChildren(input);
  };
  fill(true);
  field.addEventListener('change', () => { fill(false); pdPreview(); });
  op.addEventListener('change', pdPreview);
  row.append(field, op, value, drop);
  return row;
}

function pdRules() {
  const rules = [...$('pd-rules').querySelectorAll('.pd-rule')].map((row) => {
    const [field, op] = row.querySelectorAll(':scope > select');
    const input = row.querySelector('.pd-value > *');
    const kind = RULE_FIELDS.find((f) => f[0] === field.value)[2];
    const value = kind === 'bool' ? input.value === 'true' : kind === 'text' ? input.value : Number(input.value);
    return { field: field.value, op: op.value, value };
  });
  return { match: pdMatch.value, rules, sort: $('pd-sort').value, limit: Number($('pd-limit').value) || 0 };
}

function pdPreview() {
  clearTimeout(pdPreviewTimer);
  if (pdKind.value !== 'smart') return;
  pdPreviewTimer = setTimeout(async () => {
    try {
      const r = await call('library_items', { filters: { music: { view: 'rules', rules: pdRules() } }, offset: 0, limit: 1 });
      $('pd-preview').textContent = tn('{n} tema cumple las reglas ahora.', '{n} temas cumplen las reglas ahora.', r.total);
    } catch { $('pd-preview').textContent = ''; }
  }, 350);
}

function openPlaylistDialog(pl = null) {
  pdEditing = pl;
  $('playlist-dialog').setAttribute('heading', pl ? tr('Editar «{name}»', { name: pl.name }) : tr('Nueva playlist'));
  $('pd-name').value = pl?.name || '';
  pdKind.set(pl?.kind || 'list');
  for (const c of $('pd-kind').querySelectorAll('.gm-chip')) c.disabled = !!pl;  // a playlist keeps its kind
  const rules = pl?.rules || { match: 'all', rules: [], sort: 'new', limit: 0 };
  pdMatch.set(rules.match);
  $('pd-sort').value = rules.sort;
  $('pd-limit').value = String(rules.limit || 0);
  $('pd-rules').replaceChildren(...(rules.rules.length ? rules.rules : [undefined]).map((r) => ruleRow(r)));
  syncPdKind();
  $('playlist-dialog').show();
  $('pd-name').focus();
}

function syncPdKind() {
  const smart = pdKind.value === 'smart';
  $('pd-smart').hidden = !smart;
  $('pd-list-hint').hidden = smart;
  if (smart) pdPreview();
}

async function savePlaylist() {
  const name = $('pd-name').value.trim();
  if (!name) { toast(tr('Ponle un nombre a la playlist.')); $('pd-name').focus(); return; }
  const kind = pdKind.value;
  const r = await call('music_playlist_save', { name, kind, playlist_id: pdEditing?.id || '', rules: kind === 'smart' ? pdRules() : null })
    .catch((err) => ({ ok: false, error: String(err) }));
  if (!r.ok) { toast(tr(r.error)); return; }
  $('playlist-dialog').close();
  await loadMusicOverview();
  selectMusicView('playlist', { value: r.playlist.id, name: r.playlist.name });
}

let apIds = [];
async function openAddToPlaylist(ids) {
  if (!ids.length) return;
  apIds = ids;
  if (!MUSIC) await loadMusicOverview();
  $('ap-lead').textContent = tn('{n} tema: elige una playlist o crea una nueva.', '{n} temas: elige una playlist o crea una nueva.', ids.length);
  const lists = (MUSIC?.playlists || []).filter((p) => p.kind === 'list');
  $('ap-lists').replaceChildren(...lists.map((p) => {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'collection';
    b.innerHTML = '<span class="collection-art">♪</span>';
    const n = document.createElement('span');
    n.className = 'collection-name';
    n.textContent = p.name;
    const c = document.createElement('small');
    c.textContent = p.count;
    b.append(n, c);
    b.addEventListener('click', () => addToPlaylist(p.id, p.name));
    li.append(b);
    return li;
  }));
  $('ap-new').value = '';
  $('addpl-dialog').show();
}

async function addToPlaylist(id, name) {
  const r = await call('music_playlist_add', { playlist_id: id, ids: apIds }).catch((err) => ({ ok: false, error: String(err) }));
  if (!r.ok) { toast(tr(r.error)); return; }
  $('addpl-dialog').close();
  toast(r.added ? tn('{n} tema agregado a «{name}».', '{n} temas agregados a «{name}».', r.added, { name }) : tr('Ya estaban en «{name}».', { name }));
  loadMusicOverview();
}

// --------------------------------------------------------------- settings
// Every setting lives in Configuración (the ⚙ next to the search), once:
// General, Música, Descargas and Consumo. The sidebars show what
// is going on, not how it is set.
function openSettings(sec = 'general') {
  showSettingsSection(sec);
  $('settings-dialog').show();
}

function showSettingsSection(sec) {
  for (const b of document.querySelectorAll('#settings-nav .scope')) {
    if (b.dataset.sec === sec) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
  }
  for (const el of document.querySelectorAll('#settings-dialog section[data-sec]')) el.hidden = el.dataset.sec !== sec;
  if (sec === 'usage') renderUsage();
  if (sec === 'wallpapers') renderWallpaperSettings();
  if (sec === 'accounts') { renderWallhavenAccount(); renderCookiesVault(); }
  if (sec === 'keys') renderHotkeys();
  if (sec === 'music') syncSoundSettings();
  if (sec === 'music') {
    const fade = $('po-fade');
    fade.value = String(P.state.crossfade || 0);
    fade.closest('gm-slider')?.sync?.();
    $('po-fade-label').textContent = Number(fade.value) ? `${fade.value} s` : tr('sin fundido');
  }
}

// Configuración › Fondos de pantalla (PLAN 0.13.2): a row per monitor. The
// shell keeps and runs the rotation (rotation.rs); the engine only says which
// sources there are (its collections and their favourites).
const WP_EVERY = [[5, tr('cada 5 minutos')], [15, tr('cada 15 minutos')], [30, tr('cada 30 minutos')], [60, tr('cada hora')],
  [180, tr('cada 3 horas')], [720, tr('cada 12 horas')], [1440, tr('cada día')]];
const sameSource = (a, b) => a && b && a.kind === b.kind && JSON.stringify(a.folders) === JSON.stringify(b.folders);

async function renderWallpaperSettings() {
  const list = $('wp-list');
  let r, src;
  try {
    [r, src] = await Promise.all([tauri.core.invoke('wallpaper_rotation_get'), call('wallpaper_sources')]);
  } catch (err) {
    list.replaceChildren(Object.assign(document.createElement('p'), { className: 'hint', textContent: String(err) }));
    return;
  }
  $('wp-paused').checked = r.paused;
  list.replaceChildren(...r.monitors.map((m) => wallpaperRow(m, src.sources)));
}

function wallpaperRow(m, sources) {
  const fresh = !m.rot;
  // a new row starts from Wallhaven's folder, or the favourites
  const rot = m.rot || { on: false, source: sources.find((x) => /wallhaven/i.test(x.label)) || sources[0], every_min: 30, order: 'random', fit: true };
  const options = [...sources];
  if (!options.some((x) => sameSource(x, rot.source))) options.push(rot.source);  // a folder no longer in the library
  const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
  const select = (pairs, value, label) => {
    const sel = el('select', { ariaLabel: label });
    for (const [v, t] of pairs) sel.append(el('option', { value: String(v), textContent: t }));
    sel.value = String(value);
    const wrap = el('gm-select');
    wrap.append(sel);
    return [wrap, sel];
  };

  const box = el('div', { className: 'wp-mon' });
  box.dataset.key = m.key;
  const head = el('label', { className: 'gm-toggle-row' });
  const text = el('span', { className: 'gm-toggle-row-text' });
  text.append(el('strong', { textContent: tr('Monitor {number} · {w}×{h}', { number: m.number, w: m.w, h: m.h }) + (m.vertical ? tr(' (vertical)') : '') }),
    el('small', { className: 'wp-now', textContent: m.current ? `Ahora: ${m.current.split(/[\\/]/).pop()}` : (fresh ? tr('Sin rotación') : tr('Todavía sin fondo de Bakin')) }));
  const on = el('input', { type: 'checkbox', className: 'gm-toggle', checked: rot.on });
  on.setAttribute('role', 'switch');
  on.setAttribute('aria-label', tr('Rotar el fondo del monitor {number}', { number: m.number }));
  head.append(text, on);

  const opts = el('div', { className: 'wp-opts' });
  const [srcWrap, srcSel] = select(options.map((x, i) => [i, tr(x.label)]), options.findIndex((x) => sameSource(x, rot.source)), tr('De dónde'));
  const [everyWrap, everySel] = select(WP_EVERY.some(([v]) => v === rot.every_min) ? WP_EVERY : [...WP_EVERY, [rot.every_min, tr('cada {n} min', { n: rot.every_min })]], rot.every_min, tr('Cada cuánto'));
  const [orderWrap, orderSel] = select([['random', tr('al azar')], ['ordered', tr('en orden')]], rot.order, tr('Orden'));
  const fitLabel = el('label', { className: 'wp-fit' });
  const fit = el('input', { type: 'checkbox', checked: rot.fit });
  fitLabel.append(fit, document.createTextNode(m.vertical ? tr(' Solo verticales') : tr(' Solo horizontales')));
  fitLabel.title = tr('Solo fotos con la misma orientación que el monitor (las cuadradas sirven para los dos).');
  // colour wishes (PLAN 0.13.5): need the photos' colours, worked out by the library
  const wish = (checked, label, title) => {
    const l = el('label', { className: 'wp-fit', title });
    const c = el('input', { type: 'checkbox', checked: !!checked });
    l.append(c, document.createTextNode(` ${label}`));
    return [l, c];
  };
  const [dayLabel, daynight] = wish(rot.daynight, tr('De día claros, de noche oscuros'), tr('De 7 a 19 h fotos claras; el resto, oscuras (por su color dominante).'));
  const [coverLabel, cover] = wish(rot.cover, tr('Que siga la portada'), tr('Entre las fotos de la fuente, las de color más parecido a la portada del tema que suena.'));
  const next = el('button', { type: 'button', className: 'gm-chip gm-chip-plain', textContent: tr('Siguiente') });
  opts.append(srcWrap, everyWrap, orderWrap, fitLabel, dayLabel, coverLabel, next);
  const sync = () => { opts.hidden = !on.checked; next.disabled = !on.checked; };
  sync();

  const save = async () => {
    sync();
    const body = { on: on.checked, source: options[Number(srcSel.value)], every_min: Number(everySel.value), order: orderSel.value, fit: fit.checked,
      daynight: daynight.checked, cover: cover.checked };
    try {
      const res = await tauri.core.invoke('wallpaper_rotation_set', { key: m.key, rot: body });
      if (on.checked && res.changed === 0 && !m.current) toast(tr('No hay fotos que sirvan para este monitor en esa fuente.'), { duration: 6000 });
      refreshWallpaperRow(box);
    } catch (err) {
      toast(tr(String(err)), { duration: 6000 });
    }
  };
  for (const c of [on, srcSel, everySel, orderSel, fit, daynight, cover]) c.addEventListener('change', save);
  next.addEventListener('click', async () => {
    try {
      const n = await tauri.core.invoke('wallpaper_next', { key: m.key });
      if (!n) toast(tr('No hay otra foto que sirva para este monitor en esa fuente.'), { duration: 6000 });
      refreshWallpaperRow(box);
    } catch (err) {
      toast(tr(String(err)), { duration: 6000 });
    }
  });
  box.append(head, opts);
  return box;
}

// only the «Ahora: …» line: the rest of the row is what the user just set
async function refreshWallpaperRow(box) {
  const r = await tauri.core.invoke('wallpaper_rotation_get').catch(() => null);
  const m = r?.monitors.find((x) => x.key === box.dataset.key);
  if (m?.current) box.querySelector('.wp-now').textContent = `Ahora: ${m.current.split(/[\\/]/).pop()}`;
}

// What Bakin costs right now: every process it has open, like the Task Manager.
async function renderUsage() {
  call('log_info').then((l) => {
    $('log-size').textContent = l.bytes < 1048576 ? `${Math.max(1, Math.ceil(l.bytes / 1024))} KB` : `${(l.bytes / 1048576).toFixed(1).replace('.', ',')} MB`;
  }).catch(() => {});
  let u;
  try { u = await tauri.core.invoke('usage'); } catch { $('usage-total').textContent = tr('no disponible'); return; }
  $('usage-total').textContent = `${Math.round(u.total_mb)} MB`;
  $('usage-list').replaceChildren(...u.groups.map((g) => {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = g.name;
    const mb = document.createElement('strong');
    mb.textContent = g.mb ? `${Math.round(g.mb)} MB` : tr('cerrado');
    if (!g.mb) li.className = 'off';
    li.append(name, mb);
    return li;
  }));
}

// Descargas' sidebar: a summary, the settings are in Configuración.
function renderDlSummary() {
  const box = $('dl-summary');
  const rows = [[tr('Carpeta'), $('out-dir').value || '—']];
  if (MEDIA) {
    const p = presetById(MEDIA.default_preset);
    rows.push([tr('Música y video'), p ? `${p.name} · ${formatLabel(p)}` : '—']);
    if (MEDIA.daily_limit) rows.push([tr('Límite'), tr('{daily_limit} por día', { daily_limit: MEDIA.daily_limit })]);
    if (MEDIA.hours?.enabled) rows.push([tr('Horario'), `${String(MEDIA.hours.from).padStart(2, '0')}:00 a ${String(MEDIA.hours.to).padStart(2, '0')}:00`]);
  }
  box.replaceChildren(...rows.flatMap(([k, v]) => {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    dd.title = v;
    return [dt, dd];
  }));
}

async function renderWallhavenAccount() {
  const acc = await call('wallhaven_account').catch(() => null);
  if (!acc) return;
  $('wh-user').value = acc.user || '';
  $('wh-key').value = '';
  $('wh-key').placeholder = acc.key ? tr('Clave guardada (escribe otra para cambiarla)') : tr('Clave de API');
  $('wh-key-clear').hidden = !acc.key;
  $('wh-key-status').textContent = acc.key
    ? (acc.user ? tr('Clave guardada en Windows: tus colecciones aparecen en «Descargar › Fondos de Wallhaven».') : tr('Clave guardada. Escribe tu usuario para ver tus colecciones.'))
    : tr('Sin clave: solo lo público.');
}

function wireWallhavenAccount() {
  $('wh-key-save').addEventListener('click', async () => {
    const btn = $('wh-key-save');
    btn.setAttribute('aria-busy', 'true');
    const res = await call('wallhaven_account_set', { key: $('wh-key').value, user: $('wh-user').value }).catch((err) => ({ ok: false, error: String(err) }));
    btn.removeAttribute('aria-busy');
    $('wh-key').value = '';  // never left in the page
    if (!res.ok) { $('wh-key-status').textContent = tr(res.error); return; }
    renderWallhavenAccount();
    toast(tr('Listo: cuenta de Wallhaven guardada.'));
  });
  $('wh-key-clear').addEventListener('click', async () => {
    const res = await call('wallhaven_account_clear').catch((err) => ({ ok: false, error: String(err) }));
    if (!res.ok) { toast(tr(res.error), { duration: 6000 }); return; }
    renderWallhavenAccount();
  });
}

// «Atajos de teclado» (0.13 review): global shortcuts the shell registers (hotkeys.rs)
const KEY_NAMES = { Space: tr('Espacio'), ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→', Escape: 'Esc', Backquote: '`', Minus: '-', Equal: '=',
  BracketLeft: '[', BracketRight: ']', Semicolon: 'Ñ', Quote: "'", Comma: ',', Period: '.', Slash: '/', Backslash: '\\' };
const prettyCombo = (c) => (c ? c.split('+').map((p) => ({ ctrl: 'Ctrl', control: 'Ctrl', shift: 'Shift', alt: 'Alt', super: 'Win', meta: 'Win' }[p.toLowerCase()]
  || KEY_NAMES[p] || p.replace(/^Key/, '').replace(/^Digit/, '').replace(/^Numpad/, 'Num '))).join(' + ') : tr('Sin atajo'));

async function renderHotkeys() {
  const list = await tauri.core.invoke('hotkeys_get').catch(() => []);
  $('hotkeys').replaceChildren(...list.map((h) => {
    const row = document.createElement('div');
    row.className = 'hotkey';
    const name = document.createElement('span');
    name.textContent = tr(h.name);
    const key = document.createElement('button');
    key.type = 'button';
    key.className = 'gm-chip gm-chip-plain hotkey-key';
    key.dataset.action = h.id;
    key.textContent = prettyCombo(h.combo);
    key.title = h.active ? tr('Tocá y apretá la combinación nueva') : tr('Otro programa usa esta combinación: elegí otra');
    if (!h.active) key.dataset.tone = 'warning';
    key.addEventListener('click', () => recordHotkey(key, h));
    row.append(name, key);
    return row;
  }));
}

async function recordHotkey(key, h) {
  await tauri.core.invoke('hotkeys_pause', { paused: true }).catch(() => {});
  key.textContent = tr('Apretá la combinación…');
  key.setAttribute('aria-pressed', 'true');
  const done = async (combo) => {
    removeEventListener('keydown', onKey, true);
    key.removeAttribute('aria-pressed');
    if (combo !== null) {
      try {
        await tauri.core.invoke('hotkeys_set', { action: h.id, combo });
      } catch (err) {
        toast(tr(String(err)), { duration: 6000 });
      }
    }
    await tauri.core.invoke('hotkeys_pause', { paused: false }).catch(() => {});
    renderHotkeys();
  };
  const onKey = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) return;  // wait for the key itself
    if (e.code === 'Escape') { done(null); return; }
    if (e.code === 'Backspace' && !e.ctrlKey && !e.altKey && !e.shiftKey) { done(''); return; }
    const mods = [e.ctrlKey && 'ctrl', e.altKey && 'alt', e.shiftKey && 'shift', e.metaKey && 'super'].filter(Boolean);
    if (!mods.length && !/^F\d+$/.test(e.code)) {
      key.textContent = tr('Con Ctrl, Alt o Shift…');
      return;
    }
    done([...mods, e.code].join('+'));
  };
  addEventListener('keydown', onKey, true);
}

function wireSettings() {
  $('smooth-scroll').checked = smoothOn();
  $('smooth-scroll').addEventListener('change', (e) => store.set('bakin.smooth', e.target.checked ? 'on' : 'off'));
  wireWallhavenAccount();
  wireFrame();
  $('wp-paused').addEventListener('change', (e) => tauri.core.invoke('wallpaper_rotation_pause', { paused: e.target.checked }).catch((err) => toast(tr(String(err)), { duration: 6000 })));
  $('btn-settings').addEventListener('click', () => openSettings());
  $('btn-dl-settings').addEventListener('click', () => openSettings('downloads'));
  for (const b of document.querySelectorAll('#settings-nav .scope')) b.addEventListener('click', () => showSettingsSection(b.dataset.sec));
  $('usage-refresh').addEventListener('click', renderUsage);
  $('log-open').addEventListener('click', () => call('open_logs').catch(() => {}));
  $('settings-dialog').addEventListener('gm-close', renderDlSummary);
}

function wireMusicViews() {
  for (const b of document.querySelectorAll('#music-views .scope')) {
    b.addEventListener('click', () => (b.dataset.mview === 'queue' ? openQueueView() : selectMusicView(b.dataset.mview, { name: MVIEWS[b.dataset.mview] })));
  }
  $('lib-back').addEventListener('click', () => L.mview?.back && selectMusicView(L.mview.back, { name: MVIEWS[L.mview.back] }));
  $('mus-play').addEventListener('click', () => playList(false));
  $('mus-shuffle').addEventListener('click', () => playList(true));
  $('btn-new-playlist').addEventListener('click', () => openPlaylistDialog());
  pdKind = chipGroup($('pd-kind'), syncPdKind);
  pdMatch = chipGroup($('pd-match'), pdPreview);
  $('pd-add-rule').addEventListener('click', () => { $('pd-rules').append(ruleRow()); pdPreview(); });
  $('pd-sort').addEventListener('change', pdPreview);
  $('pd-limit').addEventListener('input', pdPreview);
  $('pd-cancel').addEventListener('click', () => $('playlist-dialog').close());
  $('pd-save').addEventListener('click', savePlaylist);
  $('pd-name').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); savePlaylist(); } });
  $('pl-edit').addEventListener('click', () => {
    const pl = MUSIC?.playlists.find((p) => p.id === L.mview?.value);
    if (pl) openPlaylistDialog(pl);
  });
  $('pl-export').addEventListener('click', async () => {
    const pl = MUSIC?.playlists.find((p) => p.id === L.mview?.value);
    if (!pl) return;
    const path = await tauri.dialog.save({ title: tr('Exportar playlist'), defaultPath: `${pl.name.replace(/[\\/:*?"<>|]/g, '_')}.m3u8`,
      filters: [{ name: 'Playlist', extensions: ['m3u8'] }] }).catch(() => null);
    if (!path) return;
    const r = await call('music_playlist_export', { playlist_id: pl.id, path }).catch((err) => ({ ok: false, error: String(err) }));
    toast(r.ok ? tr('Guardada: {path} ({count} temas).', { path: r.path, count: r.count }) : tr(r.error), { duration: 7000 });
  });
  // mirror folders (0.20.1)
  $('pl-mirror').addEventListener('click', () => onMirrorClick());
  $('pl-delete').addEventListener('click', async () => {
    const pl = MUSIC?.playlists.find((p) => p.id === L.mview?.value);
    if (!pl) return;
    const ids = pl.kind === 'list' ? (await call('music_tracks', { filters: { music: { view: 'playlist', value: pl.id } } }).catch(() => ({ tracks: [] }))).tracks.map((t) => t.id) : null;
    await call('music_playlist_delete', { playlist_id: pl.id });
    await loadMusicOverview();
    selectMusicView('songs', { name: MVIEWS.songs });
    toast(tr('Se borró la playlist «{name}» (los temas siguen en tu biblioteca).', { name: pl.name }), { duration: 9000, action: { label: tr('Deshacer'), onClick: async () => {
      await call('music_playlist_save', { name: pl.name, kind: pl.kind, rules: pl.rules, ids });
      loadMusicOverview();
    } } });
  });
  $('sel-addpl').addEventListener('click', () => openAddToPlaylist(L.items.filter((i) => L.selected.has(i.id) && i.kind === 'audio').map((i) => i.id)));
  $('sel-rmpl').addEventListener('click', async () => {
    const ids = [...L.selected];
    const r = await call('music_playlist_remove', { playlist_id: L.mview.value, ids }).catch(() => ({ removed: 0 }));
    toast(tn('{n} tema fuera de la playlist.', '{n} temas fuera de la playlist.', r.removed));
    exitSelecting();
    loadMusicOverview();
    reloadItems();
  });
  $('ap-cancel').addEventListener('click', () => $('addpl-dialog').close());
  const create = async () => {
    const name = $('ap-new').value.trim();
    if (!name) { $('ap-new').focus(); return; }
    const r = await call('music_playlist_save', { name, kind: 'list', ids: [] }).catch((err) => ({ ok: false, error: String(err) }));
    if (!r.ok) { toast(tr(r.error)); return; }
    addToPlaylist(r.playlist.id, r.playlist.name);
  };
  $('ap-create').addEventListener('click', create);
  $('ap-new').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); create(); } });
}

// ------------------------------------------------------------------ player
// Bakin Música plays in the shell (src-tauri/src/player.rs), not in this
// page: the music goes on with the window closed, and the page is a remote
// control that draws what the player reports ("player" events: state, time,
// notices). The queue lives in the player; the page only keeps its songs'
// covers for drawing.
const P = {
  state: { track: null, playing: false, count: 0, pos: 0, queued: 0, time: 0, duration: null,
    shuffle: false, repeat: 'off', volume: 0.8, muted: false, crossfade: 0, sleep: null, source: '' },
  seeking: false,
  covers: new Map(),
};
const plCur = () => P.state.track;
const plPlaying = () => !!P.state.playing;
const pc = (cmd) => tauri.core.invoke('player_call', { cmd }).then((st) => { if (st && 'playing' in st) plApply(st); return st; })
  .catch((err) => { toast(tr(String(err))); return null; });

// The songs the engine lists, reduced to what the player needs.
const trackOf = (t) => ({ id: t.id, path: t.path, name: t.name, title: t.title, artist: t.artist, album: t.album,
  duration: t.duration, fav: !!t.fav });

function plSetQueue(tracks, start, source) {
  if (!tracks.length) { toast(tr('No hay temas para reproducir.')); return; }
  pc({ cmd: 'load', tracks: tracks.map(trackOf), start: Math.max(0, start), source: source || '', play: true });
}

function plEnqueue(tracks) {
  if (!tracks.length) return;
  pc({ cmd: 'enqueue', tracks: tracks.map(trackOf) });
  toast(tracks.length === 1 ? tr('«{x}» suena a continuación.', { x: tracks[0].title }) : tr('{tracks} temas agregados a la cola.', { tracks: tracks.length }));
}

async function playFromLibrary(itemId) {
  let res;
  try { res = await call('music_tracks', { filters: libFilters() }); } catch (err) { toast(tr(String(err))); return; }
  const start = res.tracks.findIndex((t) => t.id === itemId);
  plSetQueue(res.tracks, Math.max(0, start), libTitle());
}

async function tracksFor(ids) {
  try { return (await call('music_tracks', { ids })).tracks; } catch (err) { toast(tr(String(err))); return []; }
}

// --------------------------------------------------------------- drawing
function plApply(st) {
  setTimeout(loadLyrics);  // the song may have changed
  const prev = P.state;
  P.state = { ...P.state, ...st };
  $('mc-queue').textContent = P.state.count ? String(P.state.count - P.state.pos) : '';
  if (prev.track?.id !== P.state.track?.id || prev.track?.fav !== P.state.track?.fav) plDrawNow();
  else plDrawControls();
  plDrawTime(true);
  if (L.mview?.view === 'queue' && prev.track?.id !== P.state.track?.id) openQueueView(true);
}

const SHUFFLE_NAMES = { random: tr('Totalmente al azar'), smart: tr('Inteligente'), album: tr('Por álbum'), artist: tr('Por artista') };

function plDrawControls() {
  const s = P.state;
  $('player').hidden = !s.count;
  $('player').classList.toggle('playing', !!s.playing);
  $('pl-play').setAttribute('aria-label', s.playing ? tr('Pausar') : tr('Reproducir'));
  $('pl-shuffle').setAttribute('aria-pressed', String(!!s.shuffle));
  const mode = s.shuffle_mode || 'random';
  $('pl-shuffle').dataset.mode = mode;
  $('pl-shuffle').title = (s.shuffle ? tr('Aleatorio: {mode}', { mode: SHUFFLE_NAMES[mode].toLowerCase() }) : tr('Aleatorio: no')) + tr(' (S) · clic derecho: elegir el tipo');
  for (const b of document.querySelectorAll('#shuffle-menu > button')) b.setAttribute('aria-checked', String(s.shuffle && b.value === mode));
  const rep = { off: tr('Repetir: no'), all: tr('Repetir: toda la cola'), one: tr('Repetir: este tema') }[s.repeat] || tr('Repetir');
  $('pl-repeat').dataset.mode = s.repeat;
  $('pl-repeat').title = `${rep} (R)`;
  $('pl-mute').classList.toggle('muted', !!s.muted);
  const vol = $('pl-volume');
  if (document.activeElement !== vol) { vol.value = String(Math.round(s.volume * 100)); vol.closest('gm-slider')?.sync?.(); }
  plDrawSleep();
  syncKeepAlive();
}

function plDrawTime(full = false) {
  const s = P.state;
  const dur = s.duration || s.track?.duration || 0;
  $('pl-pos').textContent = fmtDur(s.time || 0) || '0:00';
  if (full) $('pl-len').textContent = fmtDur(dur) || '0:00';
  if (!P.seeking && dur) {
    const bar = $('pl-seek');
    bar.value = String(Math.round(((s.time || 0) / dur) * 1000));
    bar.closest('gm-slider')?.sync?.();
  }
}

async function plDrawNow() {
  plDrawControls();
  const t = P.state.track;
  if (!t) return;
  $('pl-title').textContent = t.title || t.name;
  $('pl-artist').textContent = [t.artist, t.album].filter(Boolean).join(' · ') || t.name;
  $('player').title = t.path;
  $('pl-fav').setAttribute('aria-pressed', String(!!t.fav));
  $('pl-fav').title = t.fav ? tr('Quitar de favoritas (F)') : tr('Favorita (F)');
  // the playing song stands out in the library list
  document.querySelectorAll('#lib-grid .playing').forEach((el) => el.classList.remove('playing'));
  document.querySelector(`#lib-grid [data-id="${CSS.escape(t.id)}"]`)?.classList.add('playing');
  const cover = await plCover(t.id);
  if (P.state.track?.id !== t.id) return;
  const box = $('pl-cover');
  if (cover) { const img = new Image(); img.alt = ''; img.src = cover; box.replaceChildren(img); } else box.innerHTML = ICONS.audio;
  if (tintOn()) retintBackdrop(cover);
}

async function plCover(id) {
  if (P.covers.has(id)) return P.covers.get(id);
  let cover = '';
  try { cover = (await call('music_cover', { item_id: id, size: 320 })).cover; } catch { /* no cover */ }
  P.covers.set(id, cover);
  if (P.covers.size > 40) P.covers.delete(P.covers.keys().next().value);
  return cover;
}

function plDrawSleep() {
  const badge = $('pl-sleep-badge');
  const sl = P.state.sleep;
  if (sl === 'end') { badge.hidden = false; badge.textContent = tr('Para al terminar el tema'); return; }
  if (typeof sl === 'number') {
    badge.hidden = false;
    badge.textContent = tr('Para a las {time}', { time: new Date(sl * 1000).toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' }) });
    return;
  }
  badge.hidden = true;
}


// 0.6 kept only the songs' ids (engine side): hand them to the player once.
async function plMigrate() {
  let s;
  try { s = await call('music_state_get'); } catch { return; }
  if (!s.ids?.length) return;
  const tracks = await tracksFor(s.ids);
  if (!tracks.length) return;
  const same = tracks.length === s.ids.length;
  const start = same ? s.order[s.pos] ?? 0 : 0;
  await pc({ cmd: 'set', volume: s.volume, muted: s.muted, repeat: s.repeat, crossfade: s.crossfade });
  await pc({ cmd: 'load', tracks: tracks.map(trackOf), start, source: s.source || '', play: false });
  if (same && s.time) await pc({ cmd: 'seek', time: s.time });
}

function wirePlayer() {
  tauri.event.listen('player', (e) => {
    const { kind, data } = e.payload || {};
    if (kind === 'state') plApply(data);
    else if (kind === 'time') { P.state.time = data.time; if (data.duration) P.state.duration = data.duration; plDrawTime(); syncLyricsTime(); }
    else if (kind === 'queue' && L.mview?.view === 'queue') openQueueView(true);
    else if (kind === 'notice' || kind === 'error') toast(tr(data.message), { duration: 6000 });
  });
  $('pl-play').addEventListener('click', () => pc({ cmd: 'toggle' }));
  // one button each way: a click jumps a few seconds, a right click changes the song
  const skipSecs = () => Number(store.get('bakin.skip', '10')) || 10;
  const skipTitles = () => {
    $('pl-prev').title = tr('Clic: atrasar {secs} s · Clic derecho: tema anterior', { secs: skipSecs() });
    $('pl-next').title = tr('Clic: adelantar {secs} s · Clic derecho: tema siguiente', { secs: skipSecs() });
  };
  skipTitles();
  $('pl-next').addEventListener('click', () => pc({ cmd: 'skip_secs', secs: skipSecs() }));
  $('pl-prev').addEventListener('click', () => pc({ cmd: 'skip_secs', secs: -skipSecs() }));
  $('pl-next').addEventListener('contextmenu', (e) => { e.preventDefault(); pc({ cmd: 'next' }); });
  $('pl-prev').addEventListener('contextmenu', (e) => { e.preventDefault(); pc({ cmd: 'prev' }); });
  const skip = chipGroup($('po-skip'), (v) => { store.set('bakin.skip', v); skipTitles(); });
  skip.set(String(skipSecs()));
  $('pl-mini').addEventListener('click', () => tauri.core.invoke('mini_open').catch((err) => toast(tr(String(err)))));
  $('pl-shuffle').addEventListener('click', () => pc({ cmd: 'set', shuffle: !P.state.shuffle }));
  // right click: the kind of shuffle (picking one also turns it on)
  $('pl-shuffle').addEventListener('contextmenu', (e) => { e.preventDefault(); $('shuffle-menu').open(); });
  $('shuffle-menu').addEventListener('gm-select', (e) => {
    pc({ cmd: 'set', shuffle: true, shuffle_mode: e.detail.value });
    toast(tr('Aleatorio: {x}.', { x: SHUFFLE_NAMES[e.detail.value].toLowerCase() }));
  });
  $('pl-repeat').addEventListener('click', () => pc({ cmd: 'set', repeat: { off: 'all', all: 'one', one: 'off' }[P.state.repeat] || 'off' }));
  const seek = $('pl-seek');
  seek.addEventListener('input', () => {
    P.seeking = true;
    const dur = P.state.duration || P.state.track?.duration || 0;
    $('pl-pos').textContent = fmtDur((Number(seek.value) / 1000) * dur) || '0:00';
  });
  const hover = $('pl-hover');
  seek.addEventListener('pointermove', (e) => {
    const dur = P.state.duration || P.state.track?.duration || 0;
    if (!dur) return;
    const r = seek.getBoundingClientRect();
    const k = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    hover.textContent = fmtDur(k * dur) || '0:00';
    hover.style.left = `${k * 100}%`;
    hover.hidden = false;
  });
  seek.addEventListener('pointerleave', () => { hover.hidden = true; });
  seek.addEventListener('change', () => {
    P.seeking = false;
    const dur = P.state.duration || P.state.track?.duration || 0;
    pc({ cmd: 'seek', time: (Number(seek.value) / 1000) * dur });
  });
  $('pl-volume').addEventListener('input', (e) => pc({ cmd: 'set', volume: Number(e.target.value) / 100 }));
  $('pl-mute').addEventListener('click', () => pc({ cmd: 'set', muted: !P.state.muted }));
  $('pl-queue').addEventListener('click', () => openQueueView());
  $('pl-locate').addEventListener('click', locatePlaying);
  $('pl-fav').addEventListener('click', async () => {
    const t = P.state.track;
    if (!t) return;
    const value = !t.fav;
    await setFavorite([t.id], value);
    pc({ cmd: 'fav', id: t.id, value });
    loadMusicOverview();
  });
  $('queue-clear').addEventListener('click', async () => { await pc({ cmd: 'clear_after' }); openQueueView(true); });

  const sleep = chipGroup($('po-sleep'), (v) => pc({ cmd: 'set', sleep: v === 'end' ? 'end' : Number(v) }));
  const fade = $('po-fade');
  const fadeLabel = () => { $('po-fade-label').textContent = Number(fade.value) ? `${fade.value} s` : tr('sin fundido'); };
  fade.addEventListener('input', fadeLabel);
  fade.addEventListener('change', () => pc({ cmd: 'set', crossfade: Number(fade.value) }));
  $('pl-more').addEventListener('click', () => {
    const sl = P.state.sleep;
    sleep.set(sl === 'end' ? 'end' : (typeof sl === 'number' ? '' : '0'));
    fade.value = String(P.state.crossfade || 0);
    fade.closest('gm-slider')?.sync?.();
    fadeLabel();
    $('playopts-dialog').show();
  });
  $('po-close').addEventListener('click', () => $('playopts-dialog').close());

  document.addEventListener('keydown', (e) => {
    if (!P.state.count || e.altKey || e.metaKey) return;
    const el = e.target instanceof Element ? e.target : document.body;
    if (el.closest('input, textarea, select, [contenteditable]') || document.querySelector('dialog[open]')) return;
    const k = e.key;
    if (k === ' ' && !e.ctrlKey) { if (el.closest('button') && !el.closest('.lib-row, .lib-tile')) return; e.preventDefault(); pc({ cmd: 'toggle' }); }
    else if (k === 'ArrowRight' && e.ctrlKey) { e.preventDefault(); pc({ cmd: 'next' }); }
    else if (k === 'ArrowLeft' && e.ctrlKey) { e.preventDefault(); pc({ cmd: 'prev' }); }
    else if (k === 'ArrowRight' && !e.ctrlKey && !e.shiftKey) pc({ cmd: 'skip_secs', secs: Number(store.get('bakin.skip', '10')) || 10 });
    else if (k === 'ArrowLeft' && !e.ctrlKey && !e.shiftKey) pc({ cmd: 'skip_secs', secs: -(Number(store.get('bakin.skip', '10')) || 10) });
    else if ((k === 'm' || k === 'M') && !e.ctrlKey) $('pl-mute').click();
    else if ((k === 'f' || k === 'F') && !e.ctrlKey) $('pl-fav').click();
    else if ((k === 's' || k === 'S') && !e.ctrlKey) $('pl-shuffle').click();
    else if ((k === 'r' || k === 'R') && !e.ctrlKey) $('pl-repeat').click();
    else if ((k === 'q' || k === 'Q') && !e.ctrlKey) $('pl-queue').click();
    else if ((k === 'l' || k === 'L') && !e.ctrlKey) $('pl-locate').click();
    else if ((k === 'n' || k === 'N') && !e.ctrlKey) $('pl-mini').click();
  });

  // from the library: play the selection, or add it to the queue
  const selectedAudio = () => L.items.filter((i) => L.selected.has(i.id) && i.kind === 'audio').map((i) => i.id);
  $('sel-play').addEventListener('click', async () => {
    const tracks = await tracksFor(selectedAudio());
    plSetQueue(tracks, 0, tr('{tracks} temas elegidos', { tracks: tracks.length }));
  });
  $('sel-queue').addEventListener('click', async () => plEnqueue(await tracksFor(selectedAudio())));

  pc({ cmd: 'state' }).then((st) => { if (st && !st.count) plMigrate(); });
}

async function init() {
  wireWindow();
  wireOptions();
  wireJobs();
  wireSearch();
  wireSkeleton();
  wireWallhaven();
  wireViews();
  wireLibraryView();
  wireFolder();
  wireMedia();
  wireConvert();
  wireMusic();
  wirePlayer();
  wireMusicViews();
  wireSettings();
  wireCookiesVault();
  wireClean();
  wireCatalog();
  wireSpotify();
  wireSound();
  $('tb-progress').addEventListener('click', () => setView('downloads'));
  const perf = async (changes) => {
    const r = await call('settings_set', { changes }).catch((e) => ({ ok: false, error: String(e) }));
    if (!r.ok) toast(tr(r.error)); else L.catalogCovers = r.settings.catalog_covers;
  };
  $('perf-hourly').addEventListener('change', () => perf({ catalog_hourly: Number($('perf-hourly').value) || 0 }));
  $('perf-covers').addEventListener('change', () => perf({ catalog_covers: $('perf-covers').checked }));
  chipGroup($('st-span'), (v) => { statsSpan = v; reloadItems(); });
  wireHome();
  wireBackground();
  wireBackdrop();
  wireLinks();
  wireFollows();
  wireVersion();
  await tauri.event.listen('engine', (e) => onEngineEvent(e.payload));
  await tauri.event.listen('engine-exit', () => {
    setBusy(false);
    log('danger', tr('El motor de descargas se detuvo; vuelve a iniciarse solo con la próxima acción.'));
  });
  try {
    const st = await call('get_state');
    if (syncLang(st.settings.lang)) return;  // drawn in another language: reloading
    applySettings(st.settings);
    savedOutDir = st.settings.out_dir;
    setLastMove(st.last_move);
    setMoving(st.moving);
    setView(store.get('bakin.view', 'photos'));
    loadMedia().then(() => call('media_maintenance').catch(() => {}));
    setBusy(st.running);
    renderQueue(st.queue);
    $('log-path').textContent = tr('Registro detallado (sin secretos): {log_path}', { log_path: st.log_path });
    log('info', tr('Bakin {version} listo. Calidad por defecto: máxima.', { version: st.version }));
    if (st.first_run) welcome();
  } catch (err) {
    log('danger', tr('No se pudo hablar con el motor: {err}', { err }));
  }
  // everything wired and the view chosen: show it all at once (app.css, body:not(.ready))
  requestAnimationFrame(() => document.body.classList.add('ready'));
}

init();

// ------------------------------------------------------- smooth scrolling
// Optional (Configuración › Apariencia › «Desplazamiento suave», off by default: the system's
// scrolling, no delay at all). On: a wheel notch moves at once and eases out over ~180 ms;
// notches while it moves add up. 0.13 review: the browser's own smooth scroll (WebView2
// ignores Chromium's wheel smoothing; scrollTo smooth eases in, which felt delayed) — so the
// page moves it, as cheaply as it can: the end is measured once per notch, not every frame,
// and each frame only writes the position. Touchpads, Ctrl+wheel, sideways scrolling,
// sliders, fields and «reduce motion» keep the browser's own.
function smoothOn() { return store.get('bakin.smooth', 'off') === 'on'; }  // hoisted: wireSettings reads it early
(() => {
  const DURATION = 180;
  const ease = (p) => 1 - (1 - p) ** 3;  // ease-out: it moves at once
  const still = matchMedia('(prefers-reduced-motion: reduce)');
  const glide = new WeakMap();  // scroller → { raf, from, to, t0, limit }
  const scrollerOf = (from, dy) => {
    for (let n = from; n && n.nodeType === 1; n = n.parentElement || n.getRootNode?.().host) {
      const oy = getComputedStyle(n).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && n.scrollHeight > n.clientHeight + 1) {
        const room = dy > 0 ? n.scrollHeight - n.clientHeight - n.scrollTop : n.scrollTop;
        if (room > 1) return n;
      }
    }
    return null;
  };
  addEventListener('wheel', (e) => {
    if (!smoothOn() || still.matches || e.ctrlKey || e.shiftKey || e.defaultPrevented || e.deltaMode === 2) return;
    const dy = e.deltaY * (e.deltaMode === 1 ? 40 : 1);
    if (Math.abs(dy) < 40 || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;  // a touchpad: leave it
    const from = e.composedPath?.()[0] || e.target;
    if (from.closest?.('input, textarea, select, gm-slider')) return;
    const el = scrollerOf(from, dy);
    if (!el) return;
    e.preventDefault();
    let g = glide.get(el);
    if (!g) {
      g = { raf: 0, from: 0, to: 0, t0: 0, limit: 0 };
      glide.set(el, g);
      const stop = () => { cancelAnimationFrame(g.raf); g.raf = 0; };  // the scrollbar or the keyboard take over
      el.addEventListener('pointerdown', stop);
      el.addEventListener('keydown', stop);
    }
    g.limit = el.scrollHeight - el.clientHeight;  // once per notch, not per frame
    const base = g.raf && Math.sign(g.to - g.from) === Math.sign(dy) ? g.to : el.scrollTop;
    g.from = el.scrollTop;
    g.to = Math.max(0, Math.min(g.limit, base + dy));
    g.t0 = performance.now();
    if (g.raf) return;
    const step = (now) => {
      const p = Math.min(1, (now - g.t0) / DURATION);
      el.scrollTop = g.from + (g.to - g.from) * ease(p);
      g.raf = p < 1 ? requestAnimationFrame(step) : 0;
    };
    step(performance.now() + 8);  // the first move now, not a frame later
  }, { passive: false, capture: true });
})();

// ------------------------------------------------------- the browser's own
// (0.13 review) Chromium's right-click menu (Refresh, Share, Save image…) and its keys
// (Ctrl+G / Ctrl+F find, Ctrl+P print, Ctrl+R / F5 reload…) aren't Bakin's: out. Text fields
// keep their menu (copy, paste); Bakin's own shortcuts (Ctrl+K, Ctrl+D, Ctrl+B, Ctrl+A…) go on.
const BROWSER_KEYS = new Set(['f', 'g', 'p', 'r', 's', 'u', 'j', 'h', 'o', 'n', 't', 'e', 'l']);
addEventListener('contextmenu', (e) => {
  if (!e.target.closest?.('input, textarea, [contenteditable="true"]')) e.preventDefault();
});
addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  if ((e.ctrlKey && !e.altKey && BROWSER_KEYS.has(k)) || e.key === 'F3' || e.key === 'F5' || e.key === 'F7') e.preventDefault();
});

// «Carpeta espejo…» in a playlist's header (PLAN 0.20.1)
async function onMirrorClick() {
  const pl = MUSIC?.playlists.find((p) => p.id === L.mview?.value);
  if (!pl) return;
  if (pl.mirror) {
    call('music_mirror_sync', { playlist_id: pl.id });
    toast(tr('Sincronizando la carpeta espejo: {mirror}', { mirror: pl.mirror }), { duration: 8000, action: { label: tr('Dejar de mantenerla'), onClick: async () => {
      await call('music_mirror_off', { playlist_id: pl.id });
      toast(tr('Ya no se mantiene esa carpeta; lo que tiene se queda.'));
      await loadMusicOverview();
      selectMusicView('playlist', { value: pl.id, name: pl.name });
    } } });
    return;
  }
  const dir = await tauri.dialog.open({ directory: true, title: tr('Carpeta espejo de «{name}»', { name: pl.name }) }).catch(() => null);
  if (typeof dir !== 'string') return;
  let r = await call('music_mirror_set', { playlist_id: pl.id, folder: dir }).catch((e) => ({ ok: false, error: String(e) }));
  if (!r.ok && r.needs_adopt) {
    if (!(await ask(tr('Esa carpeta ya tiene cosas'), tr('Bakin suma los temas de la playlist y nunca toca lo que ya estaba. ¿Usarla igual?'), tr('Usarla')))) return;
    r = await call('music_mirror_set', { playlist_id: pl.id, folder: dir, adopt: true }).catch((e) => ({ ok: false, error: String(e) }));
  }
  if (!r.ok) { toast(tr(r.error), { duration: 7000 }); return; }
  toast(tn('Carpeta espejo lista: se copia 1 tema.', 'Carpeta espejo lista: se copian {n} temas.', r.copy), { duration: 6000 });
  await loadMusicOverview();
  selectMusicView('playlist', { value: pl.id, name: pl.name });
}

// a mirror folder finished a sync
async function onMirrorDone(ev) {
  if (!ev.ok) { toast(tr('Carpeta espejo de «{name}»: {error}', { name: ev.name, error: tr(ev.error) }), { duration: 7000 }); return; }
  const n = (k, one, many) => (k ? `${k} ${k === 1 ? one : many}` : '');
  const bits = [n(ev.copied, tr('copiado'), tr('copiados')), n(ev.updated, tr('actualizado'), tr('actualizados')), ev.removed ? tr('{n} a la Papelera', { n: ev.removed }) : '',
    ev.failed?.length ? tr('{failed} con error ({x})', { failed: ev.failed.length, x: ev.failed[0].error }) : ''].filter(Boolean);
  if (bits.length) toast(tr('Carpeta espejo de «{name}»: {bits}.', { name: ev.name, bits: bits.join(', ') }), { duration: 7000 });
  if (ev.ask_remove && await ask(tn('¿Sacar un tema de la carpeta espejo?', '¿Sacar {n} temas de la carpeta espejo?', ev.ask_remove),
    tr('Salieron de «{name}». Van a la Papelera; en un pendrive o el celular Windows no tiene Papelera y se quedan. No se vuelve a preguntar para esta playlist.', { name: ev.name }), tr('Sacarlos'))) {
    call('music_mirror_sync', { playlist_id: ev.playlist, allow_remove: true });
  }
}

// Configuración › En tu casa: the home server (PLAN 0.21.1, lan.rs in the shell)
function showLan(st) {
  $('lan-on').checked = !!st.running;
  $('lan-pair').hidden = !st.running;
  $('lan-url').textContent = st.url || '…';
  $('lan-code').textContent = st.code || '…';
  $('lan-qr').innerHTML = st.qr_svg || '';
  $('lan-status').textContent = st.error ? tr(st.error)
    : st.running && !st.private ? tr('No hay una red de casa ahora (o es una red pública): solo esta PC puede conectarse.')
    : st.running ? tr('Escuchando en {addresses}.', { addresses: st.addresses.join(', ') }) : '';
}

async function wireHome() {
  const refresh = async () => showLan(await tauri.core.invoke('lan_status').catch((e) => ({ error: String(e) })));
  $('lan-on').addEventListener('change', async () => {
    const st = await tauri.core.invoke('lan_set', { on: $('lan-on').checked }).catch((e) => ({ error: String(e) }));
    if (st.error) $('lan-on').checked = false;
    showLan(st);
  });
  $('lan-new-code').addEventListener('click', async () => {
    if (!(await ask(tr('¿Cambiar el código?'), tr('Los celulares conectados dejan de entrar hasta que les pongas el código nuevo.'), tr('Cambiar')))) return;
    showLan(await tauri.core.invoke('lan_new_code').catch((e) => ({ error: String(e) })));
  });
  document.querySelector('#settings-nav [data-sec=home]')?.addEventListener('click', refresh);
}

// The guided first start (PLAN 0.22.2): the downloads folder, «my music», what each part is.
// Only when Bakin was never used on this PC; «Saltar» or the last step marks it done.
function welcome() {
  const dlg = $('welcome-dialog');
  let step = 1;
  const show = () => {
    for (const s of dlg.querySelectorAll('.welcome-step')) s.hidden = Number(s.dataset.step) !== step;
    $('welcome-back').hidden = step === 1;
    $('welcome-next').textContent = step === 3 ? tr('Empezar') : tr('Siguiente');
    $('welcome-out').textContent = $('out-dir').value || savedOutDir || '';
  };
  $('welcome-out-pick').onclick = async () => {
    const dir = await tauri.dialog.open({ directory: true, defaultPath: savedOutDir || undefined, title: tr('Carpeta de descargas') });
    if (typeof dir === 'string') { await changeOutDir(dir); show(); }
  };
  $('welcome-add').onclick = async () => {
    const dir = await tauri.dialog.open({ directory: true, title: tr('Agregar una carpeta con música') });
    if (typeof dir !== 'string') return;
    const res = await call('library_add_folder', { path: dir }).catch((e) => ({ ok: false, error: String(e) }));
    if (!res.ok) { toast(tr(res.error), { duration: 6000 }); return; }
    const li = document.createElement('li');
    li.textContent = dir;
    $('welcome-folders').append(li);
    refreshLibrary();
  };
  // the language first: the welcome is drawn again in it (it shows until finished or skipped)
  $('welcome-lang').value = LANG_NOW;
  $('welcome-lang').addEventListener('gm-change', async () => {
    const r = await call('settings_set', { changes: { lang: $('welcome-lang').value } }).catch(() => null);
    if (r?.ok) syncLang(r.settings.lang);
  }, { once: true });
  $('welcome-back').onclick = () => { step = Math.max(1, step - 1); show(); };
  $('welcome-next').onclick = () => {
    if (step < 3) { step += 1; show(); return; }
    dlg.close('done');
  };
  dlg.addEventListener('gm-close', () => {
    call('first_run_done').catch(() => {});
    if ($('welcome-folders').children.length) setView('music');
  }, { once: true });
  show();
  dlg.show();
}
