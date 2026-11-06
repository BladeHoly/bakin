// Descargar con Bakin: hands a URL to the Bakin app through its bakin://
// link. The extension reads nothing from pages: it only passes the address
// you choose (the page, a link, a video or audio source), and Bakin always
// asks you to confirm before downloading anything.

const MENU = [
  { id: 'bakin-page', title: 'Descargar esta página con Bakin', contexts: ['page'] },
  { id: 'bakin-link', title: 'Descargar el enlace con Bakin', contexts: ['link'] },
  { id: 'bakin-media', title: 'Descargar con Bakin', contexts: ['video', 'audio', 'image'] },
];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const item of MENU) chrome.contextMenus.create(item);
  });
});

function sendToBakin(tab, url) {
  if (!/^https?:\/\//i.test(url || '')) return;
  const target = `bakin://download?url=${encodeURIComponent(url)}`;
  // Navigating to an app protocol leaves the page as it is; the browser asks
  // once whether to open Bakin (you can tell it to remember the choice).
  chrome.tabs.update(tab.id, { url: target });
}

chrome.action.onClicked.addListener((tab) => sendToBakin(tab, tab.url));

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'bakin-link') return sendToBakin(tab, info.linkUrl);
  if (info.menuItemId === 'bakin-media') {
    // A <video> on YouTube and similar sites is a blob: stream; the page
    // address is what yt-dlp needs there.
    const src = info.srcUrl || '';
    return sendToBakin(tab, /^https?:/i.test(src) && !/youtube\.com|youtu\.be/i.test(info.pageUrl || '') ? src : info.pageUrl);
  }
  return sendToBakin(tab, info.pageUrl || tab.url);
});
