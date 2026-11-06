// UI smoke test: drives a debug build of Bakin over the Chrome DevTools
// Protocol and fails on any broken view, dialog or script error.
// Run by ui-smoke.ps1 (which builds, seeds and launches the copy).
//
//   node ui-smoke.mjs <port> <seed folder with 300 images and 3 songs> <bakin exe> <tray seconds> <folder with a long song> <BAKIN_HOME> <WebView2 folder> <folder with 3500 tiny images>
const [port, seed, exe, traySeconds = '140', longDir, home, wv2, manyDir] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
const { execFileSync, spawn } = await import('node:child_process');
const fs = await import('node:fs');

// Budgets (packaging/budgets.json, docs/QA.md › Budgets): each measurement is
// kept for the table printed at the end; over budget fails the check unless
// the budget is warn_only.
const budgets = JSON.parse(fs.readFileSync(new URL('./budgets.json', import.meta.url), 'utf8'));
const measured = [];
function budget(key, value, unit) {
  const b = budgets[key];
  if (!Number.isFinite(value)) {  // a measurement that failed is never «within budget»
    measured.push({ key, value, unit, max: b.max, state: 'FALLA' });
    return `${key}: no se pudo medir (${value})`;
  }
  const over = value > b.max;
  measured.push({ key, value, unit, max: b.max, state: over ? (b.warn_only ? 'aviso' : 'FALLA') : 'ok' });
  return over && !b.warn_only ? `${key}: ${value} ${unit} (presupuesto: ${b.max} ${unit})` : '';
}
const ps = (script) => execFileSync('powershell.exe', ['-NoProfile', '-Command', script]).toString().trim();
// every process Bakin runs: the shell and its children (engine, WebView2, tools)
const tree = `
  $exe = '${exe.replace(/'/g, "''")}'
  $all = Get-CimInstance Win32_Process
  $ids = New-Object System.Collections.Generic.List[int]
  foreach ($p in $all | Where-Object { $_.ExecutablePath -eq $exe }) { $ids.Add([int]$p.ProcessId) }
  for ($i = 0; $i -lt $ids.Count; $i++) { foreach ($c in $all | Where-Object { $_.ParentProcessId -eq $ids[$i] }) { $ids.Add([int]$c.ProcessId) } }`;
// MB, counted like the Task Manager (private working set)
const memoryMB = () => Number(ps(`${tree}
  $t = 0; foreach ($id in $ids) { $q = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process -Filter "IDProcess=$id" -ErrorAction SilentlyContinue; if ($q -and $q.Name -notlike 'conhost*') { $t += $q.WorkingSetPrivate } }
  [math]::Round($t / 1MB)`));
// % of the whole machine (like the Task Manager) over a few seconds
const cpuPct = (seconds = 10) => Number(ps(`${tree}
  $cpu = { $s = 0; foreach ($id in $ids) { $p = Get-Process -Id $id -ErrorAction SilentlyContinue; if ($p) { $s += $p.TotalProcessorTime.TotalSeconds } }; $s }
  $a = & $cpu; Start-Sleep ${seconds}; $b = & $cpu
  ([math]::Round(($b - $a) / ${seconds} / [Environment]::ProcessorCount * 100, 2)).ToString([Globalization.CultureInfo]::InvariantCulture)`));
const problems = [];  // script errors seen at any time

async function connect() {
  for (let i = 0; i < 120; i++) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = targets.find((t) => t.type === 'page' && t.url.startsWith('http://tauri.localhost') && !t.url.includes('mini.html'));
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* not up yet */ }
    await sleep(500);
  }
  throw new Error('the window never came up');
}

let ws;
let id = 0;
const pending = new Map();
const onMessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return; }
  if (msg.method === 'Runtime.exceptionThrown') {
    const d = msg.params.exceptionDetails;
    problems.push(`exception: ${d.exception?.description || d.text}`);
  } else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    problems.push(`console.error: ${msg.params.args.map((a) => a.value ?? a.description).join(' ')}`);
  }
};
// every call answers within 30 s: a page that hangs fails its check instead of the whole test
const send = (method, params = {}) => new Promise((r, fail) => {
  const i = ++id;
  const t = setTimeout(() => { pending.delete(i); fail(new Error(`la página no respondió en 30 s (${method})`)); }, 30000);
  pending.set(i, (msg) => { clearTimeout(t); r(msg); });
  ws.send(JSON.stringify({ id: i, method, params }));
});
async function attach() {
  ws = new WebSocket(await connect());
  ws.onmessage = onMessage;
  await new Promise((r) => { ws.onopen = r; });
  await send('Runtime.enable');
}
await attach();

async function js(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || 'script error');
  return r.result.result?.value;
}
async function check(name, fn) {
  // SMOKE_ONLY=<text>: run only the checks whose name has it (to chase one failure)
  if (process.env.SMOKE_ONLY && !name.includes(process.env.SMOKE_ONLY)) return;
  try {
    const why = await fn();
    if (why) failures.push(`${name}: ${why}`);
    console.log(`${why ? 'FAIL' : 'ok  '} ${name}${why ? ` — ${why}` : ''}`);
  } catch (err) {
    failures.push(`${name}: ${err.message}`);
    console.log(`FAIL ${name} — ${err.message}`);
  }
}
const engine = (method, params = {}) => js(`window.__TAURI__.core.invoke('engine_call', ${JSON.stringify({ method, params })})`);

await check('el motor responde', async () => {
  for (let i = 0; i < 60; i++) {
    try { await engine('media_settings'); return ''; } catch { await sleep(1000); }
  }
  return 'media_settings no respondió en 60 s';
});

for (const [method, params] of [['library_overview', {}], ['cookies_status', {}], ['follows_list', {}], ['convert_formats', {}]]) {
  await check(`motor: ${method}`, async () => { await engine(method, params); return ''; });
}

await check('pestañas', async () => {
  for (const v of ['downloads', 'convert', 'music', 'photos']) {
    const ok = await js(`(() => { const s = document.getElementById('view'); s.value = '${v}'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: '${v}' } })); return !document.getElementById('view-${v === 'music' || v === 'photos' ? 'library' : v}').hidden && (['music', 'photos'].includes('${v}') ? document.body.dataset.space === '${v}' : true); })()`);
    if (!ok) return `la vista ${v} no se mostró`;
    await sleep(400);
  }
  return '';
});

await check('diálogos', async () => {
  await js(`(() => { const s = document.getElementById('view'); s.value = 'downloads'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'downloads' } })); })()`);
  await sleep(500);
  for (const [button, dialog] of [['btn-presets', 'preset-dialog'], ['btn-tools', 'tools-dialog'], ['btn-extension', 'extension-dialog']]) {
    const opened = await js(`(async () => { document.getElementById('${button}').click(); await new Promise((r) => setTimeout(r, 700)); const d = document.querySelector('gm-dialog[open], dialog[open]'); const ok = !!d; document.querySelectorAll('gm-dialog').forEach((g) => g.open && g.close()); return ok; })()`);
    if (!opened) return `${button} no abrió ningún diálogo (${dialog})`;
  }
  return '';
});

await check('configuración (cada ajuste en un solo lugar)', async () => {
  // every setting lives in Configuración, once (docs/QA.md C27)
  const ids = ['bg-keep', 'bg-notify', 'bg-autostart', 'clip-watch', 'po-skip', 'po-fade', 'out-dir', 'media-preset', 'media-limit',
    'hours-on', 'bg-tint', 'mini-remember', 'wp-paused', 'wh-user', 'wh-key', 'smooth-scroll'];
  const outside = await js(`${JSON.stringify(ids)}.filter((id) => document.querySelectorAll('#' + id).length !== 1 || !document.getElementById(id).closest('#settings-dialog'))`);
  if (outside.length) return `ajustes fuera de Configuración o repetidos: ${outside.join(', ')}`;
  const total = await js(`(async () => { document.getElementById('btn-settings').click(); await new Promise((r) => setTimeout(r, 500)); document.querySelector('#settings-nav [data-sec=usage]').click(); await new Promise((r) => setTimeout(r, 1500)); const t = document.getElementById('usage-total').textContent; document.getElementById('settings-dialog').close(); return t; })()`);
  if (!/^\d+ MB$/.test(total) || parseInt(total) < 5) return `Consumo no midió nada: ${total}`;
  return '';
});

await check('¿audio o video? (C42)', async () => {
  // a link with video (a saved yt-dlp -J, no network): «Video», and the music preset Bakin
  // starts with is switched to a video one by itself
  // open the format dialog the way «Elegir formato y preset…» does
  await js(`(() => { const s = document.getElementById('go'); const it = s.source('https://example.com/tema').find((a) => /formato/i.test(a.label)); s.dispatchEvent(new CustomEvent('gm-select', { detail: it, cancelable: true, bubbles: true })); return 1; })()`);
  let d;
  for (let i = 0; i < 20; i++) {
    await sleep(300);
    d = await js(`({ open: !!document.getElementById('media-dialog').open, title: document.getElementById('md-title').textContent, meta: document.getElementById('md-meta').textContent, analyzing: !document.getElementById('md-analyzing').hidden, shown: !document.getElementById('md-detected').hidden, kind: document.getElementById('md-detected-kind').textContent, why: document.getElementById('md-detected-why').textContent, preset: (() => { const b = document.getElementById('md-preset'); return (b.classList.contains('gm-select-trigger') ? b.parentElement.querySelector(':scope > select') : b).value; })() })`);
    if (d.shown) break;
  }
  const modes = await js(`window.__TAURI__.core.invoke('engine_call', { method: 'media_settings' }).then((m) => Object.fromEntries(m.presets.map((p) => [p.id, p.mode])))`);
  await js(`document.querySelector('gm-dialog#media-dialog')?.close?.()`);
  if (!d?.shown || d.kind !== 'Video') return `no se mostró «Video»: ${JSON.stringify(d)}`;
  if (modes[d.preset] !== 'video') return `no se eligió un preset de video: ${d.preset}`;
  return '';
});

await check('fondos de Wallhaven (C40)', async () => {
  // offline: the menu entry, the dialog, and Wallhaven links never going to yt-dlp
  const r = await js(`({ menu: !!document.querySelector('#hub-nav [data-sec=wallhaven]'), dialog: !!document.getElementById('download-hub'),
    one: document.getElementById('go').source('https://wallhaven.cc/w/94x38z').map((i) => i.label),
    search: document.getElementById('go').source('https://wallhaven.cc/search?q=space').map((i) => i.label) })`);
  if (!r.menu || !r.dialog) return `falta el menú o el diálogo: ${JSON.stringify(r)}`;
  if (r.one.length !== 1 || !/Wallhaven/.test(r.one[0])) return `enlace de un fondo: ${JSON.stringify(r.one)}`;
  if (r.search.length !== 1 || !/Wallhaven/.test(r.search[0])) return `enlace de búsqueda: ${JSON.stringify(r.search)}`;
  return '';
});

await check('menú de enlaces', async () => {
  const yt = await js(`document.getElementById('go').source('https://www.youtube.com/playlist?list=PLx').map((i) => i.label)`);
  if (!yt || yt.length < 3) return `YouTube: ${JSON.stringify(yt)}`;
  return '';
});

await check('biblioteca virtual (300 archivos)', async () => {
  const add = await engine('library_add_folder', { path: seed });
  if (add && add.ok === false) return `no se pudo agregar la carpeta: ${add.error}`;
  await js(`(() => { const s = document.getElementById('view'); s.value = 'photos'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'photos' } })); document.getElementById('btn-lib-refresh').click(); })()`);
  let state;
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    state = await js(`(() => { const c = document.getElementById('view-library'); c.scrollTop = c.scrollHeight; const card = document.querySelector('.lib-card'); return { total: Number(((document.getElementById('lib-count').textContent || document.getElementById('count-all').textContent).match(/\\d+/) || [0])[0]), allLoaded: document.getElementById('btn-lib-more').hidden, tiles: document.querySelectorAll('#lib-grid > .lib-tile').length, card: card.offsetHeight, content: card.scrollHeight }; })()`).catch(() => null);
    if (state && state.total >= 300 && state.allLoaded) break;  // Fotos: the 300 images, the songs live in Música
  }
  if (!state || state.total !== 300) return `Fotos debería mostrar las 300 imágenes y ninguna canción: ${JSON.stringify(state)}`;
  if (!state.allLoaded) return `no se cargaron todas las páginas: ${JSON.stringify(state)}`;
  if (state.tiles >= state.total) return `todas las tarjetas siguen en la página (${state.tiles}): no hay ventana virtual`;
  if (Math.abs(state.card - state.content) > 2) return `el panel (${state.card}px) no envuelve la grilla (${state.content}px)`;
  const top = await js(`(async () => { const c = document.getElementById('view-library'); c.scrollTop = 0; await new Promise((r) => setTimeout(r, 400)); const t = document.querySelector('#lib-grid > .lib-tile'); return t ? Number(t.dataset.vi) : -1; })()`);
  if (top !== 0) return `al volver arriba la primera tarjeta es ${top}`;
  // scrolling must not replay anything: no entry animation, tiles reused when they come back (QA C21)
  const flicker = await js(`(async () => { const c = document.getElementById('view-library'); const t = [...document.querySelectorAll('#lib-grid > .lib-tile')]; t.forEach((x) => { x.__seen = 1; }); c.scrollTop = 2500; await new Promise((r) => setTimeout(r, 600)); c.scrollTop = 0; await new Promise((r) => setTimeout(r, 600)); const now = [...document.querySelectorAll('#lib-grid > .lib-tile')]; return { anim: getComputedStyle(now[0]).animationName, reused: now.filter((x) => x.__seen).length, total: now.length }; })()`);
  if (flicker.anim !== 'none') return `las tarjetas se animan al volver a la vista (${flicker.anim})`;
  if (flicker.reused < flicker.total * 0.8) return `al volver arriba se redibujaron ${flicker.total - flicker.reused} de ${flicker.total} tarjetas`;
  // the viewer: one box for every photo, inside the window, no jump between photos (QA C22)
  const box = await js(`(async () => { document.querySelector('#lib-grid > .lib-tile').click(); await new Promise((r) => setTimeout(r, 900)); const st = document.getElementById('viewer-stage'); const sizes = new Set(); for (let k = 0; k < 3; k++) { document.getElementById('v-next').click(); for (const ms of [0, 30, 200]) { await new Promise((r) => setTimeout(r, ms)); const r = st.getBoundingClientRect(); sizes.add(Math.round(r.width) + 'x' + Math.round(r.height)); } } const img = st.querySelector('img')?.getBoundingClientRect(); const out = img && (img.bottom > innerHeight || img.right > innerWidth); document.getElementById('v-close').click(); return { sizes: [...sizes], out }; })()`);
  if (box.sizes.length !== 1) return `la caja del visor cambia de tamaño al pasar de foto: ${box.sizes.join(', ')}`;
  if (box.out) return 'la foto del visor se sale de la ventana';
  return '';
});

