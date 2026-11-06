// The interface's language (PLAN 0.22.3). Spanish is the source: the text in
// the code is the key, and i18n/en.js maps it to English (gettext style). A
// text without a translation shows in Spanish, so nothing ever goes blank.
// The choice lives in the engine's settings (`lang`: auto | es | en); a copy
// in localStorage lets this module translate before anything is drawn.
import EN from './i18n/en.js';

const KEY = 'bakin.lang';
const stored = () => { try { return localStorage.getItem(KEY) || 'es'; } catch { return 'es'; } };  // Spanish unless chosen

// "auto": the system's language (WebView2 follows Windows); Spanish or English.
export const resolveLang = (pref) => (pref === 'es' || pref === 'en' ? pref
  : (navigator.language || 'es').toLowerCase().startsWith('es') ? 'es' : 'en');

export const lang = resolveLang(stored());
export const LOCALE = lang;  // dates and numbers
const table = lang === 'en' ? EN : null;
document.documentElement.lang = lang;

const fill = (text, vars) => (vars ? text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : text);

// tr('Bajando {n} temas', { n }) → the English text when there is one.
export function tr(text, vars) {
  if (typeof text !== 'string') return text;  // an engine field that may be missing
  return fill((table && Object.hasOwn(table, text) && table[text]) || text, vars);
}

// Singular or plural by n ({n} is filled in too).
export function tn(one, many, n, vars) {
  return tr(n === 1 ? one : many, { n, ...vars });
}

// The engine's choice, at start and when it changes: the page reloads when the
// language it was drawn in is not the one chosen.
export function syncLang(pref) {
  const want = pref || 'es';
  if (want !== stored()) { try { localStorage.setItem(KEY, want); } catch { /* storage unavailable */ } }
  if (resolveLang(want) !== lang) { location.reload(); return true; }
  return false;
}

// The static page: text nodes and these attributes whose whole (trimmed,
// spaces collapsed) text has a translation. Runs before the components draw.
const ATTRS = ['title', 'aria-label', 'placeholder', 'heading', 'label'];
const norm = (s) => s.replace(/\s+/g, ' ').trim();

export function translateDom(root = document.body) {
  if (!table) return;
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement?.closest('script, style') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    const key = norm(n.nodeValue);
    const en = key && Object.hasOwn(table, key) && table[key];
    if (en) {
      const [, lead, , trail] = n.nodeValue.match(/^(\s*)([\s\S]*?)(\s*)$/);
      n.nodeValue = lead + en + trail;
    }
  }
  const sel = ATTRS.map((a) => `[${a}]`).join(',');
  for (const el of [root, ...root.querySelectorAll(sel)]) {
    for (const a of ATTRS) {
      const v = el.getAttribute?.(a);
      const en = v && Object.hasOwn(table, norm(v)) && table[norm(v)];
      if (en) el.setAttribute(a, en);
    }
  }
}

translateDom();
