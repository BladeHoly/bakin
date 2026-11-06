// «Modo mínimo»: a remote control for the player in the shell (player.rs),
// in a window of its own. It keeps nothing: it asks the player for its state
// and follows its events, like the big window does.
import { tr } from './i18n.js';  // first: translates the page before the components draw
import { tokensToCss } from '@glass-midas/core';
import './vendor/glass-midas/components/slider.js';

document.head.insertAdjacentHTML('beforeend', `<style>${tokensToCss()}</style>`);

const $ = (id) => document.getElementById(id);
const tauri = window.__TAURI__;
const pc = (cmd) => tauri.core.invoke('player_call', { cmd }).catch(() => null);
const store = {
  get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private storage off */ } },
};
const skipSecs = () => Number(store.get('bakin.skip', '10')) || 10;

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
  const vol = $('m-volume');
  if (document.activeElement !== vol && state.volume != null) { vol.value = String(Math.round(state.volume * 100)); vol.parentElement.sync?.(); }
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
// like the big player: a click jumps a few seconds, a right click changes the song
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
// volume: the slider, or the wheel anywhere
const setVolume = (v) => {
  state.volume = Math.max(0, Math.min(1, v));
  for (const id of ['m-volume', 'm-volume-v']) { $(id).value = String(Math.round(state.volume * 100)); $(id).parentElement.sync?.(); }
  pc({ cmd: 'set', volume: state.volume });
};
$('m-volume').addEventListener('input', (e) => setVolume(Number(e.target.value) / 100));
$('m-volume-v').addEventListener('input', (e) => setVolume(Number(e.target.value) / 100));
addEventListener('wheel', (e) => setVolume((state.volume ?? 0.8) + (e.deltaY < 0 ? 0.05 : -0.05)), { passive: true });

// What opens from ⋯ and 🔊 (0.13 review): vertical, in room the window makes for it (down, or up
// near the screen's bottom edge), never over the song; it folds when the pointer leaves.
const POP = { menu: 196, vol: 150 };
let popping = null;
async function openPop(kind) {
  if (popping === kind) { closePop(); return; }
  if (popping) await closePop();
  popping = kind;
  document.body.style.setProperty('--base-h', `${innerHeight}px`);
  const where = await tauri.core.invoke('mini_popup', { extra: POP[kind] }).catch(() => 'down');
  document.body.dataset.pop = where;
  document.body.dataset.popKind = kind;
  if (kind === 'vol') {
    const r = $('m-vol').getBoundingClientRect();
    $('m-volpop').style.left = `${Math.round(r.left + r.width / 2 - 20)}px`;
    $('m-volume-v').value = String(Math.round((state.volume ?? 0.8) * 100));
    $('m-volume-v').parentElement.sync?.();
  }
  syncMenu();
  $('m-pop').hidden = false;
  $('m-more').setAttribute('aria-expanded', String(kind === 'menu'));
  $('m-vol').setAttribute('aria-pressed', String(kind === 'vol'));
  if (kind === 'menu') $('m-menu').querySelector('button')?.focus();
}
async function closePop() {
  if (!popping) return;
  popping = null;
  $('m-pop').hidden = true;
  delete document.body.dataset.pop;
  delete document.body.dataset.popKind;
  $('m-more').setAttribute('aria-expanded', 'false');
  $('m-vol').setAttribute('aria-pressed', 'false');
  await tauri.core.invoke('mini_popup', { extra: 0 }).catch(() => {});
}
$('m-vol').addEventListener('click', () => openPop('vol'));
// the card shows its controls while the pointer is on it (CSS: body.hot)
document.addEventListener('pointerenter', () => document.body.classList.add('hot'));
document.addEventListener('pointerleave', () => document.body.classList.remove('hot'));
// «Atenuarse sin el mouse encima» (PLAN 0.17.4, Configuración › Música): 3 s after the pointer
// leaves, the small window fades to 55 %; it comes back on hover
let dimTimer = 0;
const dimOn = () => { try { return localStorage.getItem('bakin.mini.dim') === 'on'; } catch { return false; } };
document.addEventListener('pointerleave', () => { clearTimeout(dimTimer); if (dimOn()) dimTimer = setTimeout(() => document.body.classList.add('dim'), 3000); });
document.addEventListener('pointerenter', () => { clearTimeout(dimTimer); document.body.classList.remove('dim'); });
if (dimOn()) dimTimer = setTimeout(() => document.body.classList.add('dim'), 3000);
let leaving;
document.addEventListener('pointerleave', () => { leaving = setTimeout(closePop, 350); });
document.addEventListener('pointerenter', () => clearTimeout(leaving));

