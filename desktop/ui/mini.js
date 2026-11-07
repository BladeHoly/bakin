// «Modo mínimo»: a remote control for the player in the shell (player.rs),
// in a window of its own. It keeps nothing: it asks the player for its state
// and follows its events, like the big window does.
//
// 0.24 (the user's biggest complaint, docs/research/2026-11-07-pulido.md): two shapes, a bar
// with everything in sight and the cover as a card; window options in ⋯ only; double click or
// Esc goes back to the full window; the wheel sets the volume, said over the song for a moment.
import { tr } from './i18n.js';  // first: translates the page before the components draw
import { tokensToCss } from '@glass-midas/core';

document.head.insertAdjacentHTML('beforeend', `<style>${tokensToCss()}</style>`);

const $ = (id) => document.getElementById(id);
const tauri = window.__TAURI__;
const pc = (cmd) => tauri.core.invoke('player_call', { cmd }).catch(() => null);
const store = {
  get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private storage off */ } },
};
const skipSecs = () => Number(store.get('bakin.skip', '10')) || 10;
const expand = () => tauri.core.invoke('mini_expand');

let state = {};
let coverOf = '';

function draw(s) {
  state = { ...state, ...s };
  const t = state.track;
  document.body.classList.toggle('playing', !!state.playing);
  $('m-play').setAttribute('aria-label', state.playing ? tr('Pausar') : tr('Reproducir'));
  $('m-title').textContent = t ? t.title || t.name : tr('Nada sonando');
  $('m-artist').textContent = t ? [t.artist, t.album].filter(Boolean).join(' · ') : '';
  document.title = t ? `${t.title || t.name} · Bakin` : 'Bakin';
  drawTime();
  if (t && t.id !== coverOf) {
    coverOf = t.id;
    // from the covers the library cached: does not wake a sleeping engine
    tauri.core.invoke('track_cover', { itemId: t.id, path: t.path || '' }).then((cover) => {
      if (coverOf !== t.id) return;
      const box = $('m-cover');
      box.querySelector('img')?.remove();
      if (!cover) { box.querySelector('svg').removeAttribute('hidden'); return; }
      const img = new Image();
      img.alt = '';
      img.src = cover;
      img.setAttribute('data-tauri-drag-region', '');
      box.querySelector('svg').setAttribute('hidden', '');
      box.append(img);
    }).catch(() => {});
  }
}

const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

function drawTime() {
  const d = state.duration || 0;
  $('m-fill').style.width = d ? `${Math.min(100, (state.time || 0) / d * 100)}%` : '0';
  $('m-time').textContent = state.track ? `${clock(state.time || 0)}${d ? ` / ${clock(d)}` : ''}` : '';
}

tauri.event.listen('player', (e) => {
  const { kind, data } = e.payload || {};
  if (kind === 'state') draw(data);
  else if (kind === 'time') { state.time = data.time; if (data.duration) state.duration = data.duration; drawTime(); }
});

$('m-play').addEventListener('click', () => pc({ cmd: 'toggle' }));
// like the big player: a click jumps a few seconds, a right click changes the song (the user's choice)
$('m-prev').title = tr('Clic: atrasar {secs} s · Clic derecho: tema anterior', { secs: skipSecs() });
$('m-next').title = tr('Clic: adelantar {secs} s · Clic derecho: tema siguiente', { secs: skipSecs() });
$('m-prev').addEventListener('click', () => pc({ cmd: 'skip_secs', secs: -skipSecs() }));
$('m-next').addEventListener('click', () => pc({ cmd: 'skip_secs', secs: skipSecs() }));
$('m-prev').addEventListener('contextmenu', (e) => { e.preventDefault(); pc({ cmd: 'prev' }); });
$('m-next').addEventListener('contextmenu', (e) => { e.preventDefault(); pc({ cmd: 'next' }); });
$('m-seek').addEventListener('click', (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  if (state.duration) pc({ cmd: 'seek', time: (e.clientX - r.left) / r.width * state.duration });
});

// volume: the wheel anywhere; «Volumen 62 %» over the song for a moment
let volTimer = 0;
const setVolume = (v) => {
  state.volume = Math.max(0, Math.min(1, v));
  pc({ cmd: 'set', volume: state.volume });
  const note = $('m-volnote');
  note.textContent = tr('Volumen {pct} %', { pct: Math.round(state.volume * 100) });
  note.hidden = false;
  clearTimeout(volTimer);
  volTimer = setTimeout(() => { note.hidden = true; }, 1200);
};
addEventListener('wheel', (e) => setVolume((state.volume ?? 0.8) + (e.deltaY < 0 ? 0.05 : -0.05)), { passive: true });

// ⋯: the window options, a vertical menu in room the window makes for it, never over the song
const POP = { menu: 168, ask: 128 };
let popping = null;
async function openPop(kind) {
  if (popping === kind) { closePop(); return; }
  if (popping) await closePop();
  popping = kind;
  document.body.style.setProperty('--base-h', `${innerHeight}px`);
  const where = await tauri.core.invoke('mini_popup', { extra: POP[kind] }).catch(() => 'down');
  document.body.dataset.pop = where;
  document.body.dataset.popKind = kind;
  syncMenu();
  $('m-menu').hidden = kind !== 'menu';
  $('m-ask').hidden = kind !== 'ask';
  $('m-pop').hidden = false;
  $('m-more').setAttribute('aria-expanded', String(kind === 'menu'));
  (kind === 'menu' ? $('m-menu').querySelector('button') : $('m-ask-yes'))?.focus();
}
async function closePop() {
  if (!popping) return;
  popping = null;
  $('m-pop').hidden = true;
  delete document.body.dataset.pop;
  delete document.body.dataset.popKind;
  $('m-more').setAttribute('aria-expanded', 'false');
  await tauri.core.invoke('mini_popup', { extra: 0 }).catch(() => {});
}
$('m-more').addEventListener('click', () => openPop('menu'));
let leaving;
document.addEventListener('pointerleave', () => { leaving = setTimeout(closePop, 350); });
document.addEventListener('pointerenter', () => clearTimeout(leaving));