await check('poner de fondo por monitor (C44)', async () => {
  // never the real desktop: the copy must say it is a dry run before anything is set
  const r = await js(`window.__TAURI__.core.invoke('wallpaper_monitors')`);
  if (!r?.dry) return 'la copia de prueba no está en modo simulado (BAKIN_DRY_WALLPAPER): no se prueba para no cambiar el fondo real';
  if (r.monitors.length !== 2 || !r.monitors[1].vertical) return `monitores simulados mal leídos: ${JSON.stringify(r.monitors)}`;
  const dryLog = () => { try { return JSON.parse(fs.readFileSync(`${home}/wallpaper-dry.json`, 'utf8')); } catch { return []; } };
  const pick = async (value) => {
    for (let i = 0; i < 20; i++) {
      await sleep(150);
      const ok = await js(`(() => { const m = document.getElementById('wallpaper-menu'); if (!m?.matches(':popover-open')) return false; m.querySelector('button[value="${value}"]').click(); return true; })()`);
      if (ok) return true;
    }
    return false;
  };
  // right click on a photo → «Monitor 2 (vertical)»
  await js(`(() => { const s = document.getElementById('view'); s.value = 'photos'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'photos' } })); })()`);
  await sleep(1000);
  await js(`document.querySelector('#lib-grid > .lib-tile[data-kind="image"]').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))`);
  const items = await js(`new Promise((res) => setTimeout(() => res([...document.querySelectorAll('#wallpaper-menu > button:not([value=lock]):not([value=frame])')].map((b) => b.textContent)), 600))`);
  // the lock screen item only where Windows allows it; in a test copy it never changes it
  const lock = await js(`window.__TAURI__.core.invoke('lockscreen_set', { path: ${JSON.stringify(`${seed}/foto-000.png`)} })`);
  if (lock.set) return 'la copia de prueba cambió la pantalla de bloqueo';
  if (r.lock !== lock.supported) return `el menú y Windows no coinciden sobre la pantalla de bloqueo: ${r.lock} / ${lock.supported}`;
  if (r.lock !== (await js(`!!document.querySelector('#wallpaper-menu > button[value=lock]')`))) return '«Pantalla de bloqueo» no sigue a lo que permite Windows';
  if (items.length !== 3 || !/vertical/.test(items[1])) return `el menú no lista los dos monitores y «Todos»: ${JSON.stringify(items)}`;
  if (!(await pick('2'))) return 'el menú «Poner de fondo» no se abrió con clic derecho';
  await sleep(800);
  let log = dryLog();
  if (log.length !== 1 || log[0].monitor !== 2 || !/foto-\d+\.png$/.test(log[0].path)) return `no se pidió el fondo del monitor 2 con la foto: ${JSON.stringify(log)}`;
  // the viewer's button → all the monitors
  await js(`document.querySelector('#lib-grid > .lib-tile[data-kind="image"]').click()`);
  await sleep(900);
  await js(`(() => { document.getElementById('viewer-side').hidden = false; document.getElementById('v-wallpaper').click(); })()`);
  const inside = await pick('all');
  await sleep(800);
  await js(`document.getElementById('v-close').click()`);
  if (!inside) return 'el menú no se abrió desde el visor';
  log = dryLog();
  if (log.length !== 3 || log[1].monitor !== 1 || log[2].monitor !== 2) return `«Todos los monitores» no pidió los dos: ${JSON.stringify(log)}`;
  // each monitor gets its own copy framed to its shape (PLAN 0.13.7): the 64×80 test photo,
  // cut (never enlarged) to 64×36 for the 16:9 one and 45×80 for the vertical one, as PNG
  const pngSize = (f) => { const b = fs.readFileSync(f); return b.toString('ascii', 1, 4) === 'PNG' ? [b.readUInt32BE(16), b.readUInt32BE(20)] : null; };
  const framedSizes = log.slice(1).map((x) => (x.shown && x.shown !== x.path ? pngSize(x.shown) : null));
  if (JSON.stringify(framedSizes) !== '[[64,36],[45,80]]') return `las copias encuadradas no tienen la forma de cada monitor: ${JSON.stringify(framedSizes)}`;
  // only images
  const bad = await js(`window.__TAURI__.core.invoke('wallpaper_set', { path: ${JSON.stringify(`${home}/queue.json`)}, number: 1 }).then(() => 'aceptado', (e) => String(e))`);
  if (bad === 'aceptado' || dryLog().length !== 3) return 'aceptó un archivo que no es una imagen';
  return '';
});

await check('rotación de fondos por monitor (C45)', async () => {
  const st = await js(`window.__TAURI__.core.invoke('wallpaper_rotation_get')`);
  if (!st?.dry) return 'la copia de prueba no está en modo simulado: no se prueba la rotación';
  const dryLog = () => { try { return JSON.parse(fs.readFileSync(`${home}/wallpaper-dry.json`, 'utf8')); } catch { return []; } };
  const before = dryLog().length;
  // Configuración › Fondos de pantalla: a row per monitor
  await js(`(async () => { document.getElementById('btn-settings').click(); await new Promise((r) => setTimeout(r, 400)); document.querySelector('#settings-nav [data-sec=wallpapers]').click(); })()`);
  let rows = 0;
  for (let i = 0; i < 20 && rows < 2; i++) { await sleep(300); rows = await js(`document.querySelectorAll('#wp-list .wp-mon').length`); }
  if (rows !== 2) return `Fondos de pantalla muestra ${rows} monitores (esperaba 2)`;
  // the vertical one: from the test photos (64×80, vertical), every minute, only verticals
  const set = await js(`(() => {
    const row = document.querySelector('#wp-list .wp-mon[data-key="1080x1920"]');
    const [src, every] = row.querySelectorAll('select');
    const opt = [...src.options].find((o) => /Fotos de prueba/.test(o.textContent));
    if (!opt) return 'sin la carpeta de prueba entre las fuentes: ' + [...src.options].map((o) => o.textContent).join(', ');
    src.value = opt.value;
    const o = document.createElement('option'); o.value = '1'; o.textContent = 'cada minuto (prueba)'; every.append(o); every.value = '1';
    const on = row.querySelector('.gm-toggle'); on.checked = true; on.dispatchEvent(new Event('change'));
    return '';
  })()`);
  if (set) return set;
  // turning it on shows one right away
  let log = [];
  for (let i = 0; i < 20; i++) { await sleep(300); log = dryLog().slice(before); if (log.length) break; }
  if (log.length !== 1 || log[0].monitor !== 2) return `al activarla no se puso un fondo en el monitor 2: ${JSON.stringify(log)}`;
  // then, by itself, with the minute (the scheduler looks every 30 s)
  for (let i = 0; i < 30 && log.length < 2; i++) { await sleep(5000); log = dryLog().slice(before); }
  if (log.length < 2) return 'pasó más de 2 minutos y la rotación no cambió el fondo sola';
  // «Siguiente» now, and nothing repeats
  await js(`document.querySelector('#wp-list .wp-mon[data-key="1080x1920"] .wp-opts > button').click()`);
  await sleep(1500);
  log = dryLog().slice(before);
  const paths = log.map((x) => x.path);
  if (log.length < 3) return `«Siguiente» no cambió el fondo: ${log.length} cambios`;
  if (new Set(paths).size !== paths.length) return `se repitió un fondo: ${paths.join(', ')}`;
  if (log.some((x) => x.monitor !== 2)) return 'la rotación tocó otro monitor';
  // the horizontal one, only horizontals, from the same vertical photos: nothing suits it
  const n = await js(`window.__TAURI__.core.invoke('wallpaper_rotation_set', { key: '1920x1080', rot: { on: true, source: { kind: 'folder', label: 'prueba', folders: [${JSON.stringify(seed)}], metas: [] }, every_min: 30, order: 'random', fit: true } }).then((r) => r.changed)`);
  if (n !== 0) return `puso una foto vertical en el monitor horizontal (${n})`;
  // paused: «Siguiente» from the tray still works, but the time does not
  const paused = await js(`(async () => { const p = document.getElementById('wp-paused'); p.checked = true; p.dispatchEvent(new Event('change')); await new Promise((r) => setTimeout(r, 400)); return (await window.__TAURI__.core.invoke('wallpaper_rotation_get')).paused; })()`);
  if (!paused) return '«Pausar la rotación» no quedó guardado';
  // leave it off for the rest of the test
  await js(`(async () => { for (const key of ['1080x1920', '1920x1080']) await window.__TAURI__.core.invoke('wallpaper_rotation_set', { key, rot: { on: false, source: { kind: 'folder', label: 'x', folders: [], metas: [] }, every_min: 30, order: 'random', fit: true } }); await window.__TAURI__.core.invoke('wallpaper_rotation_pause', { paused: false }); document.getElementById('settings-dialog').close(); })()`);
  return '';
});

await check('filtros de fondos en Fotos (C46)', async () => {
  const toPhotos = `(() => { const s = document.getElementById('view'); s.value = 'photos'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'photos' } })); })()`;
  const toMusic = toPhotos.replaceAll("'photos'", "'music'");
  await js(toPhotos);
  await sleep(1200);
  if (await js(`document.getElementById('lib-wp').hidden`)) return 'los filtros de fondos no se ven en Fotos';
  await js(toMusic);
  await sleep(800);
  const inMusic = await js(`document.getElementById('lib-wp').hidden`);
  await js(toPhotos);
  await sleep(1200);
  if (!inMusic) return 'los filtros de fondos aparecen en Música';
  // the 300 test photos are 64×80: portrait, small
  const total = async (f) => (await engine('library_items', { filters: { kinds: ['image'], ...f }, offset: 0, limit: 1 })).total;
  const counts = { portrait: await total({ orient: 'portrait' }), landscape: await total({ orient: 'landscape' }), big: await total({ min_px: 1080 }) };
  if (counts.portrait < 300 || counts.landscape !== 0 || counts.big !== 0) return `forma / tamaño mal filtrados: ${JSON.stringify(counts)}`;
  // through the page: «Horizontales» empties the grid, «Verticales» fills it
  const shown = async (v) => js(`(async () => { const b = document.getElementById('lib-orient'); const s = b.classList.contains('gm-select-trigger') ? b.parentElement.querySelector(':scope > select') : b; s.value = '${v}'; s.dispatchEvent(new Event('change')); await new Promise((r) => setTimeout(r, 1500)); return document.querySelectorAll('#lib-grid > .lib-tile').length; })()`);
  const none = await shown('landscape');
  const some = await shown('portrait');
  await shown('');
  if (none !== 0 || some === 0) return `el filtro de forma de la página no anda: horizontales ${none}, verticales ${some}`;
  // every photo ends up with one dominant colour (worked out in the background)
  let sum = 0;
  const palette = await js(`[...document.querySelectorAll('#lib-color-grid .color-dot')].map((b) => b.value)`);
  for (let i = 0; i < 20; i++) {
    sum = 0;
    for (const c of palette) sum += await total({ color: c });
    if (sum >= 300) break;
    await sleep(1500);
  }
  if (palette.length !== 29 || sum < 300) return `colores: ${palette.length} en la paleta, ${sum} fotos con color`;
  // «Fondo · monitor 1»: the photo put on every monitor in C44 (monitor 2 rotated since)
  const badge = await js(`(() => { const b = document.querySelector('#lib-grid > .lib-tile .wp-badge'); return b ? b.title : ''; })()`);
  if (!/del monitor 1$/.test(badge)) return `la foto puesta de fondo no dice que es el fondo del monitor 1: «${badge}»`;
  return '';
});

await check('fondos claros de día, oscuros de noche (C48)', async () => {
  // runs after C46: every test photo has its colour by now
  const dryLog = () => { try { return JSON.parse(fs.readFileSync(`${home}/wallpaper-dry.json`, 'utf8')); } catch { return []; } };
  const before = dryLog().length;
  const hour = new Date().getHours();
  const day = hour >= 7 && hour < 19;
  const rot = (on) => ({ on, source: { kind: 'folder', label: 'prueba', folders: [seed], metas: [] }, every_min: 30, order: 'random', fit: false, daynight: true, cover: false });
  await js(`window.__TAURI__.core.invoke('wallpaper_rotation_set', { key: '1080x1920', rot: ${JSON.stringify(rot(true))} })`);
  for (let i = 0; i < 3; i++) await js(`window.__TAURI__.core.invoke('wallpaper_next', { key: '1080x1920' })`);
  await js(`window.__TAURI__.core.invoke('wallpaper_rotation_set', { key: '1080x1920', rot: ${JSON.stringify(rot(false))} })`);
  const picked = dryLog().slice(before).map((x) => x.path);
  if (picked.length !== 4) return `esperaba 4 fondos (al activar y 3 «Siguiente»), hubo ${picked.length}`;
  const items = JSON.parse(fs.readFileSync(`${home}/imageinfo.json`, 'utf8')).items;
  const lum = (h) => (0.2126 * parseInt(h.slice(0, 2), 16) + 0.7152 * parseInt(h.slice(2, 4), 16) + 0.0722 * parseInt(h.slice(4, 6), 16)) / 255;
  const colours = Object.entries(items).filter(([, v]) => v.c).map(([k, v]) => [k.split('|')[0].toLowerCase(), v.c]);
  const wanted = colours.filter(([, c]) => (lum(c) >= 0.5) === day).length;
  if (!wanted) return `ninguna foto de prueba es ${day ? 'clara' : 'oscura'}: no se puede probar`;
  for (const p of picked) {
    const c = colours.find(([k]) => k === p.toLowerCase())?.[1];
    if (!c) return `no se encontró el color de ${p} en imageinfo.json`;
    if ((lum(c) >= 0.5) !== day) return `a las ${hour} h eligió una foto ${day ? 'oscura' : 'clara'} (#${c}) habiendo ${wanted} que servían`;
  }
  return '';
});

await check('cuenta de Wallhaven y seguir búsquedas (C47)', async () => {
  // never the network here: a malformed key is refused before asking Wallhaven
  const acc = await engine('wallhaven_account');
  if (acc.key) return 'la copia de prueba ya tiene una clave de Wallhaven';
  const r = await js(`(async () => {
    document.getElementById('btn-settings').click();
    await new Promise((r) => setTimeout(r, 400));
    document.querySelector('#settings-nav [data-sec=accounts]').click();
    await new Promise((r) => setTimeout(r, 900));
    const key = document.getElementById('wh-key');
    const out = { type: key.type, clear: document.getElementById('wh-key-clear').hidden, status: document.getElementById('wh-key-status').textContent };
    key.value = 'corta';
    document.getElementById('wh-key-save').click();
    await new Promise((r) => setTimeout(r, 900));
    out.after = document.getElementById('wh-key-status').textContent;
    out.left = key.value;
    document.getElementById('settings-dialog').close();
    return out;
  })()`);
  if (r.type !== 'password') return 'la clave se ve en pantalla (el campo no es de contraseña)';
  if (!r.clear) return '«Quitar la clave» aparece sin clave guardada';
  if (!/no parece una clave/.test(r.after)) return `una clave inválida no avisó: «${r.after}»`;
  if (r.left) return 'la clave quedó escrita en la página después de guardar';
  if ((await engine('wallhaven_account')).key) return 'se guardó una clave inválida';
  // the hub: «Seguir…» with its intervals; no «Mis colecciones» without a key
  const hub = await js(`(async () => {
    document.getElementById('download-hub').show?.();
    await new Promise((r) => setTimeout(r, 300));
    const out = { follow: !!document.getElementById('wh-follow'), items: document.querySelectorAll('#wh-follow-menu > button:not([value=all])').length, mine: document.getElementById('wh-mine').hidden };
    document.getElementById('download-hub').close?.();
    return out;
  })()`);
  if (!hub.follow || hub.items !== 3) return `«Seguir…» no está o le faltan opciones: ${JSON.stringify(hub)}`;
  if (!hub.mine) return '«Mis colecciones» aparece sin clave';
  return '';
});