// the size: a bar, a card, the song only (the shell resizes the window and remembers it)
const LAYOUTS = { bar: tr('Barra'), card: tr('Tarjeta'), info: tr('Solo el tema') };
const NEXT = { bar: 'card', card: 'info', info: 'bar' };
const showLayout = (l) => {
  if (!LAYOUTS[l]) l = 'bar';
  document.body.dataset.layout = l;
  closePop();
  $('m-size').title = tr('Tamaño: {now} · clic: {next}', { now: LAYOUTS[l].toLowerCase(), next: LAYOUTS[NEXT[l]].toLowerCase() });
};
$('m-size').addEventListener('click', () => tauri.core.invoke('mini_layout', { layout: NEXT[document.body.dataset.layout] || 'bar' }));
// ⋯ (the song only): the window options, a vertical menu under it; «Cerrar» last, far from ⋯
$('m-more').addEventListener('click', () => openPop('menu'));
function syncMenu() {
  const l = $('m-pin').dataset.layer || 'top';
  $('mm-layer').querySelector('span').textContent = tr('Capa: {layer}', { layer: LAYERS[l].toLowerCase().replace(tr(' (sobre el escritorio)'), '') });
  $('mm-size').querySelector('span').textContent = tr('Tamaño: {size}', { size: LAYOUTS[NEXT[document.body.dataset.layout] || 'bar'].toLowerCase() });
}
$('mm-layer').addEventListener('click', () => { $('m-pin').click(); syncMenu(); });
$('mm-through').addEventListener('click', async () => { await closePop(); $('m-through').click(); });
$('mm-size').addEventListener('click', async () => { await closePop(); $('m-size').click(); });
$('mm-expand').addEventListener('click', () => tauri.core.invoke('mini_expand'));
$('mm-close').addEventListener('click', () => tauri.window.getCurrentWindow().close());
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
  else if (e.key === 'Escape') tauri.core.invoke('mini_expand');
});
addEventListener('contextmenu', (e) => e.preventDefault());
// the browser's keys aren't Bakin's (find, print, reload…)
addEventListener('keydown', (e) => {
  if ((e.ctrlKey && !e.altKey && 'fgprsujhontel'.includes(e.key.toLowerCase()) && e.key.length === 1) || ['F3', 'F5', 'F7'].includes(e.key)) e.preventDefault();
});

// 📌 cycles the layer: above everything, a normal window, under everything (on the desktop)
const LAYERS = { top: tr('Siempre encima'), normal: tr('Normal'), bottom: tr('Siempre debajo (sobre el escritorio)') };
const layer = (l) => {
  if (!LAYERS[l]) l = 'top';
  $('m-pin').dataset.layer = l;
  $('m-pin').title = tr('{layer} · clic: cambiar', { layer: LAYERS[l] });
  $('m-pin').setAttribute('aria-label', tr('Capa: {layer}', { layer: LAYERS[l].toLowerCase() }));
  store.set('bakin.mini.layer', l);
  tauri.core.invoke('mini_layer', { layer: l });
};
layer(store.get('bakin.mini.layer', 'top'));
$('m-pin').addEventListener('click', () => layer({ top: 'normal', normal: 'bottom', bottom: 'top' }[$('m-pin').dataset.layer]));
// transparent to the mouse: a picture only; the tray menu (or the full window) brings the mouse back
const through = (on) => {
  document.body.classList.toggle('through', on);
  $('m-through').setAttribute('aria-pressed', String(on));
};
$('m-through').addEventListener('click', () => {
  if (store.get('bakin.mini.through-told', '') !== '1') {
    store.set('bakin.mini.through-told', '1');
    tauri.core.invoke('notify', { title: tr('Modo mínimo: transparente al mouse'),
      body: tr('Los clics pasan a lo que está detrás. Para volver a usarlo: clic derecho en el ícono de Bakin en la bandeja › «Modo mínimo: usar el mouse».') }).catch(() => {});
  }
  tauri.core.invoke('mini_through', { on: true });
});
tauri.event.listen('mini', (e) => {
  if (e.payload?.layer) { layer(e.payload.layer); return; }  // the gaming shortcut: on top
  through(!!e.payload?.through);
  if (e.payload?.layout) showLayout(e.payload.layout);
});
tauri.core.invoke('mini_state').then((s) => { showLayout(s.layout); through(!!s.through); if (s.layer) layer(s.layer); }).catch(() => {});
$('m-cover').addEventListener('click', () => tauri.core.invoke('mini_expand'));
$('m-expand').addEventListener('click', () => tauri.core.invoke('mini_expand'));
$('m-close').addEventListener('click', () => tauri.window.getCurrentWindow().close());

pc({ cmd: 'state' }).then((s) => s && draw(s));
