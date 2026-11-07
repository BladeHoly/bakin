// Descargar con Bakin: hands a URL to the Bakin app through its bakin://
// link. The extension reads nothing from pages: it only passes the address
// you choose (the page, a link, a video or audio source), and Bakin always
// asks you to confirm before downloading anything.
//
// And, only when you pick «Darle a Bakin mi sesión de YouTube» on its icon,
// your youtube.com and google.com cookies go to Bakin on this PC through the
// browser's native messaging (no network, no file): Brave, Chrome and Edge
// keep them encrypted for themselves, so Bakin can't read them otherwise.
// Bakin keeps them encrypted for your Windows user, for its downloads only.

const MENU = [
  { id: 'bakin-page', title: 'Descargar esta página con Bakin', contexts: ['page'] },
  { id: 'bakin-link', title: 'Descargar el enlace con Bakin', contexts: ['link'] },
  { id: 'bakin-media', title: 'Descargar con Bakin', contexts: ['video', 'audio', 'image'] },
];

const COOKIES_ITEM = { id: 'bakin-cookies', title: 'Darle a Bakin mi sesión de YouTube', contexts: ['action'] };

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const item of [...MENU, COOKIES_ITEM]) chrome.contextMenus.create(item);
  });
});

// One cookie as a cookies.txt (Netscape) line, the format yt-dlp reads.
function netscapeLine(c) {
  const domain = c.hostOnly ? c.domain : (c.domain.startsWith('.') ? c.domain : `.${c.domain}`);
  const expires = c.session ? 0 : Math.floor(c.expirationDate || 0);
  const line = [domain, c.hostOnly ? 'FALSE' : 'TRUE', c.path, c.secure ? 'TRUE' : 'FALSE', expires, c.name, c.value].join('\t');
  return c.httpOnly ? `#HttpOnly_${line}` : line;
}

function say(tab, text, ok) {
  chrome.action.setBadgeBackgroundColor({ color: ok ? '#2e9e63' : '#c0392b' });
  chrome.action.setBadgeText({ text: ok ? '✓' : '!' });
  chrome.action.setTitle({ title: text });
  setTimeout(() => {
    chrome.action.setBadgeText({ text: '' });
    chrome.action.setTitle({ title: 'Descargar esta página con Bakin' });
  }, 8000);
}

async function giveCookies(tab) {
  const all = [];
  for (const domain of ['youtube.com', 'google.com']) all.push(...await chrome.cookies.getAll({ domain }));
  const text = ['# Netscape HTTP Cookie File', ...all.map(netscapeLine)].join('\n') + '\n';
  chrome.runtime.sendNativeMessage('dev.bakin.cookies', { cookies: text }, (reply) => {
    if (chrome.runtime.lastError || !reply) {
      // the host is registered when Bakin starts: an older Bakin, or not opened since the update
      say(tab, 'Bakin no respondió: ábrelo una vez (versión 0.24.1 o más nueva) y vuelve a probar.', false);
      return;
    }
    if (!reply.ok) { say(tab, reply.error || 'Bakin no pudo guardar la sesión.', false); return; }
    say(tab, 'Sesión de YouTube guardada en Bakin.', true);
    // Bakin takes them now if it's open (or when it opens)
    chrome.tabs.update(tab.id, { url: 'bakin://cookies' });
  });
}

function sendToBakin(tab, url) {
  if (!/^https?:\/\//i.test(url || '')) return;
  const target = `bakin://download?url=${encodeURIComponent(url)}`;
  // Navigating to an app protocol leaves the page as it is; the browser asks
  // once whether to open Bakin (you can tell it to remember the choice).
  chrome.tabs.update(tab.id, { url: target });
}

chrome.action.onClicked.addListener((tab) => sendToBakin(tab, tab.url));

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'bakin-cookies') return giveCookies(tab);
  if (info.menuItemId === 'bakin-link') return sendToBakin(tab, info.linkUrl);
  if (info.menuItemId === 'bakin-media') {
    // A <video> on YouTube and similar sites is a blob: stream; the page
    // address is what yt-dlp needs there.
    const src = info.srcUrl || '';
    return sendToBakin(tab, /^https?:/i.test(src) && !/youtube\.com|youtu\.be/i.test(info.pageUrl || '') ? src : info.pageUrl);
  }
  return sendToBakin(tab, info.pageUrl || tab.url);
});