await check('panel de Wallhaven liviano (C49)', async () => {
  // 0.13 review: the grid lagged with big thumbnails. A saved page of 60 results (no network):
  // the small thumbnail, decoded off the main thread, tiles out of view not painted
  const opened = await js(`(async () => {
    document.querySelector('#hub-nav [data-sec=wallhaven]').click();
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 200));
      if (document.querySelectorAll('#wh-grid .wh-tile').length >= 60) return '';
    }
    return 'la grilla no mostró los 60 resultados';
  })()`);
  if (opened) return opened;
  // the panel's glass is the light one: refraction made the grid drop to ~20 fps the first time (C52)
  const glass = await js(`(() => { const d = document.getElementById('download-hub'); const g = d.shadowRoot?.querySelector('[data-glass-tier]') || d.querySelector('[data-glass-tier]') || d; return { variant: d.getAttribute('variant'), tier: g.dataset.glassTier || '' }; })()`);
  if (glass.variant === 'full' || glass.tier === 'refract') { await js(`document.getElementById('download-hub').close()`); return `el panel «Descargar» usa vidrio con refracción: ${JSON.stringify(glass)}`; }
  const t = await js(`(() => { const img = document.querySelector('#wh-grid .wh-tile img'); const tile = img.closest('.wh-tile');
    return { src: img.getAttribute('src'), decoding: img.decoding, cv: getComputedStyle(tile).contentVisibility, h: tile.offsetHeight }; })()`);
  // minimal (0.13 review): filters folded under «Filtros», the footer only ⋯ and «Bajar»
  const plain = await js(`({ folded: document.getElementById('wh-filters').hidden, foot: [...document.querySelectorAll('.wh-actions > button')].filter((b) => b.offsetParent).length })`);
  await js(`document.getElementById('download-hub').close()`);
  if (!plain.folded) return 'los filtros de Wallhaven no arrancan plegados';
  if (plain.foot > 2) return `el pie del panel tiene ${plain.foot} botones (esperaba ⋯ y «Bajar»)`;
  if (!t.src.includes('AAAAEAAAAB')) return 'la grilla carga la miniatura grande, no la chica';
  if (t.decoding !== 'async') return 'las miniaturas se decodifican en el hilo principal';
  if (t.cv !== 'auto' || t.h < 50) return `las tiles fuera de la vista se pintan igual (o colapsaron): ${JSON.stringify(t)}`;
  return '';
});

await check('desplazamiento suave opcional y sin demora (C50)', async () => {
  // 0.13 review: off by default (the system's scrolling); on, a notch moves at once and eases out
  // quickly (the browser's smooth scroll felt delayed), and quick notches add up
  const r = await js(`(async () => {
    const s = document.getElementById('view'); s.value = 'photos'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'photos' } }));
    await new Promise((r) => setTimeout(r, 1200));
    const box = document.getElementById('view-library');
    const wheel = () => { const e = new WheelEvent('wheel', { deltaY: 100, bubbles: true, cancelable: true }); document.querySelector('#lib-grid > .lib-tile').dispatchEvent(e); return e.defaultPrevented; };
    const out = { off: wheel() };  // off: the browser's own
    localStorage.setItem('bakin.smooth', 'on');
    box.scrollTop = 0; await new Promise((r) => setTimeout(r, 300));
    wheel();
    await new Promise((r) => setTimeout(r, 40));
    out.at40 = Math.round(box.scrollTop);
    await new Promise((r) => setTimeout(r, 300));
    out.done = Math.round(box.scrollTop);
    box.scrollTop = 0; await new Promise((r) => setTimeout(r, 300));
    for (let i = 0; i < 5; i++) { wheel(); await new Promise((r) => setTimeout(r, 30)); }
    await new Promise((r) => setTimeout(r, 500));
    out.five = Math.round(box.scrollTop);
    box.scrollTop = 0;
    localStorage.removeItem('bakin.smooth');
    return out;
  })()`);
  if (r.off) return 'con el desplazamiento suave apagado la página igual toma la rueda';
  if (r.at40 < 45) return `prendido, la muesca arranca con demora: ${JSON.stringify(r)}`;
  if (r.done < 95) return `prendido, la muesca no llega a su lugar: ${JSON.stringify(r)}`;
  if (r.five < 400) return `prendido, muescas rápidas no suman distancia: ${JSON.stringify(r)}`;
  return '';
});

await check('una pausa de YouTube no frena fondos ni conversiones (C51)', async () => {
  // 0.13 review: ~4000 liked songs waiting for tomorrow's limit kept Wallhaven out of the queue
  await engine('pause_queue', { reason: 'Se llegó al límite diario (prueba).', until: Date.now() / 1000 + 3600, scope: 'media' });
  const q = (await engine('get_state')).queue;
  await engine('resume_queue');
  if (!q.paused || q.scope !== 'media') return `la pausa no quedó solo para música y video: ${JSON.stringify({ paused: q.paused, scope: q.scope })}`;
  return '';
});

await check('un solo aviso que suma (C53)', async () => {
  // 0.13 review: every small Wallhaven job showed its own «1 fondo nuevo» and they stacked
  const r = await js(`(async () => {
    document.querySelectorAll('gm-toast').forEach((t) => t.remove());
    const done = { kind: 'finished', mode: 'media', outcome: 'ok', message: 'Listo: 1 fondo nuevo.', lane: 'wallhaven', count: 1, skipped: 0, dir: null };
    for (let i = 0; i < 3; i++) { await window.__TAURI__.event.emit('engine', done); await new Promise((r) => setTimeout(r, 300)); }
    await new Promise((r) => setTimeout(r, 300));
    const shown = [...document.querySelectorAll('gm-toast:not([leaving])')].map((t) => t.textContent);
    document.querySelectorAll('gm-toast').forEach((t) => t.remove());
    return shown;
  })()`);
  const ours = r.filter((t) => /fondo/.test(t));
  if (ours.length !== 1) return `${ours.length} avisos de fondos en vez de uno: ${JSON.stringify(r)}`;
  if (!/3 fondos nuevos/.test(ours[0])) return `el aviso no suma: «${ours[0]}»`;
  return '';
});

await check('colas: cada una con su estado y su botón (C54)', async () => {
  // 0.13 review: «having to resume the queue is weird». Music waits for tomorrow's limit on its own row;
  // its button resumes only that lane; empty lanes don't show
  await js(`(() => { const s = document.getElementById('view'); s.value = 'downloads'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'downloads' } })); })()`);
  await engine('pause_queue', { reason: 'Se llegó al límite diario (prueba).', until: Date.now() / 1000 + 3 * 3600, scope: 'media' });
  await sleep(800);
  const r = await js(`(() => ({ card: !document.getElementById('queue-card').hidden,
    rows: [...document.querySelectorAll('#lanes .lane')].map((l) => ({ lane: l.dataset.lane, text: l.textContent })) }))()`);
  if (!r.card) { await engine('resume_queue'); return 'la tarjeta de colas no se ve con la música en pausa'; }
  if (r.rows.length !== 1 || r.rows[0].lane !== 'media') { await engine('resume_queue'); return `filas: ${JSON.stringify(r.rows)}`; }
  if (!/Espera hasta (hoy|mañana)/.test(r.rows[0].text) || !/límite diario/.test(r.rows[0].text)) { await engine('resume_queue'); return `la fila de música no dice hasta cuándo ni por qué: «${r.rows[0].text}»`; }
  await js(`document.querySelector('#lanes .lane[data-lane=media] button').click()`);
  await sleep(800);
  const after = await engine('get_state');
  const card = await js(`document.getElementById('queue-card').hidden`);
  if (after.queue.paused) { await engine('resume_queue'); return '«Seguir ahora» no reanudó la música'; }
  if (!card) return 'la tarjeta de colas sigue a la vista sin nada en cola';
  return '';
});

await check('diálogos grandes con vidrio liviano (C56)', async () => {
  // 0.13 review: Configuración lagged (measured: refraction on a big dialog, music playing behind)
  const heavy = await js(`['settings-dialog', 'viewer', 'media-dialog', 'preset-dialog', 'tools-dialog', 'news-dialog', 'download-hub']
    .filter((id) => document.querySelector('#' + id + ' > .gm-dialog-panel')?.dataset.glassTier === 'refract')`);
  return heavy.length ? `con refracción: ${heavy.join(', ')}` : '';
});

await check('filtro de color con vuelta atrás (C57)', async () => {
  // 0.13 review: once a colour was chosen there was no way back
  const r = await js(`(async () => {
    const s = document.getElementById('view'); s.value = 'photos'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'photos' } }));
    await new Promise((r) => setTimeout(r, 800));
    const dot = document.querySelector('#lib-color-grid .color-dot');
    dot.click(); await new Promise((r) => setTimeout(r, 300));
    const set = { clear: !document.getElementById('lib-color-clear').hidden };
    dot.click(); await new Promise((r) => setTimeout(r, 300));  // the same colour again
    set.afterSame = document.getElementById('lib-color-clear').hidden;
    document.querySelector('#lib-color-grid .color-dot').click(); await new Promise((r) => setTimeout(r, 300));
    document.getElementById('lib-color-clear').click(); await new Promise((r) => setTimeout(r, 300));
    set.afterX = document.getElementById('lib-color-clear').hidden;
    set.popover = document.getElementById('lib-color-pop').matches(':popover-open');
    document.getElementById('lib-color-pop').close();
    return set;
  })()`);
  if (!r.clear) return 'al elegir un color no aparece la × para quitarlo';
  if (!r.afterSame) return 'tocar el mismo color no lo quita';
  if (!r.afterX) return 'la × no quita el color';
  if (r.popover) return 'la × abre la paleta en vez de solo quitar el color';
  return '';
});

await check('barra de selección integrada (C58)', async () => {
  // 0.13 review: the bar stacked under the filters and didn't fit the design
  const r = await js(`(async () => {
    document.getElementById('btn-select').click(); await new Promise((r) => setTimeout(r, 300));
    const out = { filters: getComputedStyle(document.getElementById('lib-filters')).display, bar: !document.getElementById('selbar').hidden,
      round: document.getElementById('btn-select').classList.contains('gm-chip-round'), shown: document.querySelectorAll('#selbar > .chips > button:not([hidden])').length };
    document.getElementById('sel-more').click(); await new Promise((r) => setTimeout(r, 300));
    out.menu = document.getElementById('sel-menu').matches(':popover-open');
    document.getElementById('sel-menu').close();
    document.getElementById('sel-done').click(); await new Promise((r) => setTimeout(r, 300));
    out.back = getComputedStyle(document.getElementById('lib-filters')).display !== 'none';
    return out;
  })()`);
  if (!r.round) return 'el botón «Seleccionar» no es el redondo con ícono';
  if (!r.bar || r.filters !== 'none') return `la barra no reemplaza a los filtros: ${JSON.stringify(r)}`;
  if (r.shown > 6) return `demasiadas acciones a la vista (${r.shown})`;
  if (!r.menu) return '⋯ no abre las demás acciones';
  if (!r.back) return 'al terminar no vuelven los filtros';
  return '';
});

await check('♥ desde la lista de música (C59)', async () => {
  // 0.13 review: making a song a favourite took opening it and the player's heart
  const r = await js(`(async () => {
    const s = document.getElementById('view'); s.value = 'music'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'music' } }));
    await new Promise((r) => setTimeout(r, 1500));
    const row = document.querySelector('#lib-grid .lib-row:not(.lib-row-head)');
    const heart = row.querySelector('.row-fav');
    const before = heart.dataset.on;
    const playingBefore = document.getElementById('pl-title').textContent;
    heart.click(); await new Promise((r) => setTimeout(r, 900));
    const after = row.isConnected ? row.querySelector('.row-fav').dataset.on : document.querySelector('#lib-grid .lib-row:not(.lib-row-head) .row-fav').dataset.on;
    heart.click(); await new Promise((r) => setTimeout(r, 900));  // back as it was
    return { before, after, played: document.getElementById('pl-title').textContent !== playingBefore };
  })()`);
  if (r.before === r.after) return `el ♥ de la fila no cambió: ${JSON.stringify(r)}`;
  if (r.played) return 'tocar el ♥ reprodujo el tema';
  return '';
});

await check('modo mínimo: tarjeta y opciones en «solo el tema» (C60, C61)', async () => {
  // 0.13 review: in the card the title sat on the cover; the song-only layout hid the layer and «transparente»
  await js(`window.__TAURI__.core.invoke('mini_open')`);
  let ok = false;
  for (let i = 0; i < 20 && !ok; i++) { await sleep(400); ok = !!(await jsIn('mini.html', `document.body?.dataset.layout`).catch(() => '')); }
  if (!ok) return 'no se abrió la ventana chica';
  await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_layout', { layout: 'card' })`);
  await sleep(800);
  // the card is the cover (0.13 review: it took 248 × 384): a small square, the controls over it on hover
  const card = await jsIn('mini.html', `(async () => {
    const c = document.getElementById('m-cover').getBoundingClientRect();
    const op = (id) => Number(getComputedStyle(id === 'm-play' ? document.getElementById(id).parentElement : document.getElementById(id)).opacity);  // the buttons fade as a group
    document.body.classList.remove('hot'); await new Promise((r) => setTimeout(r, 300));
    const rest = { play: op('m-play'), title: op('m-title') };
    document.body.classList.add('hot'); await new Promise((r) => setTimeout(r, 300));
    const hot = { play: op('m-play'), title: op('m-title') };
    document.body.classList.remove('hot');
    return { w: innerWidth, h: innerHeight, fills: c.width >= innerWidth - 1 && c.height >= innerHeight - 1, rest, hot };
  })()`);
  await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_layout', { layout: 'info' })`);
  await sleep(800);
  // ⋯: a vertical menu in room the window makes, under the song (not over it); «Cerrar» last,
  // far from ⋯; closing it gives the window its size back (0.13 review)
  const info = await jsIn('mini.html', `(async () => {
    const h0 = innerHeight;
    document.getElementById('m-more').click(); await new Promise((r) => setTimeout(r, 700));
    const items = [...document.querySelectorAll('#m-menu button')];
    const song = document.querySelector('.mini').getBoundingClientRect();
    const menu = document.getElementById('m-menu').getBoundingClientRect();
    const more = document.getElementById('m-more').getBoundingClientRect();
    const close = document.getElementById('mm-close').getBoundingClientRect();
    const out = { grew: innerHeight - h0, // a column (top to bottom, or bottom to top when it opened upwards)
      vertical: items.length >= 5 && new Set(items.map((b) => Math.round(b.getBoundingClientRect().top))).size === items.length
        && new Set(items.map((b) => Math.round(b.getBoundingClientRect().left))).size === 1,
      overSong: menu.top < song.bottom && menu.bottom > song.top, last: items.at(-1).id,
      far: items.every((b) => b === items.at(-1) || Math.abs(b.getBoundingClientRect().top - more.top) < Math.abs(close.top - more.top)) };
    document.getElementById('m-more').click(); await new Promise((r) => setTimeout(r, 700));
    out.back = innerHeight === h0;
    return out; })()`);
  // 🔊 in the bar: a vertical slider below, not a row over the song
  await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_layout', { layout: 'bar' })`);
  await sleep(700);
  const vol = await jsIn('mini.html', `(async () => {
    document.getElementById('m-vol').click(); await new Promise((r) => setTimeout(r, 700));
    const i = document.getElementById('m-volume-v'); const r = i.getBoundingClientRect();
    const out = { shown: r.height > 0, vertical: r.height > r.width * 2, title: getComputedStyle(document.getElementById('m-title')).display !== 'none' };
    document.getElementById('m-vol').click(); await new Promise((r) => setTimeout(r, 600));
    return out; })()`);
  await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_layout', { layout: 'bar' })`);
  await sleep(400);
  jsIn('mini.html', `window.__TAURI__.core.invoke('mini_expand')`).catch(() => {});
  await sleep(1500);
  if (card.w > 240 || Math.abs(card.w - card.h) > 2 || !card.fills) return `la tarjeta no es la portada en un cuadrado chico: ${JSON.stringify(card)}`;
  if (card.rest.play > 0.05 || card.rest.title > 0.05 || card.hot.play < 0.95 || card.hot.title < 0.95) return `los controles de la tarjeta no aparecen solo al pasar el mouse: ${JSON.stringify(card)}`;
  if (info.grew < 100 || !info.vertical || info.overSong) return `«solo el tema»: ⋯ no abre un menú vertical debajo del tema: ${JSON.stringify(info)}`;
  if (info.last !== 'mm-close' || !info.far) return `«Cerrar» no queda último y lejos del ⋯: ${JSON.stringify(info)}`;
  if (!info.back) return 'al cerrar el menú la ventana no volvió a su tamaño';
  if (!vol.shown || !vol.vertical || !vol.title) return `el volumen de la barra no es un deslizador vertical: ${JSON.stringify(vol)}`;
  return '';
});

