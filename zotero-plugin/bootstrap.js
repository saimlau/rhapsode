/* paper2audio for Zotero — thin shell around the local paper2audio server.
 * Adds "Listen with paper2audio" to the item context menu; the read-along
 * web app runs inside a Zotero tab via an embedded browser element. */
"use strict";

var rootURI;
var tabByWindow = new Map(); // main window -> { tabID, browser }

function log(msg) {
  Zotero.debug("[paper2audio] " + msg);
}

function getPort() {
  try {
    const p = Zotero.Prefs.get("extensions.paper2audio.port", true);
    if (p) return parseInt(p, 10);
  } catch (e) {}
  return 7717;
}

function base() {
  return "http://127.0.0.1:" + getPort();
}

function repoPath() {
  // The plugin lives at <repo>/zotero-plugin/, so the repo (and the
  // `paper2audio` launcher) is rootURI's parent when dev-installed.
  try {
    const uri = Services.io.newURI(rootURI);
    if (uri.schemeIs("file")) {
      return uri.QueryInterface(Ci.nsIFileURL).file.parent.path;
    }
  } catch (e) {}
  try {
    return Zotero.Prefs.get("extensions.paper2audio.repo", true) || null;
  } catch (e) {
    return null;
  }
}

async function serverAlive() {
  try {
    await Zotero.HTTP.request("GET", base() + "/api/library", { timeout: 1500 });
    return true;
  } catch (e) {
    return false;
  }
}

async function ensureServer() {
  if (await serverAlive()) return;
  const repo = repoPath();
  if (!repo) {
    throw new Error("paper2audio server is not running, and the repo path "
      + "is unknown (set extensions.paper2audio.repo in the config editor)");
  }
  log("starting server from " + repo);
  const { Subprocess } =
    ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
  await Subprocess.call({
    command: repo + "/paper2audio",
    arguments: ["--gui", "--no-open"],
  });
  for (let i = 0; i < 30; i++) {
    await Zotero.Promise.delay(500);
    if (await serverAlive()) return;
  }
  throw new Error("paper2audio server did not come up (tried "
    + repo + "/paper2audio --gui --no-open)");
}

async function sendItem(att) {
  const path = await att.getFilePathAsync();
  if (!path) return null;
  const resp = await Zotero.HTTP.request("POST", base() + "/api/papers/by-path", {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return JSON.parse(resp.responseText).id;
}

async function listen(win) {
  const items = win.ZoteroPane.getSelectedItems();
  if (!items.length) return;
  await ensureServer();
  let lastId = null;
  for (const item of items) {
    let att = null;
    if (item.isAttachment()) {
      att = item;
    } else if (item.isRegularItem()) {
      att = await item.getBestAttachment();
    }
    if (!att || att.attachmentContentType !== "application/pdf") continue;
    const id = await sendItem(att);
    if (id) lastId = id;
  }
  if (!lastId) {
    throw new Error("No PDF attachment found on the selected item(s)");
  }
  openTab(win, base() + "/?play=" + encodeURIComponent(lastId));
}

function loadInBrowser(browser, url) {
  try {
    browser.loadURI(Services.io.newURI(url), {
      triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal(),
    });
  } catch (e) {
    log("loadURI failed (" + e + "); falling back to src attribute");
    browser.setAttribute("src", url);
  }
}

function openTab(win, url) {
  const existing = tabByWindow.get(win);
  if (existing && win.Zotero_Tabs._tabs.some(t => t.id === existing.tabID)) {
    loadInBrowser(existing.browser, url);
    win.Zotero_Tabs.select(existing.tabID);
    return;
  }
  // Zotero's gBrowser shim lacks getTabForBrowser; Gecko's browser custom
  // element calls it on pagehide and throws — give it a harmless no-op
  if (win.gBrowser && typeof win.gBrowser.getTabForBrowser !== "function") {
    win.gBrowser.getTabForBrowser = () => null;
  }
  const { id, container } = win.Zotero_Tabs.add({
    type: "paper2audio",
    title: "paper2audio",
    data: {},  // Zotero 9's tab bar reads tab.data.icon; must not be undefined
    select: true,
    onClose: () => tabByWindow.delete(win),
  });
  log("tab created: " + id);
  const browser = win.document.createXULElement("browser");
  browser.setAttribute("type", "content");
  browser.setAttribute("remote", "true");
  browser.setAttribute("maychangeremoteness", "true");
  browser.setAttribute("flex", "1");
  browser.style.width = "100%";
  browser.style.height = "100%";
  container.appendChild(browser);
  loadInBrowser(browser, url);
  tabByWindow.set(win, { tabID: id, browser });
}

// ------------------------------------------------------------ plugin hooks

function install() {}
function uninstall() {}

function startup({ rootURI: uri }) {
  rootURI = uri;
  log("startup, rootURI=" + rootURI);
  // onMainWindowLoad only fires for windows opened after startup; when the
  // plugin is installed into a running Zotero, inject into existing windows
  for (const win of Zotero.getMainWindows()) {
    try {
      onMainWindowLoad({ window: win });
    } catch (e) {
      log("existing-window inject failed: " + e);
    }
  }
}

function onMainWindowLoad({ window: win }) {
  const doc = win.document;
  const menu = doc.getElementById("zotero-itemmenu");
  if (!menu || doc.getElementById("paper2audio-menuitem")) return;
  const sep = doc.createXULElement("menuseparator");
  sep.id = "paper2audio-menusep";
  const item = doc.createXULElement("menuitem");
  item.id = "paper2audio-menuitem";
  item.setAttribute("label", "Listen with paper2audio");
  item.addEventListener("command", () => {
    listen(win).catch(err => {
      log("error: " + err);
      Services.prompt.alert(win, "paper2audio", String(err.message || err));
    });
  });
  menu.appendChild(sep);
  menu.appendChild(item);
}

function onMainWindowUnload({ window: win }) {
  for (const id of ["paper2audio-menuitem", "paper2audio-menusep"]) {
    win.document.getElementById(id)?.remove();
  }
  tabByWindow.delete(win);
}

function shutdown() {
  for (const win of Zotero.getMainWindows()) {
    const existing = tabByWindow.get(win);
    if (existing) {
      try { win.Zotero_Tabs.close(existing.tabID); } catch (e) {}
    }
    onMainWindowUnload({ window: win });
  }
  tabByWindow.clear();
}