// the card shows its controls while the pointer is on it (CSS: body.hot)
document.addEventListener('pointerenter', () => document.body.classList.add('hot'));
document.addEventListener('pointerleave', () => document.body.classList.remove('hot'));
// «Atenuarse sin el mouse encima» (PLAN 0.17.4, Configuración › Música): 3 s after the pointer
// leaves, the small window fades to 55 %; it comes back on hover
let dimTimer = 0;
const dimOn = () => store.get('bakin.mini.dim', '') === 'on';
document.addEventListener('pointerleave', () => { clearTimeout(dimTimer); if (dimOn()) dimTimer = setTimeout(() => document.body.classList.add('dim'), 3000); });
document.addEventListener('pointerenter', () => { clearTimeout(dimTimer); document.body.classList.remove('dim'); });
if (dimOn()) dimTimer = setTimeout(() => document.body.classList.add('dim'), 3000);

// the shape: a bar or a card (the shell resizes the window and remembers it); the song only is
// what the gaming mode shows, not a shape to pick
const showLayout = (l) => {
  document.body.dataset.layout = ['bar', 'card', 'info'].includes(l) ? l : 'bar';
  closePop();
};
function syncMenu() {
  const top = (store.get('bakin.mini.layer', 'top')) === 'top';
  $('mm-top').setAttribute('aria-checked', String(top));
  $('mm-layout').querySelector('span').textContent = document.body.dataset.layout === 'card' ? tr('Ver como barra') : tr('Ver como tarjeta');
}
$('mm-top').addEventListener('click', () => { layer(store.get('bakin.mini.layer', 'top') === 'top' ? 'normal' : 'top'); syncMenu(); });
$('mm-layout').addEventListener('click', async () => {
  await closePop();
  tauri.core.invoke('mini_layout', { layout: document.body.dataset.layout === 'card' ? 'bar' : 'card' });
});
$('mm-gaming').addEventListener('click', async () => {
  if (store.get('bakin.mini.through-told', '') === '1') { await closePop(); tauri.core.invoke('mini_through', { on: true }); return; }
  openPop('ask');  // the first time: how to get back, before the clicks start going through
});
$('m-ask-no').addEventListener('click', () => closePop());
$('m-ask-yes').addEventListener('click', async () => {
  store.set('bakin.mini.through-told', '1');
  await closePop();
  tauri.core.invoke('mini_through', { on: true });
});
$('mm-tray').addEventListener('click', () => tauri.window.getCurrentWindow().close());
$('m-menu').addEventListener('keydown', (e) => {
  const items = [...$('m-menu').querySelectorAll('button')];
  const i = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
  } else if (e.key === 'Escape') { e.stopPropagation(); closePop(); }
});
addEventListener('keydown', (e) => {
  if (e.key === ' ' && !e.target.closest('button')) { e.preventDefault(); pc({ cmd: 'toggle' }); }
  else if (e.key === 'Escape' && !popping) expand();
  else if (e.key === 'Escape') closePop();
});
addEventListener('contextmenu', (e) => e.preventDefault());
// the browser's keys aren't Bakin's (find, print, reload…)
addEventListener('keydown', (e) => {
  if ((e.ctrlKey && !e.altKey && 'fgprsujhontel'.includes(e.key.toLowerCase()) && e.key.length === 1) || ['F3', 'F5', 'F7'].includes(e.key)) e.preventDefault();
});

// on top of other windows, or a normal window (0.24: «under everything» is gone)
const layer = (l) => {
  if (l !== 'normal') l = 'top';
  store.set('bakin.mini.layer', l);
  tauri.core.invoke('mini_layer', { layer: l });
};
layer(store.get('bakin.mini.layer', 'top'));
// the gaming mode: clicks go through, the song only; the shortcut or the tray brings the mouse back
const through = (on) => document.body.classList.toggle('through', on);
tauri.event.listen('mini', (e) => {
  if (e.payload?.layer) { layer(e.payload.layer); return; }  // the gaming shortcut: on top
  through(!!e.payload?.through);
  if (e.payload?.layout) showLayout(e.payload.layout);
});
tauri.core.invoke('mini_state').then((s) => { showLayout(s.layout); through(!!s.through); if (s.layer) layer(s.layer); }).catch(() => {});
// back to the full window: ⤢, a double click on the cover (or anywhere empty), Esc
$('m-expand').addEventListener('click', expand);
document.querySelector('.mini').addEventListener('dblclick', (e) => { if (!e.target.closest('button, .m-seek')) expand(); });
// the card's size: its corner (a square, 160–400 px; the shell keeps it square and remembers it)
$('m-grip').addEventListener('mousedown', (e) => { e.preventDefault(); tauri.window.getCurrentWindow().startResizeDragging('SouthEast'); });

pc({ cmd: 'state' }).then((s) => s && draw(s));