await check('ajustar el encuadre antes de poner de fondo (C62)', async () => {
  // 0.13 review: a preview of the photo on the monitor's shape, and moving the crop
  const dryLog = () => { try { return JSON.parse(fs.readFileSync(`${home}/wallpaper-dry.json`, 'utf8')); } catch { return []; } };
  const before = dryLog().length;
  await js(`(() => { const s = document.getElementById('view'); s.value = 'photos'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'photos' } })); })()`);
  await sleep(1000);
  const r = await js(`(async () => {
    const tile = document.querySelector('#lib-grid > .lib-tile[data-kind="image"]');
    tile.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
    let m;
    for (let i = 0; i < 20 && !m; i++) { await new Promise((r) => setTimeout(r, 150)); m = document.querySelector('#wallpaper-menu:popover-open'); }
    if (!m) return { error: 'sin menú' };
    m.querySelector('button[value=frame]').click();
    for (let i = 0; i < 20 && !document.getElementById('frame-dialog').hasAttribute('open'); i++) await new Promise((r) => setTimeout(r, 150));
    await new Promise((r) => setTimeout(r, 400));
    document.querySelector('#frame-monitors [data-n="1"]').click();  // 16:9: the 64×80 test photo is cut top and bottom
    await new Promise((r) => setTimeout(r, 200));
    const box = document.getElementById('frame-box');
    const axis = box.dataset.axis;
    const top0 = box.offsetTop;
    for (let i = 0; i < 10; i++) box.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    const top1 = box.offsetTop;
    document.getElementById('frame-go').click();
    await new Promise((r) => setTimeout(r, 1200));
    return { axis, moved: top1 - top0, open: document.getElementById('frame-dialog').hasAttribute('open'), path: tile.dataset.path };
  })()`);
  if (r.error) return r.error;
  if (r.axis !== 'y' || r.moved <= 0) return `el recuadro no se movió hacia abajo: ${JSON.stringify(r)}`;
  if (r.open) return '«Poner en el monitor» no cerró el diálogo';
  const focus = await js(`window.__TAURI__.core.invoke('wallpaper_focus_get', { path: ${JSON.stringify(r.path)} })`);
  const f = focus['1920x1080'];
  if (!f || Math.abs(f[1] - 0.7) > 0.01) return `no guardó el encuadre: ${JSON.stringify(focus)}`;
  const log = dryLog().slice(before);
  const centred = dryLog().find((x) => x.path === r.path && x.monitor === 1 && x !== log.at(-1));
  if (log.length !== 1 || log[0].monitor !== 1 || log[0].path !== r.path) return `no se puso esa foto en el monitor 1: ${JSON.stringify(log)}`;
  if (centred && centred.shown === log[0].shown) return 'la copia es la misma que la centrada: no usó el encuadre';
  await js(`window.__TAURI__.core.invoke('wallpaper_focus_set', { path: ${JSON.stringify(r.path)}, w: 1920, h: 1080, x: 0.5, y: 0.5 })`);
  return '';
});

await check('atajos de teclado y modo gaming (C63)', async () => {
  // 0.13 review: customizable shortcuts, and «Modo gaming» (small window, on top, mouse through)
  const r = await js(`(async () => {
    document.getElementById('btn-settings').click(); await new Promise((r) => setTimeout(r, 400));
    document.querySelector('#settings-nav [data-sec=keys]').click(); await new Promise((r) => setTimeout(r, 800));
    const rows = () => Object.fromEntries([...document.querySelectorAll('#hotkeys .hotkey-key')].map((b) => [b.dataset.action, b.textContent]));
    const first = rows();
    const key = (id) => document.querySelector('#hotkeys .hotkey-key[data-action=' + id + ']');
    key('wallpaper').click(); await new Promise((r) => setTimeout(r, 300));
    dispatchEvent(new KeyboardEvent('keydown', { key: 'k', code: 'KeyK', ctrlKey: true, altKey: true, bubbles: true }));
    await new Promise((r) => setTimeout(r, 900));
    const set = rows().wallpaper;
    key('wallpaper').click(); await new Promise((r) => setTimeout(r, 300));
    dispatchEvent(new KeyboardEvent('keydown', { key: 'Backspace', code: 'Backspace', bubbles: true }));
    await new Promise((r) => setTimeout(r, 900));
    const cleared = rows().wallpaper;
    document.getElementById('settings-dialog').close();
    return { count: Object.keys(first).length, gaming: first.gaming, set, cleared };
  })()`);
  if (r.count !== 7) return `«Atajos de teclado» muestra ${r.count} acciones (esperaba 7)`;
  if (r.gaming !== 'Ctrl + Shift + G') return `el modo gaming no tiene Ctrl+Shift+G por defecto: «${r.gaming}»`;
  if (r.set !== 'Ctrl + Alt + K') return `no se grabó la combinación: «${r.set}»`;
  if (r.cleared !== 'Sin atajo') return `Retroceso no borró el atajo: «${r.cleared}»`;
  // gaming: on → the small window, letting the mouse through; again → the mouse back
  await js(`window.__TAURI__.core.invoke('hotkeys_run', { action: 'gaming' })`);
  let st = null;
  for (let i = 0; i < 20; i++) { await sleep(400); st = await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_state')`).catch(() => null); if (st?.through) break; }
  const pin = await jsIn('mini.html', `document.getElementById('m-pin').dataset.layer`).catch(() => '');
  await js(`window.__TAURI__.core.invoke('hotkeys_run', { action: 'gaming' })`);
  await sleep(800);
  const back = await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_state')`).catch(() => null);
  jsIn('mini.html', `window.__TAURI__.core.invoke('mini_expand')`).catch(() => {});
  await sleep(1500);
  if (!st?.through) return `el modo gaming no dejó la ventana chica transparente al mouse: ${JSON.stringify(st)}`;
  if (pin !== 'top') return `el modo gaming no la puso siempre encima: «${pin}»`;
  if (back?.through) return 'apretarlo otra vez no devolvió el mouse';
  return '';
});

await check('rueda rápida sin ir y volver (C65)', async () => {
  // 0.13 review: a fast flick went back and forth while the list drew itself. Real wheel input (CDP)
  await js(`(async () => { const s = document.getElementById('view'); s.value = 'photos'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'photos' } }));
    await new Promise((r) => setTimeout(r, 1200)); document.getElementById('view-library').scrollTop = 0; })()`);
  await sleep(500);
  const at = await js(`(() => { const r = document.getElementById('view-library').getBoundingClientRect(); return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }; })()`);
  const out = {};
  for (const dir of [1, -1]) {
    await js(`window.__t = []; window.__go = true; (() => { const b = document.getElementById('view-library'); const f = () => { window.__t.push(b.scrollTop); if (window.__go) requestAnimationFrame(f); }; requestAnimationFrame(f); })()`);
    for (let i = 0; i < 25; i++) { await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: at.x, y: at.y, deltaX: 0, deltaY: 120 * dir }); await sleep(16); }
    await sleep(900);
    out[dir > 0 ? 'down' : 'up'] = await js(`(() => { window.__go = false; const t = window.__t; let back = 0; for (let i = 1; i < t.length; i++) if ((t[i] - t[i - 1]) * ${dir} < -1) back++; return { moved: Math.round(Math.abs(t.at(-1) - t[0])), back }; })()`);
  }
  await js(`document.getElementById('view-library').scrollTop = 0`);
  if (out.down.back || out.up.back) return `la rueda rápida fue para atrás: ${JSON.stringify(out)}`;
  if (out.down.moved < 500 || out.up.moved < 500) return `la rueda rápida casi no movió la lista: ${JSON.stringify(out)}`;
  return '';
});

await check('la portada de un tema que se baja no es una foto (C66)', async () => {
  // 0.13 review: while a song downloaded, its cover showed in Fotos until it finished
  await engine('library_overview', { refresh: true });
  const names = (await engine('library_items', { filters: { kinds: ['image'] }, offset: 0, limit: 2000 })).items.map((i) => i.name);
  if (names.includes('Tema que baja.webp')) return 'la portada del tema que se está bajando aparece en Fotos';
  return '';
});

await check('scroll de una lista larga de música: fluido y sin frenarse (C69)', async () => {
  // test build feedback: scrolling the songs lagged. 600 songs, real wheel input (CDP, as Playwright),
  // frames timed in the page; the glide must not stop dead where the loaded part of the list ends
  const songs = process.env.BAKIN_SMOKE_SONGS;
  if (!songs) return 'falta la carpeta de 600 canciones';
  const add = await engine('library_add_folder', { path: songs });
  if (add && add.ok === false) return `no se pudo agregar la carpeta: ${add.error}`;
  await js(`(async () => { document.getElementById('btn-lib-refresh').click(); await new Promise((r) => setTimeout(r, 2500));
    const s = document.getElementById('view'); s.value = 'music'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'music' } }));
    await new Promise((r) => setTimeout(r, 2500)); document.getElementById('view-library').scrollTop = 0; })()`);
  await sleep(800);
  const at = await js(`(() => { const r = document.getElementById('view-library').getBoundingClientRect(); return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }; })()`);
  await js(`window.__f = []; window.__go = true; (() => { let last = performance.now(); const f = (t) => { window.__f.push(t - last); last = t; if (window.__go) requestAnimationFrame(f); }; requestAnimationFrame(f); })()`);
  for (let i = 0; i < 40; i++) {
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: at.x, y: at.y, deltaX: 0, deltaY: 100 });
    await sleep(25);
  }
  await sleep(1500);
  const r = await js(`(() => { window.__go = false; const f = window.__f.slice(2).sort((a, b) => a - b);
    return { frames: f.length, p95: Math.round(f[Math.floor(f.length * 0.95)]), worst: Math.round(f.at(-1)), top: Math.round(document.getElementById('view-library').scrollTop) }; })()`);
  console.log(`     scroll de 600 temas: p95 ${r.p95} ms, peor ${r.worst} ms, avanzó ${r.top} px de 4000`);
  await js(`document.getElementById('view-library').scrollTop = 0`);
  await engine('library_remove_folder', { collection: (await engine('library_overview')).collections.find((c) => /Canciones/.test(c.name))?.id });
  // leave the library as the next checks expect it (the page doesn't hear about the folder going)
  await js(`(async () => { document.getElementById('btn-lib-refresh').click(); await new Promise((r) => setTimeout(r, 2500)); })()`);
  // 144 Hz frames come in 7 ms steps: 25 ms = at most two skipped; a stall is > 50 ms
  if (r.p95 > 25 || r.worst > 50) return `el scroll de la música no es fluido: ${JSON.stringify(r)}`;
  if (r.top < 3200) return `el scroll se frenó antes de tiempo (la lista cargando por partes): ${JSON.stringify(r)}`;
  return '';
});

await check('barra de arriba: pestañas elegibles, búsqueda centrada (C74)', async () => {
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const shown = () => [...document.querySelectorAll('#view > button')].filter((b) => !b.hidden).map((b) => b.value).join(',');
    const before = localStorage.getItem('bakin.tabs');
    localStorage.removeItem('bakin.tabs');
    document.querySelector('#view > button[value=music]').click(); await wait(300);
    const r = { def: shown(), label: document.querySelector('#view > button[value=photos]').textContent.trim() };
    document.getElementById('view-more').click(); await wait(300);
    r.menu = document.getElementById('view-menu').matches(':popover-open');
    document.querySelector('#view-menu button[value=convert]').click(); await wait(200);
    r.stillOpen = document.getElementById('view-menu').matches(':popover-open');
    r.added = shown(); r.saved = localStorage.getItem('bakin.tabs');
    document.getElementById('view-menu').close();
    localStorage.removeItem('bakin.tabs');
    document.querySelector('#view > button[value=downloads]').click(); await wait(300);
    r.openHidden = shown();
    document.querySelector('#view > button[value=music]').click(); await wait(300);
    r.back = shown();
    if (before !== null) localStorage.setItem('bakin.tabs', before);
    r.winPill = !!document.querySelector('.win-controls')?.closest('gm-surface');
    return r;
  })()`);
  if (r.def !== 'music,photos' || r.label !== 'Imágenes') return `por defecto deberían verse Música e Imágenes: ${JSON.stringify(r)}`;
  if (!r.menu || !r.stillOpen || r.added !== 'music,photos,convert' || r.saved !== 'convert') return `el «+» no agrega Convertir: ${JSON.stringify(r)}`;
  if (r.openHidden !== 'music,photos,downloads' || r.back !== 'music,photos') return `una vista oculta abierta debería mostrar su pestaña mientras está: ${JSON.stringify(r)}`;
  // the search sits in the middle of a wide window (1920 px), the sidebar open or not
  await send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
  await sleep(500);
  const off = [];
  for (let i = 0; i < 2; i++) {
    off.push(await js(`(() => { const s = document.querySelector('.tb-center').getBoundingClientRect(); return Math.round(Math.abs((s.left + s.right) / 2 - innerWidth / 2)); })()`));
    await js(`document.getElementById('btn-side').click()`);
    await sleep(400);
  }
  await send('Emulation.clearDeviceMetricsOverride');
  await sleep(300);
  if (off.some((o) => o > 4)) return `la búsqueda no está centrada en la ventana: ${off.join(' / ')} px`;
  if (r.winPill) return 'los botones de la ventana siguen dentro de una píldora';
  return '';
});

await check('sin el menú ni los atajos del navegador (C75)', async () => {
  const r = await js(`(() => {
    const fire = (el, ev) => { el.dispatchEvent(ev); return ev.defaultPrevented; };
    const key = (k, ctrl = true) => fire(document.body, new KeyboardEvent('keydown', { key: k, ctrlKey: ctrl, bubbles: true, cancelable: true }));
    return {
      menu: fire(document.querySelector('.lib-card') || document.body, new MouseEvent('contextmenu', { bubbles: true, cancelable: true })),
      input: fire(document.getElementById('lib-q'), new MouseEvent('contextmenu', { bubbles: true, cancelable: true })),
      g: key('g'), f: key('f'), p: key('p'), r: key('r'), f5: key('F5', false),
    };
  })()`);
  if (!r.menu) return 'el clic derecho sigue abriendo el menú de Chromium';
  if (r.input) return 'los campos de texto perdieron su menú (copiar, pegar)';
  if (!r.g || !r.f || !r.p || !r.r || !r.f5) return `atajos del navegador sin bloquear: ${JSON.stringify(r)}`;
  return '';
});

await check('barra lateral colapsada: alineada, tags y sin envoltorio (C76)', async () => {
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const before = localStorage.getItem('bakin.side');
    localStorage.setItem('bakin.side', 'open');
    document.querySelector('#view > button[value=photos]').click(); await wait(500);
    document.getElementById('btn-side').click(); await wait(500);
    const mid = (el) => { const b = el.getBoundingClientRect(); return (b.left + b.right) / 2; };
    const fold = document.getElementById('tags-fold');
    const r = { collapsed: document.body.classList.contains('side-collapsed'),
      toggleOff: Math.round(Math.abs(mid(document.getElementById('btn-side')) - mid(document.getElementById('side-library')))),
      foldShown: fold.getBoundingClientRect().height > 20 };
    // unfolding: the cards whose shape changes a lot frost plainly until their new map is drawn
    // (glass-midas core 0.3.1), and refract again after
    const card = [...document.querySelectorAll('#side-library gm-surface[data-glass-tier=refract]')].find((e) => e.offsetParent);
    const bent = () => (card.style.getPropertyValue('backdrop-filter') || '').includes('url(');
    fold.click(); await wait(40);
    r.unfolded = !document.body.classList.contains('side-collapsed');
    r.settling = !bent();
    await wait(500);
    r.settled = !bent();
    if (before !== null) localStorage.setItem('bakin.side', before); else localStorage.removeItem('bakin.side');
    document.querySelector('#view > button[value=music]').click(); await wait(300);
    return r;
  })()`);
  if (!r.collapsed || r.toggleOff > 3) return `el ☰ no queda sobre la columna colapsada: ${JSON.stringify(r)}`;
  if (!r.foldShown || !r.unfolded) return `en Imágenes colapsada, los tags deberían ser un botón que abre la barra: ${JSON.stringify(r)}`;
  if (!r.settling || r.settled) return `el vidrio no se asienta al cambiar de forma: ${JSON.stringify(r)}`;
  return '';
});

