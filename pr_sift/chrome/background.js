// Relays the content script's requests to pr-sift, which Chrome starts as
// a native messaging host, and opens reports in tabs.
//
// One connection serves every tab. While it is open Chrome keeps both
// this worker and pr-sift running, and with them the report servers.

const HOST = "pr_sift";
const NOT_FOUND = "Specified native messaging host not found.";

let port = null;
let nextId = 1;
const pending = new Map();

function connect() {
  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener((message) => {
    const resolve = pending.get(message.id);
    pending.delete(message.id);
    resolve?.(message);
  });
  port.onDisconnect.addListener(() => {
    const error = chrome.runtime.lastError?.message || "pr-sift stopped";
    port = null;
    for (const resolve of pending.values()) resolve({ ok: false, error, installed: error !== NOT_FOUND });
    pending.clear();
  });
}

function request(message) {
  if (!port) connect();
  const id = nextId++;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    port.postMessage({ ...message, id });
  });
}

// The report of a PR opens next to its GitHub tab, or in the tab it opened
// in before, which then loads the report again if pr-sift restarted.
async function openReport(pr, from) {
  const reply = await request({ type: "open", pr });
  if (!reply.ok) return reply;
  const { tabs = {} } = await chrome.storage.session.get("tabs");
  const known = tabs[pr];
  if (known) {
    try {
      const tab = await chrome.tabs.update(known.tab, { active: true, ...(known.url !== reply.url && { url: reply.url }) });
      await chrome.windows.update(tab.windowId, { focused: true });
      tabs[pr] = { tab: tab.id, url: reply.url };
      await chrome.storage.session.set({ tabs });
      return reply;
    } catch {
      // the tab was closed
    }
  }
  const tab = await chrome.tabs.create({
    url: reply.url,
    ...(from && { index: from.index + 1, openerTabId: from.id, windowId: from.windowId }),
  });
  tabs[pr] = { tab: tab.id, url: reply.url };
  await chrome.storage.session.set({ tabs });
  return reply;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const reply = message.type === "open" ? openReport(message.pr, sender.tab) : request(message);
  reply.then(sendResponse, (e) => sendResponse({ ok: false, error: String(e?.message || e) }));
  return true;
});
