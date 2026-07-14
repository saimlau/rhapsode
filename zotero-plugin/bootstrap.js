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

function itemMeta(item) {
  // Zotero's curated metadata beats anything extracted from the PDF
  const src = item.isAttachment() ? (item.parentItem || item) : item;
  const authors = src.getCreators()
    .map(c => (c.firstName ? c.firstName + " " : "") + (c.lastName || ""))
    .map(s => s.trim()).filter(Boolean).join(", ");
  const date = Zotero.Date.strToDate(src.getField("date"));
  return {
    title: src.getField("title") || null,
    authors: authors || null,
    year: date && date.year ? date.year : null,
  };
}

async function sendItem(item, att, playlist) {
  const path = await att.getFilePathAsync();
  if (!path) return null;
  const body = { path, ...itemMeta(item) };
  if (playlist) body.playlist = playlist;
  const resp = await Zotero.HTTP.request("POST", base() + "/api/papers/by-path", {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    timeout: 120000,  // default 30 s is too tight for very large PDFs
  });
  return JSON.parse(resp.responseText).id;
}

async function bestPdf(item) {
  let att = null;
  if (item.isAttachment()) att = item;
  else if (item.isRegularItem()) att = await item.getBestAttachment();
  return att && att.attachmentContentType === "application/pdf" ? att : null;
}

function collectionPath(col) {
  // full ancestry, so a right-click on a nested subcollection resolves to
  // the same "Grandparent / Parent / Child" playlist as a top-level send
  const segs = [];
  for (let c = col; c; c = c.parentID ? Zotero.Collections.get(c.parentID)
                                      : null) {
    segs.unshift(c.name);
  }
  return segs;
}

async function ensurePlaylist(name) {
  await Zotero.HTTP.request("POST", base() + "/api/playlists", {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
    timeout: 30000,
  });
}

async function listenCollection(win) {
  const col = win.ZoteroPane.getSelectedCollection();
  if (!col) throw new Error("No collection selected");
  await ensureServer();
  const walk = async (c, path) => {
    const playlist = path.join(" / ");
    let n = 0;
    for (const item of c.getChildItems()) {
      const att = await bestPdf(item);
      if (att && await sendItem(item, att, playlist)) n++;
    }
    for (const sub of c.getChildCollections()) {
      n += await walk(sub, path.concat(sub.name));  // subcollections become
    }                                               // "Parent / Child" playlists
    if (n) await ensurePlaylist(playlist);  // parents exist even when all
    return n;                               // their papers are in children
  };
  const rootPath = collectionPath(col);
  const sent = await walk(col, rootPath);
  if (!sent) throw new Error("No PDF attachments found in this collection");
  openTab(win, base() + "/?playlist="
               + encodeURIComponent(rootPath.join(" / ")));
}

async function listen(win) {
  const items = win.ZoteroPane.getSelectedItems();
  if (!items.length) return;
  await ensureServer();
  let lastId = null;
  for (const item of items) {
    const att = await bestPdf(item);
    if (!att) continue;
    const id = await sendItem(item, att, null);
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
  // Heal zombie tabs (data: undefined) left by earlier plugin versions:
  // Zotero_Tabs._update() reads tab.data.icon for every tab and one bad
  // entry breaks all tab operations until restart
  for (const t of win.Zotero_Tabs._tabs) {
    if (!t.data) t.data = {};
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

function _addMenuItem(win, menuId, itemId, label, handler) {
  const doc = win.document;
  const menu = doc.getElementById(menuId);
  if (!menu || doc.getElementById(itemId)) return;
  const sep = doc.createXULElement("menuseparator");
  sep.id = itemId + "-sep";
  const item = doc.createXULElement("menuitem");
  item.id = itemId;
  item.setAttribute("label", label);
  item.addEventListener("command", () => {
    handler(win).catch(err => {
      log("error: " + err);
      Services.prompt.alert(win, "paper2audio", String(err.message || err));
    });
  });
  menu.appendChild(sep);
  menu.appendChild(item);
}

function onMainWindowLoad({ window: win }) {
  _addMenuItem(win, "zotero-itemmenu", "paper2audio-menuitem",
               "Listen with paper2audio", listen);
  _addMenuItem(win, "zotero-collectionmenu", "paper2audio-colmenuitem",
               "Listen to collection with paper2audio", listenCollection);
}

function onMainWindowUnload({ window: win }) {
  for (const id of ["paper2audio-menuitem", "paper2audio-menuitem-sep",
                    "paper2audio-colmenuitem", "paper2audio-colmenuitem-sep"]) {
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