await check('barra de arriba pareja a cualquier ancho (C78)', async () => {
  // (user, prueba.7) spacing and sizes, and a narrow window: one height for the whole bar, nothing
  // on top of something else, settings always there, the song count in the title without sidebar
  const bad = [];
  for (const w of [1600, 1366, 1000, 760, 520]) {
    await send('Emulation.setDeviceMetricsOverride', { width: w, height: 760, deviceScaleFactor: 1, mobile: false });
    await sleep(500);
    const r = await js(`(() => {
      const sel = ['#view', '#view-more', '.top-search', '.top-dl', '#btn-settings', '#win-min', '#win-max', '#win-close']
        .filter((s) => document.querySelector(s).offsetParent);
      const boxes = sel.map((s) => document.querySelector(s).getBoundingClientRect());
      const heights = [...new Set(boxes.map((b) => Math.round(b.height)))];
      const overlaps = [];
      for (let i = 1; i < boxes.length; i++) if (boxes[i].left < boxes[i - 1].right - 1) overlaps.push(sel[i - 1] + '/' + sel[i]);
      const out = boxes.filter((b) => b.right > innerWidth + 1 || b.width < 20).length;
      return { heights, overlaps, out, count: document.getElementById('lib-count').textContent };
    })()`);
    if (r.heights.length > 1) bad.push(`${w}px: alturas ${r.heights.join('/')}`);
    if (r.overlaps.length || r.out) bad.push(`${w}px: se pisan ${r.overlaps.join(', ')}${r.out ? ' / afuera o aplastados: ' + r.out : ''}`);
    if (w <= 900 && !r.count) bad.push(`${w}px: sin el número de temas en el título`);
  }
  await send('Emulation.clearDeviceMetricsOverride');
  await sleep(400);
  return bad.join(' · ');
});

await check('Configuración por categorías (C79)', async () => {
  // (user, after 0.13.1) shortcuts in their own category, and each setting where one looks for it
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    document.getElementById('btn-settings').click(); await wait(400);
    const secOf = (id) => document.getElementById(id)?.closest('section[data-sec]')?.dataset.sec;
    const where = Object.fromEntries(['hotkeys', 'bg-tint', 'smooth-scroll', 'mini-remember', 'wh-key', 'bg-keep', 'out-dir', 'po-fade', 'perf-hourly', 'perf-covers'].map((id) => [id, secOf(id)]));
    const navs = [...document.querySelectorAll('#settings-nav .scope')].map((b) => b.dataset.sec);
    const sections = [...document.querySelectorAll('#settings-dialog section[data-sec]')].map((x) => x.dataset.sec);
    // each nav entry shows its own section, and only it
    const shows = [];
    for (const sec of navs) {
      document.querySelector('#settings-nav [data-sec=' + sec + ']').click(); await wait(150);
      const open = [...document.querySelectorAll('#settings-dialog section[data-sec]')].filter((x) => !x.hidden).map((x) => x.dataset.sec);
      if (open.join() !== sec) shows.push(sec + '→' + open.join('+'));
    }
    // the tab switches in Apariencia follow the «+» and the other way round
    const before = localStorage.getItem('bakin.tabs');
    localStorage.removeItem('bakin.tabs');
    document.querySelector('#settings-nav [data-sec=appearance]').click(); await wait(150);
    const t = document.querySelector('.tab-toggle[value=convert]');
    t.checked = true; t.dispatchEvent(new Event('change'));
    const shown = !document.querySelector('#view > button[value=convert]').hidden;
    const menu = document.querySelector('#view-menu button[value=convert]').getAttribute('aria-checked');
    t.checked = false; t.dispatchEvent(new Event('change'));
    if (before !== null) localStorage.setItem('bakin.tabs', before); else localStorage.removeItem('bakin.tabs');
    document.getElementById('settings-dialog').close();
    return { where, navs, sections, shows, shown, menu };
  })()`);
  const want = { hotkeys: 'keys', 'bg-tint': 'appearance', 'smooth-scroll': 'appearance', 'mini-remember': 'music', 'wh-key': 'accounts', 'bg-keep': 'general', 'out-dir': 'downloads', 'po-fade': 'music', 'perf-hourly': 'perf', 'perf-covers': 'perf' };
  const wrong = Object.entries(want).filter(([id, sec]) => r.where[id] !== sec).map(([id]) => `${id} en ${r.where[id]}`);
  if (wrong.length) return `ajustes fuera de su categoría: ${wrong.join(', ')}`;
  if (r.navs.join() !== r.sections.join()) return `el menú y las secciones no coinciden: ${r.navs.join()} / ${r.sections.join()}`;
  if (r.shows.length) return `secciones que no se muestran solas: ${r.shows.join(', ')}`;
  if (!r.shown || r.menu !== 'true') return `el interruptor de pestañas no sigue al «+»: ${JSON.stringify(r)}`;
  return '';
});

await check('avisos encima de los diálogos (C80)', async () => {
  // glass-midas 0.8.1 does it now (Bakin's own stand-in is gone): a toast fired with Configuración
  // open must show over it, in the top layer
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    document.getElementById('btn-settings').click(); await wait(400);
    const region = () => document.querySelector('.gm-toasts');
    const modal = !!document.querySelector('dialog:modal');
    const { toast } = await import('./vendor/glass-midas/components/index.js');
    toast('Prueba', { duration: 800 });
    await wait(100);
    const r = { modal, popover: region()?.getAttribute('popover'), open: region()?.matches(':popover-open') };
    document.getElementById('settings-dialog').close();
    return r;
  })()`);
  if (!r.modal) return 'Configuración no abrió como diálogo modal';
  if (r.popover !== 'manual' || !r.open) return `el aviso no queda encima del diálogo: ${JSON.stringify(r)}`;
  return '';
});

await check('los temas aparecen mientras baja una lista, sin saltar (C81)', async () => {
  // a friend (2026-11-06): many songs downloaded and none showed until Bakin restarted. Each song
  // now re-reads the library (at most every 4 s), in place: the list keeps where it was.
  const songs = process.env.BAKIN_SMOKE_SONGS;
  if (!songs) return 'falta la carpeta de 600 canciones';
  await engine('library_add_folder', { path: songs });
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    document.querySelector('#view > button[value=music]').click(); await wait(300);
    document.querySelector('#music-views [data-mview=songs]').click(); await wait(300);
    document.getElementById('btn-lib-refresh').click(); await wait(2500);
    const sc = document.getElementById('view-library');
    sc.scrollTop = 3000; await wait(400);
    const firstSeen = () => { const top = sc.getBoundingClientRect().top; return [...document.querySelectorAll('#lib-grid .lib-row:not(.lib-row-head)')].find((x) => x.getBoundingClientRect().bottom > top + 60)?.dataset.id; };
    const before = { top: sc.scrollTop, id: firstSeen() };
    // count the re-reads while ten songs land in two seconds (each one asks the shell for
    // sync_media_scope once; __TAURI__ is frozen, so the IPC requests are counted)
    performance.setResourceTimingBufferSize(5000);
    performance.clearResourceTimings();
    let blank = 0;
    const watch = setInterval(() => { if (!document.querySelector('#lib-grid .lib-row:not(.lib-row-head)')) blank++; }, 50);
    for (let i = 0; i < 10; i++) { await window.__TAURI__.event.emit('engine', { kind: 'media_file', id: 'x' + i, title: 'x', path: 'x' }); await wait(200); }
    await wait(5500);
    clearInterval(watch);
    const reads = performance.getEntriesByType('resource').filter((e) => e.name.endsWith('/sync_media_scope')).length;
    return { before, after: { top: sc.scrollTop, id: firstSeen() }, reads, blank };
  })()`);
  await engine('library_remove_folder', { collection: (await engine('library_overview')).collections.find((c) => /Canciones/.test(c.name))?.id });
  await js(`(async () => { document.getElementById('btn-lib-refresh').click(); await new Promise((r) => setTimeout(r, 2500)); })()`);
  if (!r.reads) return 'llegan temas nuevos y la lista no se vuelve a leer';
  if (r.reads > 2) return `se relee la biblioteca ${r.reads} veces por 10 temas en 2 s (máximo 2)`;
  if (Math.abs(r.after.top - r.before.top) > 4 || r.after.id !== r.before.id) return `la lista saltó al releerse: ${JSON.stringify(r)}`;
  if (r.blank) return `la lista quedó en blanco ${r.blank} veces mientras se releía`;
  return '';
});

await check('buscar algo que no tengo ofrece YouTube Music (C82)', async () => {
  // the friend typed an artist in the library search to find new music and saw an empty list
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    document.querySelector('#view > button[value=music]').click(); await wait(300);
    const q = document.getElementById('lib-q');
    q.value = 'qwzx artista inexistente'; q.dispatchEvent(new Event('input', { bubbles: true })); await wait(1500);
    const b = document.getElementById('lib-empty-web');
    const out = { shown: !b.hidden, text: b.textContent, title: document.getElementById('lib-empty-title').textContent };
    q.value = ''; q.dispatchEvent(new Event('input', { bubbles: true })); await wait(1200);
    out.gone = b.hidden || document.getElementById('lib-empty').hidden;
    return out;
  })()`);
  if (!r.shown || !/YouTube Music/.test(r.text) || !/qwzx/.test(r.title)) return `sin la oferta de buscar en YouTube Music: ${JSON.stringify(r)}`;
  if (!r.gone) return 'la oferta queda al borrar la búsqueda';
  return '';
});

await check('cookies guardadas en Bakin, cifradas (C83)', async () => {
  // 2026-11-07, the user: their cookies.txt sat in plain text in Downloads. Imported into the
  // vault, a preset uses it, Configuración › Cuentas says so, and the page never holds a value.
  const fs = await import('node:fs');
  const path = await import('node:path');
  const fake = path.join(home, 'cookies-de-prueba.txt');
  const exp = Math.floor(Date.now() / 1000) + 86400 * 30;
  fs.writeFileSync(fake, `# Netscape HTTP Cookie File
.youtube.com	TRUE	/	TRUE	${exp}	SID	valor-falso-C83
`);
  const imp = await engine('cookies_import', { path: fake });
  if (!imp?.ok) return `no se importaron: ${JSON.stringify(imp)}`;
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    document.getElementById('btn-settings').click(); await wait(400);
    document.querySelector('#settings-nav [data-sec=accounts]').click(); await wait(1200);
    const out = { status: document.getElementById('cv-status').textContent,
      del: !document.getElementById('cv-delete-file').hidden, leaked: document.documentElement.outerHTML.includes('valor-falso-C83') };
    document.getElementById('settings-dialog').close();
    return out;
  })()`);
  const stored = fs.readdirSync(path.join(home, 'vault')).filter((f) => f.endsWith('.bin'));
  const plainInVault = stored.some((f) => fs.readFileSync(path.join(home, 'vault', f)).includes('valor-falso-C83'));
  const del = await engine('cookies_delete_original');
  const gone = !fs.existsSync(fake);
  await engine('cookies_forget');
  if (!/cifradas/.test(r.status) || !r.del) return `Cuentas no muestra las cookies guardadas: ${JSON.stringify(r)}`;
  if (r.leaked) return 'el valor de una cookie llegó a la página';
  if (!stored.length || plainInVault) return 'la bóveda guarda las cookies en texto plano';
  if (!del?.ok || !gone) return `no se borró el archivo original: ${JSON.stringify(del)}`;
  return '';
});

await check('biblioteca limpia: limpiar títulos, duplicados, estadísticas, radio (C84–C88)', async () => {
  // PLAN 0.14. The smoke's songs are WAV (no tags to write): the dialogs open and say so, a pair
  // of copies makes a group (closed without resolving: tests never touch the Recycle Bin), the
  // statistics count what's played, «Más escuchadas» shows its counts, a row's menu starts a radio.
  const fs = await import('node:fs');
  const path = await import('node:path');
  const dir = path.join(path.dirname(seed), 'Duplicados de prueba');  // not inside BAKIN_HOME: Bakin refuses its own data
  fs.mkdirSync(dir, { recursive: true });
  const wav = fs.readdirSync(seed).find((f) => f.endsWith('.wav'));
  fs.copyFileSync(path.join(seed, wav), path.join(dir, 'Artista Doble - Tema Repetido.wav'));
  fs.copyFileSync(path.join(seed, wav), path.join(dir, 'Artista Doble - Tema Repetido (Official Video).wav'));
  const add = await engine('library_add_folder', { path: dir });
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    document.querySelector('#view > button[value=music]').click(); await wait(300);
    document.getElementById('btn-lib-refresh').click(); await wait(2500);
    document.querySelector('#music-views [data-mview=songs]').click(); await wait(1200);
    const out = {};
    // C84: the clean-up dialog proposes (these WAV names split into artist and title)
    document.getElementById('mus-more').click(); await wait(300);
    document.querySelector('#mus-menu button[value=clean]').click(); await wait(1500);
    out.cleanOpen = document.getElementById('clean-dialog').open;
    out.proposals = document.querySelectorAll('#cl-list .cl-row').length;
    out.hint = document.querySelector('#cl-list > p.hint')?.textContent || '';
    document.getElementById('cl-close').click(); await wait(300);
    // C85: duplicates: the two copies, the clean one marked
    document.getElementById('mus-more').click(); await wait(300);
    document.querySelector('#mus-menu button[value=dupes]').click(); await wait(1500);
    out.groups = [...document.querySelectorAll('#du-list .du-group')].map((g) => g.querySelectorAll('label').length);
    document.getElementById('du-close').click(); await wait(300);
    // C86: statistics after two plays
    const ids = (await window.__TAURI__.core.invoke('engine_call', { method: 'library_items', params: { filters: { kinds: ['audio'], music: { view: 'songs' } }, offset: 0, limit: 2 } })).items.map((i) => i.id);
    for (const id of [ids[0], ids[0], ids[1]]) await window.__TAURI__.core.invoke('engine_call', { method: 'music_played', params: { item_id: id } });
    document.querySelector('#music-views [data-mview=stats]').click(); await wait(1500);
    out.statsShown = !document.getElementById('lib-stats').hidden && document.getElementById('lib-grid').hidden;
    out.plays = Number(document.getElementById('st-plays').textContent);
    out.topSongs = document.querySelectorAll('#st-top-songs li b').length;
    // C87: «Más escuchadas» counts
    document.querySelector('#music-views [data-mview=top]').click(); await wait(1500);
    out.counts = [...document.querySelectorAll('#lib-grid .lib-row:not(.lib-row-head) .c-plays')].map((c) => c.textContent);
    out.countHead = document.querySelector('#lib-grid .lib-row-head .c-bitrate')?.textContent;
    // C88: a row's right click → radio from that song
    const row = document.querySelector('#lib-grid .lib-row:not(.lib-row-head)');
    row.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true })); await wait(500);
    document.querySelector('#row-menu button[value=radio]').click(); await wait(1500);
    out.radioTitle = document.getElementById('lib-title').textContent;
    out.radioFirst = document.querySelector('#lib-grid .lib-row:not(.lib-row-head)')?.dataset.id === row.dataset.id;
    out.artistShuffle = !!document.querySelector('#shuffle-menu button[value=artist]');
    document.querySelector('#music-views [data-mview=songs]').click(); await wait(800);
    return out;
  })()`);
  await engine('library_remove_folder', { collection: add.id });
  await js(`(async () => { document.getElementById('btn-lib-refresh').click(); await new Promise((r) => setTimeout(r, 2000)); })()`);
  if (!r.cleanOpen || !(r.proposals || r.hint)) return `limpiar títulos no abrió o no dijo nada: ${JSON.stringify(r)}`;
  if (!r.groups.some((n) => n === 2)) return `los duplicados no aparecen como grupo: ${JSON.stringify(r.groups)}`;
  if (!r.statsShown || r.plays < 3 || !r.topSongs) return `estadísticas: ${JSON.stringify(r)}`;
  if (!r.counts.length || !/vez|veces/.test(r.counts[0]) || r.countHead !== 'Escuchado') return `«Más escuchadas» sin el contador: ${JSON.stringify(r)}`;
  if (!/^Radio de/.test(r.radioTitle) || !r.radioFirst) return `radio desde el tema: ${JSON.stringify(r)}`;
  if (!r.artistShuffle) return 'falta «Por artista» en el aleatorio';
  return '';
});

await check('buscar música: el catálogo de YouTube Music (C90)', async () => {
  // PLAN 0.15.1, from a saved catalogue (BAKIN_FAKE_YTMUSIC): search, tick, open an album.
  // «Bajar» isn't pressed: it would queue real downloads.
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    localStorage.removeItem('bakin.hub');
    document.getElementById('btn-download').click(); await wait(600);
    const out = { shown: !document.querySelector('#download-hub section[data-sec=catalog]').hidden };
    document.querySelector('#cat-kind button[value=songs]').click(); await wait(100);
    document.getElementById('cat-q').value = 'prueba';
    document.getElementById('cat-go').click(); await wait(1500);
    out.songs = [...document.querySelectorAll('#cat-songs .cat-song strong')].map((s) => s.textContent);
    const box = document.querySelector('#cat-songs input');
    box.checked = true; box.dispatchEvent(new Event('change')); await wait(100);
    out.button = document.getElementById('cat-download').textContent;
    document.querySelector('#cat-kind button[value=albums]').click(); await wait(1500);
    document.querySelector('#cat-tiles .cat-tile')?.click(); await wait(1500);
    out.album = document.getElementById('cat-page-title').textContent;
    out.tracks = document.querySelectorAll('#cat-songs .cat-song').length;
    out.all = !document.getElementById('cat-page-all').hidden;
    document.getElementById('download-hub').close(); await wait(300);
    return out;
  })()`);
  if (!r.shown) return 'el panel «Descargar» no abre en «Buscar música»';
  if (r.songs.join() !== 'Tema de prueba,Otro tema' || r.button !== 'Bajar (1)') return `búsqueda: ${JSON.stringify(r)}`;
  if (r.album !== 'Disco de prueba' || r.tracks !== 2 || !r.all) return `álbum: ${JSON.stringify(r)}`;
  return '';
});

await check('Spotify → YouTube Music (C91)', async () => {
  // PLAN 0.15.2, from a saved Spotify page (BAKIN_FAKE_SPOTIFY) and the saved catalogue: the
  // link field offers it, one song found and ticked, the other «no está». «Bajar» isn't pressed.
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const go = document.getElementById('go');
    const acts = go.source('https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy');
    const out = { action: acts[0]?.label };
    go.dispatchEvent(new CustomEvent('gm-select', { detail: acts[0], cancelable: true, bubbles: true }));
    for (let i = 0; i < 40 && !/encontrados/.test(document.getElementById('sp-status').textContent); i++) await wait(250);
    out.open = document.getElementById('spotify-dialog').open;
    out.rows = [...document.querySelectorAll('#sp-list .sp-row')].map((row) => ({ to: row.querySelector('.sp-to').textContent, on: row.querySelector('input').checked }));
    out.button = document.getElementById('sp-download').textContent;
    out.status = document.getElementById('sp-status').textContent;
    document.getElementById('sp-close').click(); await wait(300);
    return out;
  })()`);
  if (!/YouTube Music/.test(r.action || '')) return `el campo no ofrece buscar en YouTube Music: ${JSON.stringify(r)}`;
  if (!r.open || r.rows.length !== 2) return `diálogo de Spotify: ${JSON.stringify(r)}`;
  if (!r.rows[0].on || !/Tema de prueba/.test(r.rows[0].to) || r.rows[1].on || r.button !== 'Bajar (1)') return `coincidencias: ${JSON.stringify(r)}`;
  return '';
});

await check('descargas en segundo plano, con un anillo arriba (C92)', async () => {
  // PLAN 0.15.3: a job starting doesn't switch to Descargas; a ring next to «Descargar» shows it,
  // a click on it opens Descargas; the step-by-step activity is folded under «Detalles».
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    document.querySelector('#view > button[value=music]').click(); await wait(300);
    const emit = (ev) => window.__TAURI__.event.emit('engine', ev);
    await emit({ kind: 'start', mode: 'media', target_kind: 'media', title: 'Prueba', message: 'Descargando' }); await wait(300);
    const out = { view: document.getElementById('view').value, ring: !document.getElementById('tb-progress').hidden };
    document.getElementById('tb-progress').click(); await wait(300);
    out.opened = document.getElementById('view').value;
    out.folded = !document.getElementById('activity').open;
    await emit({ kind: 'finished', mode: 'media', outcome: 'cancelled', message: 'Cancelado.' }); await wait(400);
    out.after = document.getElementById('tb-progress').hidden;
    document.querySelector('#view > button[value=music]').click(); await wait(300);
    return out;
  })()`);
  if (r.view !== 'music' || !r.ring) return `empezar una descarga cambió de vista o no mostró el anillo: ${JSON.stringify(r)}`;
  if (r.opened !== 'downloads' || !r.folded) return `el anillo no abre Descargas o la actividad no está plegada: ${JSON.stringify(r)}`;
  if (!r.after) return 'el anillo queda cuando no baja nada';
  return '';
});

await check('sonido: volumen parejo, ecualizador, salida, letras, A–B (C94)', async () => {
  // PLAN 0.16. The long test song gets a .lrc next to it; it plays (silently: BAKIN_MUTE).
  const fs = await import('node:fs');
  const path = await import('node:path');
  const song = fs.readdirSync(longDir).find((f) => f.endsWith('.wav'));
  const lrc = path.join(longDir, song.replace(/\.wav$/, '.lrc'));
  fs.writeFileSync(lrc, '[00:00.00]primera línea\n[00:01.50]segunda línea\n[03:00.00]la última\n');
  const add = await engine('library_add_folder', { path: longDir });
  await js(`(async () => { document.getElementById('btn-lib-refresh').click(); await new Promise((r) => setTimeout(r, 2000)); })()`);
  const r = await js(`(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const call = (m, p) => window.__TAURI__.core.invoke('engine_call', { method: m, params: p || {} });
    const pc = (cmd) => window.__TAURI__.core.invoke('player_call', { cmd });
    const out = {};
    // Configuración › Música: each setting once
    document.getElementById('btn-settings').click(); await wait(400);
    document.querySelector('#settings-nav [data-sec=music]').click(); await wait(1200);
    out.once = ['ld-mode', 'eq-on', 'out-device', 'ly-online'].every((id) => document.querySelectorAll('#' + id).length === 1);
    const dev = document.getElementById('out-device');  // gm-select moves the id to its button
    out.devices = (dev.tagName === 'SELECT' ? dev : dev.parentElement.querySelector(':scope > select')).options.length;
    document.querySelector('#ld-mode button[value=track]').click(); await wait(500);
    out.mode = (await call('settings_set', { changes: {} })).settings.loudness_mode;
    document.querySelector('#eq-presets button[value=bass]').click(); await wait(500);
    const st = await pc({ cmd: 'state' });
    out.eq = st.eq && st.eq.on && st.eq.gains[0] === 6;
    document.querySelector('#eq-presets button[value=flat]').click();
    document.querySelector('#ld-mode button[value=off]').click(); await wait(300);
    document.getElementById('settings-dialog').close();
    // the long song, with its lyrics
    const items = (await call('library_items', { filters: { kinds: ['audio'], q: 'larga' }, offset: 0, limit: 5 })).items;
    await pc({ cmd: 'load', tracks: items.map((i) => ({ id: i.id, path: i.path, name: i.name, title: i.title || i.name })), start: 0, play: true });
    await wait(800);
    document.getElementById('pl-lyrics').click(); await wait(1500);
    out.lines = [...document.querySelectorAll('#ly-lines p')].map((p) => p.textContent);
    await wait(2200);
    out.now = document.querySelector('#ly-lines p.now')?.textContent;
    // A–B
    await pc({ cmd: 'set', ab: [1, 2] }); await wait(1600);
    const ab = await pc({ cmd: 'state' });
    out.ab = ab.ab && ab.time < 2.6;
    await pc({ cmd: 'set', ab: null });
    await pc({ cmd: 'pause' });
    document.getElementById('pl-lyrics').click();
    return out;
  })()`);
  fs.rmSync(lrc, { force: true });
  if (add?.id) await engine('library_remove_folder', { collection: add.id });
  if (!r.once || r.devices < 1) return `ajustes de sonido: ${JSON.stringify(r)}`;
  if (r.mode !== 'track' || !r.eq) return `volumen parejo o ecualizador: ${JSON.stringify(r)}`;
  if (r.lines.join('|') !== 'primera línea|segunda línea|la última' || r.now !== 'segunda línea') return `letra: ${JSON.stringify(r)}`;
  if (!r.ab) return `A–B no repite: ${JSON.stringify(r)}`;
  return '';
});

await check('tus datos: copia de seguridad y carpeta de datos (C95)', async () => {
  // PLAN 0.18: export a backup to the test's folder (no dialog: the engine call), preview it;
  // Configuración › General shows the data folder. Importing isn't run here (it would merge).
  const fs = await import('node:fs');
  const path = await import('node:path');
  const dir = path.join(path.dirname(seed), 'respaldo de prueba');
  fs.mkdirSync(dir, { recursive: true });
  const ex = await engine('backup_export', { dest: dir });
  if (!ex?.ok || !fs.existsSync(ex.path)) return `no se exportó: ${JSON.stringify(ex)}`;
  const names = fs.readFileSync(ex.path).toString('latin1');
  if (/vault\/|cookies|tools\/|logs\//.test(names)) return 'la copia trae la bóveda, cookies, herramientas o registros';
  const ins = await engine('backup_inspect', { path: ex.path });
  if (!ins?.ok || !ins.manifest?.bakin_version) return `vista previa: ${JSON.stringify(ins)}`;
  const r = await js(`(async () => {
    document.getElementById('btn-settings').click(); await new Promise((r) => setTimeout(r, 500));
    document.querySelector('#settings-nav [data-sec=general]').click(); await new Promise((r) => setTimeout(r, 800));
    const out = { dir: document.getElementById('data-dir').textContent, buttons: ['backup-export', 'backup-import', 'data-dir-move'].every((id) => document.getElementById(id)) };
    document.getElementById('settings-dialog').close();
    return out;
  })()`);
  if (!r.buttons || !r.dir || r.dir === '…') return `Configuración › General: ${JSON.stringify(r)}`;
  return '';
});

await check('seguidos con historial (C97)', async () => {
  // PLAN 0.20.2: a follow shows what its last check brought and why something didn't come
  const r = await js(`[...document.querySelectorAll('#follows .follow-item')].map((li) => li.textContent)`);
  const row = r.find((t) => t.includes('Lista seguida de prueba'));
  if (!row) return `no está el seguido de prueba: ${JSON.stringify(r)}`;
  if (!/2 nuevos/.test(row) || !/1 ya los tenías/.test(row) || !/falló: El video es privado/.test(row)) return `sin el historial: ${row}`;
  return '';
});

await check('carpeta espejo de una playlist (C98)', async () => {
  // PLAN 0.20.1: the playlist's songs are copied to a folder with a playlist.m3u8; off leaves them
  const fs = await import('node:fs');
  const path = await import('node:path');
  const dir = path.join(path.dirname(seed), 'Espejo de prueba');
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  const { tracks } = await engine('music_tracks', { filters: { music: { view: 'songs' } } });
  const pl = (await engine('music_playlist_save', { name: 'Espejo de prueba' })).playlist;
  try {
    await engine('music_playlist_add', { playlist_id: pl.id, ids: tracks.slice(0, 2).map((t) => t.id) });
    const set = await engine('music_mirror_set', { playlist_id: pl.id, folder: dir });
    if (!set?.ok) return `no se pudo elegir la carpeta: ${JSON.stringify(set)}`;
    let names = [];
    for (let i = 0; i < 30 && !names.includes('playlist.m3u8'); i++) { await sleep(500); names = fs.readdirSync(dir); }
    const songs = names.filter((n) => !n.startsWith('.') && n !== 'playlist.m3u8');
    if (!names.includes('playlist.m3u8') || songs.length !== 2) return `la carpeta no quedó igual a la playlist: ${JSON.stringify(names)}`;
    const ov = await engine('music_overview');
    if (ov.playlists.find((p) => p.id === pl.id)?.mirror !== dir) return 'la playlist no dice que tiene carpeta espejo';
    if (!(await js(`!!document.getElementById('pl-mirror')`))) return 'falta el botón «Carpeta espejo…»';
    await engine('music_mirror_off', { playlist_id: pl.id });
    if (fs.readdirSync(dir).filter((n) => !n.startsWith('.')).length !== 3) return 'dejar de mantenerla borró algo';
  } finally {
    await engine('music_playlist_delete', { playlist_id: pl.id });
  }
  return '';
});

await check('en tu casa: servidor Subsonic, página del celular (C100, C101, R31)', async () => {
  // PLAN 0.21: on → only this PC in a test copy (BAKIN_LAN_LOOPBACK); ping with the code, the
  // phone page, a wrong code refused; off → the port closes
  const port = 45000 + Math.floor(Math.random() * 1000);
  await engine('settings_set', { changes: { lan_port: port } });
  const inv = (cmd, args = {}) => js(`window.__TAURI__.core.invoke(${JSON.stringify(cmd)}, ${JSON.stringify(args)})`);
  const st = await inv('lan_set', { on: true });
  try {
    if (!st.running || st.error) return `no prendió: ${JSON.stringify({ ...st, code: st.code ? '…' : '', qr_svg: '' })}`;
    if (st.addresses.some((a) => !a.startsWith('127.0.0.1:'))) return `la copia de prueba escucha fuera de esta PC: ${st.addresses}`;
    if (!/^[A-Z2-9]{4}-[A-Z2-9]{4}$/.test(st.code)) return 'el código no tiene la forma esperada';
    const base = `http://127.0.0.1:${port}`;
    const ping = await (await fetch(`${base}/rest/ping?u=bakin&p=${encodeURIComponent(st.code)}&v=1.16.1&c=smoke&f=json`)).json();
    if (ping['subsonic-response']?.status !== 'ok') return `ping: ${JSON.stringify(ping)}`;
    const bad = await (await fetch(`${base}/rest/ping?u=bakin&p=nope&v=1.16.1&c=smoke&f=json`)).json();
    if (bad['subsonic-response']?.error?.code !== 40) return 'un código equivocado entró';
    const songs = await (await fetch(`${base}/rest/search3?u=bakin&p=${encodeURIComponent(st.code)}&v=1.16.1&c=smoke&f=json&query=`)).json();
    if (!(songs['subsonic-response']?.searchResult3?.song?.length >= 3)) return 'search3 no trae la biblioteca';
    const page = await fetch(`${base}/app/`);
    if (!page.ok || !(await page.text()).includes('Bakin')) return 'la página del celular no está';
    const ui = await js(`(async () => {
      document.getElementById('btn-settings').click(); await new Promise((r) => setTimeout(r, 500));
      document.querySelector('#settings-nav [data-sec=home]').click(); await new Promise((r) => setTimeout(r, 1500));
      const out = { on: document.getElementById('lan-on').checked, pair: !document.getElementById('lan-pair').hidden, code: document.getElementById('lan-code').textContent };
      document.getElementById('settings-dialog').close();
      return out;
    })()`);
    if (!ui.on || !ui.pair || ui.code !== st.code) return `Configuración › En tu casa: ${JSON.stringify({ ...ui, code: ui.code === st.code })}`;
  } finally {
    await inv('lan_set', { on: false });
  }
  try { await fetch(`http://127.0.0.1:${port}/rest/ping`); return 'apagado, el puerto sigue abierto'; } catch { /* closed */ }
  return '';
});

await check('primer arranque guiado solo la primera vez (C102)', async () => {
  // PLAN 0.22.2: this home was used (ui-settings.json is there): no welcome; the dialog exists
  // (pytest covers the other side: an empty data folder shows it)
  const r = await js(`({ exists: !!document.getElementById('welcome-dialog'), open: !!document.querySelector('#welcome-dialog[open], #welcome-dialog dialog[open]') })`);
  const st = await engine('get_state');
  if (st.first_run) return 'el motor dice «primera vez» en un Bakin usado';
  if (!r.exists) return 'falta el diálogo de bienvenida';
  if (r.open) return 'la bienvenida apareció en un Bakin usado';
  return '';
});

await check('accesibilidad: axe sin fallas graves (C103)', async () => {
  // PLAN 0.22.4: axe-core (desktop/node_modules) on the main views and every Configuración section
  const path = await import('node:path');
  const axePath = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..', 'desktop', 'node_modules', 'axe-core', 'axe.min.js');
  if (!fs.existsSync(axePath)) return `falta axe-core (npm install en desktop/): ${axePath}`;
  await js(fs.readFileSync(axePath, 'utf8') + ';0');
  const view = (v) => `(() => { const s = document.getElementById('view'); s.value = '${v}'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: '${v}' } })); })()`;
  const secs = await js(`[...document.querySelectorAll('#settings-nav [data-sec]')].map((b) => b.dataset.sec)`);
  const steps = [['Fotos', view('photos')], ['Música', view('music')], ['Configuración', `document.getElementById('btn-settings').click()`],
    ...secs.map((sec) => [`Configuración › ${sec}`, `document.querySelector('#settings-nav [data-sec=${sec}]').click()`]),
    ['Descargar', `document.getElementById('settings-dialog').close(); document.getElementById('download-hub').show()`]];
  const bad = [];
  for (const [name, action] of steps) {
    await js(action);
    await sleep(900);
    const v = await js(`axe.run(document, { resultTypes: ['violations'] }).then((r) => r.violations.filter((x) => ['serious', 'critical'].includes(x.impact)).map((x) => x.id + ' ' + x.nodes[0].target.join(' ')))`);
    if (v.length) bad.push(`${name}: ${v.slice(0, 3).join(', ')}`);
  }
  await js(`document.getElementById('download-hub').close?.()`);
  return bad.join(' · ');
});

await check('cabecera de la lista de música prolija (C73)', async () => {
  // 0.13 review: a loose «Tamaño» select and «Baja calidad» chip, a heavy dark head, «Duración» on «Formato»
  const r = await js(`(async () => {
    const s = document.getElementById('view'); s.value = 'music'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'music' } }));
    await new Promise((r) => setTimeout(r, 1500));
    const head = document.querySelector('#lib-grid > .lib-row-head');
    if (!head) return { error: 'sin cabecera' };
    const bg = getComputedStyle(head).backgroundColor.replace('rgba(', '').replace('rgb(', '').replace(')', '').split(',').map(Number);
    const rect = (sel) => head.querySelector(sel).getBoundingClientRect();
    const d = rect('.c-duration'); const f = rect('.c-bitrate');
    const lowq = document.getElementById('lib-lowq');
    return { filters: getComputedStyle(document.getElementById('lib-filters')).display, lowqWithActions: !!lowq.closest('#music-actions'),
      alpha: bg.length > 3 ? bg[3] : 1, overlap: d.right > f.left + 1,
      textFits: [...head.querySelectorAll('button')].every((b) => b.scrollWidth <= b.clientWidth + 1) };
  })()`);
  if (r.error) return r.error;
  if (r.filters !== 'none') return 'en Música sigue la fila de filtros (orden y «Baja calidad» sueltos)';
  if (!r.lowqWithActions) return '«Baja calidad» no está con las acciones de la lista';
  if (r.alpha > 0.6) return `la cabecera es una caja opaca (alfa ${r.alpha})`;
  if (r.overlap || !r.textFits) return `los nombres de columna se pisan o no entran: ${JSON.stringify(r)}`;
  return '';
});

await check('presupuesto: ventana abierta con 300 fotos', async () => {
  await sleep(10000);
  const mb = memoryMB();
  console.log(`     ventana abierta: ${mb} MB`);
  return budget('window_open_mb', mb, 'MB');
});

await check('reproductor', async () => {
  await js(`(() => { const s = document.getElementById('view'); s.value = 'music'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'music' } })); })()`);
  let rows = 0;
  for (let i = 0; i < 20 && rows < 3; i++) { await sleep(500); rows = await js(`document.querySelectorAll('#lib-grid .lib-row:not(.lib-row-head)').length`); }
  if (rows < 3) return `la lista de música tiene ${rows} filas`;
  await sleep(800);  // the list may still be redrawn after the switch: click a row that stays
  await js(`document.querySelectorAll('#lib-grid .lib-row:not(.lib-row-head)')[0].click()`);  // first of 3: next, then the last one by itself
  await sleep(2500);
  const st = await js(`({ shown: !document.getElementById('player').hidden, playing: document.getElementById('player').classList.contains('playing'), title: document.getElementById('pl-title').textContent, row: document.querySelector('#lib-grid .lib-row.playing strong')?.textContent || '' })`);
  if (!st.shown || !st.playing) return `no arrancó: ${JSON.stringify(st)}`;
  if (st.row !== st.title) return `la fila que suena no está marcada: ${JSON.stringify(st)}`;
  await js(`document.getElementById('pl-next').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }))`);  // right click: next song
  await sleep(1500);
  const next = await js(`document.getElementById('pl-title').textContent`);
  if (next === st.title) return 'el botón siguiente no cambió de tema';
  for (let i = 0; i < 12; i++) {  // a 3-second song ends and the next one starts by itself
    await sleep(500);
    const t = await js(`document.getElementById('pl-title').textContent`);
    if (t !== next) return '';
  }
  return 'al terminar el tema no pasó al siguiente';
});

await check('vistas de música', async () => {
  if (await js(`getComputedStyle(document.getElementById('music-card')).display === 'none'`)) return 'no aparece la tarjeta Música';
  if (await js(`getComputedStyle(document.getElementById('scopes').closest('gm-surface')).display !== 'none'`)) return 'en Música se ve la tarjeta de Fotos';
  await js(`document.querySelector('#music-views [data-mview=artists]').click()`);
  let cards = 0;
  for (let i = 0; i < 20 && !cards; i++) { await sleep(300); cards = await js(`document.querySelectorAll('#lib-groups .group-card').length`); }
  if (!cards) return 'Artistas no mostró ninguna tarjeta';
  await js(`document.querySelector('#lib-groups .group-card').click()`);
  await sleep(1500);
  const rows = await js(`document.querySelectorAll('#lib-grid .lib-row:not(.lib-row-head)').length`);
  if (rows < 3) return `el artista tiene ${rows} temas (se esperaban los 3 de prueba)`;
  if (await js(`document.getElementById('lib-back').hidden`)) return 'falta «← Artistas»';
  await js(`document.getElementById('mus-play').click()`);
  await sleep(1500);
  if (!(await js(`document.getElementById('player').classList.contains('playing')`))) return '«Reproducir» no arrancó la lista';
  return '';
});

await check('cambiar de lista mientras otra carga (C38)', async () => {
  // 0.9.2: Música then Fotos before the first list arrived left Fotos empty, stuck with Música's «Cargar más»
  for (const [first, second] of [['music', 'photos'], ['photos', 'music'], ['music', 'photos']]) {
    const r = await js(`(async () => { const c = (v) => document.querySelector('.view-switch button[value=' + v + ']').click(); c('${first}'); c('${second}'); await new Promise((r) => setTimeout(r, 2500)); const more = document.getElementById('btn-lib-more'); return { items: document.querySelectorAll('#lib-grid > .lib-tile, #lib-grid .lib-row:not(.lib-row-head)').length, more: more.hidden ? '' : more.textContent }; })()`);
    if (!r.items) return `${first} → ${second}: la lista quedó vacía ${JSON.stringify(r)}`;
  }
  return '';
});

await check('aleatorio con clic derecho (C35)', async () => {
  // right click: the menu; a pick turns shuffle on with that kind, the button shows it, it is saved
  const r = await js(`(async () => { const wait = (ms) => new Promise((r) => setTimeout(r, ms)); const call = (cmd) => window.__TAURI__.core.invoke('player_call', { cmd }); await call({ cmd: 'set', shuffle: false, shuffle_mode: 'random' }); await wait(300); document.getElementById('pl-shuffle').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true })); await wait(400); const open = document.getElementById('shuffle-menu').matches(':popover-open'); document.querySelector('#shuffle-menu button[value=album]').click(); await wait(600); const st = await call({ cmd: 'state' }); const btn = document.getElementById('pl-shuffle'); const r = { open, shuffle: st.shuffle, mode: st.shuffle_mode, pressed: btn.getAttribute('aria-pressed'), shown: btn.dataset.mode, mark: getComputedStyle(btn.querySelector('.pl-mode'), '::before').content }; await call({ cmd: 'set', shuffle: false, shuffle_mode: 'random' }); return r; })()`);
  if (!r.open || !r.shuffle || r.mode !== 'album' || r.pressed !== 'true' || r.shown !== 'album' || r.mark === 'none') return JSON.stringify(r);
  return '';
});

// One expression in another window's page (the mini window), over its own connection.
async function jsIn(urlPart, expression) {
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  const t = targets.find((x) => x.type === 'page' && x.url.includes(urlPart));
  if (!t) return undefined;
  const w = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((r) => { w.onopen = r; });
  const r = await new Promise((done) => {
    w.onmessage = (m) => { const msg = JSON.parse(m.data); if (msg.id === 1) done(msg); };
    w.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression, returnByValue: true, awaitPromise: true } }));
  });
  w.close();
  return r.result?.result?.value;
}

await check('modo mínimo (C36, C41)', async () => {
  // the small window opens with the song in the player, the big one hides; three sizes,
  // a volume slider, «transparente» shows the song only and back, the size is remembered;
  // ⤢ brings the big window back (paused: the test songs last 3 s)
  await js(`window.__TAURI__.core.invoke('player_call', { cmd: { cmd: 'pause' } })`);
  await sleep(300);
  const title = await js(`document.getElementById('pl-title').textContent`);
  const look = `({ title: document.getElementById('m-title').textContent, layout: document.body.dataset.layout, through: document.body.classList.contains('through'), w: innerWidth, h: innerHeight })`;
  const openMini = async () => {
    await js(`window.__TAURI__.core.invoke('mini_open')`);
    let m;
    for (let i = 0; i < 20 && !m?.title; i++) { await sleep(400); m = await jsIn('mini.html', look).catch(() => undefined); }
    return m;
  };
  const closeMini = async () => {
    jsIn('mini.html', `window.__TAURI__.core.invoke('mini_expand')`).catch(() => {});  // never answers: its window goes
    for (let i = 0; i < 20; i++) {  // gone within 5 s
      await sleep(250);
      const t = await (await fetch(`http://127.0.0.1:${port}/json`)).json().catch(() => []);
      if (!t.some((x) => x.url.includes('mini.html'))) break;
    }
    await sleep(300);
  };
  const mini = await openMini();
  const hidden = !(await js(`window.__TAURI__.window.getCurrentWindow().isVisible()`));
  const steps = {};
  if (mini) {
    await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_layout', { layout: 'card' })`);
    await sleep(600);
    steps.card = await jsIn('mini.html', look);
    await jsIn('mini.html', `(() => { const v = document.getElementById('m-volume'); v.value = '30'; v.dispatchEvent(new Event('input')); return 1; })()`);
    await sleep(500);
    steps.volume = await js(`window.__TAURI__.core.invoke('player_call', { cmd: { cmd: 'state' } }).then((s) => s.volume)`);
    await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_through', { on: true })`);
    await sleep(600);
    steps.through = await jsIn('mini.html', look);
    await js(`window.__TAURI__.core.invoke('mini_through', { on: false })`);
    await sleep(600);
    steps.back = await jsIn('mini.html', look);
  }
  await closeMini();
  const again = mini ? await openMini() : null;  // remembered: the card
  if (again) {
    await jsIn('mini.html', `window.__TAURI__.core.invoke('mini_layout', { layout: 'bar' })`);
    await sleep(400);
    await closeMini();
  }
  await js(`window.__TAURI__.core.invoke('player_call', { cmd: { cmd: 'set', volume: 0.8 } })`);
  if (!mini) return 'no se abrió la ventana chica';
  if (mini.title !== title) return `la ventana chica muestra «${mini.title}» y el reproductor «${title}»`;
  if (mini.layout !== 'bar' || mini.w > 540 || mini.h > 100) return `al abrir: ${JSON.stringify(mini)}`;
  if (!hidden) return 'la ventana grande siguió a la vista';
  if (steps.card?.layout !== 'card' || steps.card.w > 240 || Math.abs(steps.card.w - steps.card.h) > 2) return `tarjeta: ${JSON.stringify(steps.card)}`;
  if (Math.abs((steps.volume ?? 0) - 0.3) > 0.02) return `el volumen de la barrita no llegó al reproductor: ${steps.volume}`;
  if (!steps.through?.through || steps.through.layout !== 'info') return `transparente: ${JSON.stringify(steps.through)}`;
  if (steps.back?.through || steps.back?.layout !== 'card') return `al volver de transparente: ${JSON.stringify(steps.back)}`;
  if (again?.layout !== 'card') return `no recordó el tamaño: ${JSON.stringify(again)}`;
  const left = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  if (left.some((t) => t.url.includes('mini.html'))) return 'la ventana chica no se cerró con ⤢';
  if (!(await js(`window.__TAURI__.window.getCurrentWindow().isVisible()`))) return 'con ⤢ no volvió la ventana grande';
  return '';
});

await check('fondo procedural (C37)', async () => {
  // drawn once, the window's size, not flat black; nothing redrawn while music plays; a resize redraws once
  const r = await js(`(async () => { const wait = (ms) => new Promise((r) => setTimeout(r, ms)); const c = document.querySelectorAll('canvas.backdrop'); if (c.length !== 1) return { count: c.length }; const el = c[0]; const g = el.getContext('2d'); const px = [[0.1, 0.1], [0.5, 0.5], [0.9, 0.3], [0.3, 0.9]].map(([x, y]) => [...g.getImageData(Math.floor(x * el.width), Math.floor(y * el.height), 1, 1).data].slice(0, 3).join(',')); await wait(2500); const same = document.querySelector('canvas.backdrop') === el; return { count: 1, w: el.width, h: el.height, iw: innerWidth, ih: innerHeight, distinct: new Set(px).size, flat: px.every((p) => p === '13,12,20'), same, bodyBg: getComputedStyle(document.body).backgroundColor }; })()`);
  if (r.count !== 1) return `${r.count} lienzos de fondo`;
  if (r.w !== r.iw || r.h !== r.ih) return `el fondo mide ${r.w}×${r.h} y la ventana ${r.iw}×${r.ih}`;
  if (r.flat || r.distinct < 2) return 'el fondo es negro liso';
  if (!r.same) return 'el fondo se volvió a dibujar sin motivo (debe dibujarse una vez)';
  if (r.bodyBg !== 'rgba(0, 0, 0, 0)') return `el body tapa el fondo (${r.bodyBg})`;
  await send('Emulation.setDeviceMetricsOverride', { width: 900, height: 700, deviceScaleFactor: 1, mobile: false });
  await sleep(1600);
  const after = await js(`(() => { const c = document.querySelectorAll('canvas.backdrop'); return { count: c.length, w: c[0]?.width, h: c[0]?.height }; })()`);
  await send('Emulation.clearDeviceMetricsOverride');
  await sleep(1600);
  if (after.count !== 1 || after.w !== 900 || after.h !== 700) return `al cambiar el tamaño: ${JSON.stringify(after)}`;
  return '';
});

await check('esqueleto: barra lateral, panel «Descargar», ventana vertical (C31–C34)', async () => {
  // Música starts at «Todo», with no «MÚSICA» heading repeating the tab (C31)
  const first = await js(`[document.querySelector('#music-views [data-mview=songs] span').textContent, [...document.querySelectorAll('#music-card h3')].some((h) => /^música$/i.test(h.textContent.trim()))]`);
  if (first[0] !== 'Todo' || first[1]) return `Música: la primera entrada es «${first[0]}»${first[1] ? ' y sigue el título MÚSICA' : ''}`;
  // the list's count shows only when the sidebar isn't showing it (C31)
  await js(`document.querySelector('#music-views [data-mview=songs]').click()`);
  await sleep(1200);
  if (await js(`document.getElementById('lib-count').textContent`)) return 'el número de temas aparece dos veces (barra lateral y título)';
  // Ctrl B: a column of icons, and then the count comes back to the title (C32)
  const collapsed = await js(`(async () => { document.getElementById('btn-side').click(); await new Promise((r) => setTimeout(r, 400)); const r = { on: document.body.classList.contains('side-collapsed'), w: document.getElementById('side-library').getBoundingClientRect().width, count: document.getElementById('lib-count').textContent }; document.getElementById('btn-side').click(); await new Promise((r) => setTimeout(r, 300)); r.back = !document.body.classList.contains('side-collapsed'); return r; })()`);
  if (!collapsed.on || collapsed.w > 80 || !collapsed.count || !collapsed.back) return `barra lateral colapsada: ${JSON.stringify(collapsed)}`;
  // «Descargar»: one panel with every way to download; «Música o video» puts «Elegir formato» first (C33)
  // (opened on the link section: the Wallhaven one would search the real site)
  const dl = await js(`(async () => { const wait = (ms) => new Promise((r) => setTimeout(r, ms)); localStorage.setItem('bakin.hub', 'link'); document.getElementById('btn-download').click(); await wait(400); const hub = document.getElementById('download-hub'); const items = document.querySelectorAll('#hub-nav .scope').length; const open = hub.open; document.querySelector('#hub-nav [data-sec=link]').click(); await wait(400); const linkShown = !hub.querySelector('section[data-sec=link]').hidden; const first = document.getElementById('go').source('https://www.youtube.com/watch?v=x')[0]?.label; hub.close(); await wait(300); return { items, open, linkShown, first, closed: !hub.open }; })()`);
  // three ways in: Buscar música (0.15), Fondos, and Música y video (one field for any link, «Me gusta» a card)
  if (dl.items !== 3 || !dl.open || !dl.linkShown || !dl.closed || !/formato/i.test(dl.first || '')) return `Descargar: ${JSON.stringify(dl)}`;
  // its button: the same frosted pill as the search, next to it (C64, C74)
  const btn = await js(`(() => { const g = document.getElementById('btn-download').closest('gm-surface'); return g ? g.getAttribute('material') + ':' + (g.parentElement === document.getElementById('lib-q').closest('gm-surface').parentElement) : ''; })()`);
  if (btn !== 'frost:true') return `«Descargar» no es una píldora frosted junto a la búsqueda: «${btn}»`;
  // the vertical monitor (1080×1920), the same at 125 % and a narrow window: nothing spills out (C34)
  const fits = [];
  for (const [w, h] of [[1080, 1920], [864, 1536], [560, 1000]]) {
    await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: false });
    await sleep(700);
    const r = await js(`(async () => { const box = (id) => document.getElementById(id).getBoundingClientRect(); const out = [...document.querySelectorAll('.toolbar > *, .player, #lib-grid')].filter((e) => e.offsetParent && e.getBoundingClientRect().right > innerWidth + 1).map((e) => e.id || e.className); const r = { scroll: document.documentElement.scrollWidth - innerWidth, search: Math.round(box('lib-q').width), out }; if (innerWidth <= 900) { document.getElementById('btn-side').click(); await new Promise((r) => setTimeout(r, 400)); const s = box('side-library'); r.drawer = s.left >= -1 && s.width > 200; document.getElementById('lib-grid').dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); await new Promise((r) => setTimeout(r, 300)); r.closed = !document.body.classList.contains('side-open'); } return r; })()`);
    if (r.scroll > 0 || r.out.length || r.search < 80 || r.drawer === false || r.closed === false) fits.push(`${w}×${h}: ${JSON.stringify(r)}`);
  }
  await send('Emulation.clearDeviceMetricsOverride');
  await sleep(500);
  return fits.join(' | ');
});

await check('bandeja con música, el motor se duerme (C9, C10, R1, C43)', async () => {
  // a long song on repeat (like the user's), the window closed to the tray
  // for a while, then reopened: 0.6.1 paused it after ~100 s or reloaded
  await engine('library_add_folder', { path: longDir });
  await js(`(async () => { document.getElementById('btn-lib-refresh').click(); })()`);
  await sleep(2500);
  const t = (await engine('music_tracks', { filters: { q: 'larga' } })).tracks;
  if (!t.length) return 'no aparece la canción larga';
  await js(`(async () => { document.getElementById('lib-q').value = 'larga'; document.getElementById('lib-q').dispatchEvent(new Event('input')); })()`);
  await sleep(1500);
  await js(`document.querySelector('#lib-grid .lib-row:not(.lib-row-head), #lib-grid .lib-tile').click()`);
  await sleep(2000);
  if (!(await js(`document.getElementById('pl-title').textContent.toLowerCase().includes('larga')`))) return 'no arrancó la canción larga';
  // the player answers asynchronously: set repeat through it, not by clicking until the button changes
  await js(`window.__TAURI__.core.invoke('player_call', { cmd: { cmd: 'set', repeat: 'one' } })`);
  await sleep(1000);
  const track = (await js(`window.__TAURI__.core.invoke('player_call', { cmd: { cmd: 'state' } })`)).track.id;
  await js(`document.getElementById('win-close').click()`);
  // in the tray the window is destroyed (after 20 s) and WebView2 with it;
  // the player in the shell goes on: its position file keeps moving
  const position = () => { try { return JSON.parse(fs.readFileSync(`${home}/music/player-position.json`, 'utf8')); } catch { return null; } };
  const webviews = () => Number(execFileSync('powershell.exe', ['-NoProfile', '-Command',
    `@(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" | Where-Object { $_.CommandLine -like '*${wv2.replace(/'/g, "''")}*' }).Count`]).toString().trim());
  // engine on demand: with the window gone and nothing queued it is let go (C43)
  const engines = () => Number(execFileSync('powershell.exe', ['-NoProfile', '-Command', `
    $exe = '${exe.replace(/'/g, "''")}'
    $all = Get-CimInstance Win32_Process
    $me = @($all | Where-Object { $_.ExecutablePath -eq $exe } | ForEach-Object { [int]$_.ProcessId })
    @($all | Where-Object { $me -contains [int]$_.ParentProcessId -and ($_.Name -like 'bakin-engine*' -or $_.Name -like 'python*') }).Count`]).toString().trim());
  let asleep = false;
  await sleep(30000);
  if (webviews() > 0) return `a los 30 s en la bandeja sigue abierto WebView2 (${webviews()} procesos)`;
  let last = position()?.time ?? -1;
  for (let waited = 30; waited < Number(traySeconds); waited += 20) {
    await sleep(20000);
    const p = position();
    if (!p || p.id !== track) return `a los ${waited + 20} s en la bandeja el reproductor cambió de tema o no guarda su posición`;
    if (p.time <= last) return `a los ${waited + 20} s en la bandeja la música no avanza (${last} -> ${p.time})`;
    last = p.time;
    if (engines() === 0) asleep = true;
  }
  if (!asleep) return `a los ${traySeconds} s en la bandeja, sin nada en la cola, el motor sigue abierto`;
  // budgets (docs/QA.md), with the engine asleep: what the user has all day
  const mb = memoryMB();
  const cpu = cpuPct();
  console.log(`     bandeja con música: ${mb} MB, CPU ${cpu} %`);
  const over = budget('tray_music_mb', mb, 'MB') || budget('tray_music_cpu_pct', cpu, '%');
  if (over) return over;
  spawn(exe, [], { detached: true, stdio: 'ignore' }).unref();  // opening Bakin again makes a new window
  await sleep(3000);
  await attach();
  let st = null;
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    st = await js(`({ playing: document.getElementById('player').classList.contains('playing'), shown: !document.getElementById('player').hidden, title: document.getElementById('pl-title').textContent })`).catch(() => null);
    if (st?.playing) break;
  }
  if (!st?.shown || !st.playing) {
    const real = await js(`window.__TAURI__.core.invoke('player_call', { cmd: { cmd: 'state' } })`).catch((e) => String(e));
    return `al volver de la bandeja el reproductor no se ve sonando: ${JSON.stringify(st)}; reproductor: ${JSON.stringify({ playing: real?.playing, time: real?.time, track: real?.track?.id })}; posición: ${JSON.stringify(position())}`;
  }
  if (!st.title.toLowerCase().includes('larga')) return `al volver suena otro tema: ${st.title}`;
  if (!(await engine('get_state'))?.settings) return 'al volver de la bandeja el motor no despertó';
  await js(`window.__TAURI__.core.invoke('player_call', { cmd: { cmd: 'set', repeat: 'off' } })`);
  return '';
});

await sleep(500);
await check('en inglés y de vuelta (C104)', async () => {
  // PLAN 0.22.3: Configuración › General › Idioma = English: the page, the settings and the
  // engine's fixed messages in English after a reload; back to Español → Spanish again
  const ready = async (lang) => {
    for (let i = 0; i < 80; i++) {
      const r = await js(`({ lang: document.documentElement.lang, ready: document.body?.classList.contains('ready') || false })`).catch(() => null);
      if (r?.ready && r.lang === lang) return true;
      await sleep(100);
    }
    return false;
  };
  const setLang = async (lang) => {
    await engine('settings_set', { changes: { lang } });
    await js(`(() => { try { localStorage.setItem('bakin.lang', '${lang}'); } catch {} })()`);
    await send('Page.reload', { ignoreCache: false });
  };
  try {
    await setLang('en');
    if (!(await ready('en'))) return 'no quedó en inglés después de recargar';
    const r = await js(`({ home: document.querySelector('#settings-nav [data-sec=home] span')?.textContent,
      auto: document.querySelector('#ui-lang-box option[value=auto]')?.textContent, sel: document.getElementById('ui-lang-box')?.value,
      dl: !!document.querySelector('[title="Download (Ctrl D)"]') })`);
    if (r.home !== 'At home' || r.auto !== 'Automatic' || r.sel !== 'en' || !r.dl) return `textos en inglés: ${JSON.stringify(r)}`;
  } finally {
    await setLang('es');
  }
  if (!(await ready('es'))) return 'no volvió al español';
  const back = await js(`document.querySelector('#settings-nav [data-sec=home] span')?.textContent`);
  if (back !== 'En tu casa') return `de vuelta: ${back}`;
  await sleep(1500);
  return '';
});

await check('arranque sin destello (C39)', async () => {
  // 0.9.3 report: on a cold start the raw dialogs, menus and buttons flashed for a few frames.
  // Until the page is wired, nothing of it may be visible; then it must show within a few seconds.
  await send('Page.reload', { ignoreCache: true });
  const seen = [];
  for (let i = 0; i < 60; i++) {
    // before app.css arrives the browser paints nothing (the stylesheet blocks rendering), so only count from then on
    const r = await js(`({ ready: document.body?.classList.contains('ready') || false, css: [...document.styleSheets].some((s) => (s.href || '').endsWith('/app.css')), vis: document.body ? getComputedStyle(document.body).visibility : 'none', dialogs: document.querySelectorAll('gm-dialog:not(:defined)').length })`).catch(() => null);
    if (r) seen.push(r);
    if (r?.ready) break;
    await sleep(50);
  }
  const leak = seen.find((r) => r.css && !r.ready && r.vis === 'visible');
  if (leak) return `la página se vio antes de estar armada: ${JSON.stringify(leak)} (de ${seen.length} muestras)`;
  if (!seen.at(-1)?.ready) return `la página no se mostró en 3 s: ${JSON.stringify(seen.at(-1))}`;
  await sleep(1500);
  return '';
});

await check('presupuesto: bandeja sin música (C43)', async () => {
  // what keeps Bakin in the tray without music: a queue paused until later
  await js(`window.__TAURI__.core.invoke('player_call', { cmd: { cmd: 'pause' } })`);
  await engine('pause_queue', { reason: 'Prueba', until: Date.now() / 1000 + 3600 });
  await sleep(1500);
  await js(`document.getElementById('win-close').click()`);
  // the window goes after 20 s, the engine after a quiet minute (checked every 30 s)
  let mb = 0;
  for (let waited = 10; waited <= 150; waited += 10) {
    await sleep(10000);
    mb = memoryMB();
    if (waited >= 90 && mb <= budgets.tray_idle_mb.max) break;
  }
  console.log(`     bandeja sin música: ${mb} MB`);
  if (mb > budgets.tray_idle_mb.max) {  // say why, if it happens again (seen once, 178 MB: the window's WebView2 still alive?)
    const wv = ps(`@(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" | Where-Object { $_.CommandLine -like '*${wv2.replace(/'/g, "''")}*' }).Count`);
    const engines = ps(`${tree}
  @($all | Where-Object { $ids -contains [int]$_.ProcessId -and ($_.Name -like 'bakin-engine*' -or $_.Name -like 'python*') }).Count`);
    console.log(`     (diagnóstico: ${wv} procesos de WebView2 de la copia, ${engines} del motor)`);
  }
  spawn(exe, [], { detached: true, stdio: 'ignore' }).unref();
  await sleep(3000);
  await attach();
  await engine('resume_queue');
  return budget('tray_idle_mb', mb, 'MB');
});

// time from loading the page to the library on screen
async function startupMs() {
  await js(`window.__before_reload = 1`);
  await send('Page.reload', { ignoreCache: false });
  for (let i = 0; i < 500; i++) {
    await sleep(20);
    // only the new page counts (the old one may still answer right after the reload)
    const ms = await js(`!window.__before_reload && document.querySelector('#lib-grid > .lib-tile, #lib-grid .lib-row:not(.lib-row-head)') ? Math.round(performance.now()) : 0`).catch(() => 0);
    if (ms) return ms;
  }
  return 10000;
}

await check('presupuesto: arranque con 300 fotos', async () => {
  await js(`(() => { const s = document.getElementById('view'); s.value = 'photos'; s.dispatchEvent(new CustomEvent('gm-change', { detail: { value: 'photos' } })); })()`);
  await sleep(1500);
  await startupMs();  // the first load after a change of view warms caches: measure the second
  const ms = await startupMs();
  console.log(`     arranque (300 fotos): ${ms} ms`);
  return budget('startup_ms', ms, 'ms');
});

await check('presupuesto: arranque con 3500 fotos', async () => {
  if (!manyDir) return 'falta la carpeta de 3500 imágenes';
  const add = await engine('library_add_folder', { path: manyDir });
  if (add && add.ok === false) return `no se pudo agregar la carpeta: ${add.error}`;
  await js(`document.getElementById('btn-lib-refresh').click()`);
  let n = 0;
  for (let i = 0; i < 60 && n < 3800; i++) {
    await sleep(500);
    n = Number(((await js(`document.getElementById('count-all').textContent`)) || '').match(/\d+/)?.[0] || 0);
  }
  if (n < 3800) return `la biblioteca no llegó a 3800 archivos (${n})`;
  await startupMs();
  const ms = await startupMs();
  console.log(`     arranque (3800 fotos): ${ms} ms`);
  return budget('startup_3500_ms', ms, 'ms');
});

await check('sin errores de JavaScript', async () => (problems.length ? problems.slice(0, 5).join(' | ') : ''));

ws.close();
console.log('\nPresupuestos (packaging/budgets.json):');
for (const m of measured) console.log(`  ${m.state.padEnd(5)} ${m.key.padEnd(20)} ${String(m.value).padStart(7)} ${m.unit.padEnd(2)} (máximo ${m.max})`);
console.log(failures.length ? `\nUI smoke: ${failures.length} falla(s)` : '\nUI smoke: todo bien');
process.exit(failures.length ? 1 : 0);
