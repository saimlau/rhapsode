/* Rhapsode for Zotero — thin shell around the local Rhapsode server.
 * Adds "Listen with Rhapsode" to the item context menu; the read-along
 * web app runs inside a Zotero tab via an embedded browser element. */
"use strict";

var rootURI;
var tabByWindow = new Map(); // main window -> { tabID, browser }

function log(msg) {
  Zotero.debug("[rhapsode] " + msg);
}

function getPort() {
  try {
    const p = Zotero.Prefs.get("extensions.rhapsode.port", true)
          || Zotero.Prefs.get("extensions.paper2audio.port", true);
    if (p) return parseInt(p, 10);
  } catch (e) {}
  return 7717;
}

function serverUrl() {
  const v = Zotero.Prefs.get("extensions.rhapsode.server_url", true) || "";
  return String(v).trim().replace(/\/+$/, "");
}
function isRemote() { return !!serverUrl(); }
function base() {
  return isRemote() ? serverUrl() : "http://127.0.0.1:" + getPort();
}
// Zotero's bootstrap scope is privileged JS, not a DOM window: FormData, File
// and fetch are NOT defined here. Build the multipart body by hand and send it
// through Zotero.HTTP.request, which every other call in this file already uses.
const BOUNDARY = "----RhapsodeFormBoundary7MA4YWxkTrZu0gW";

function multipartBody(fileBytes, filename, fields) {
  const enc = new TextEncoder();          // UTF-8: titles/authors aren't ASCII
  const chunks = [];
  for (const [name, value] of Object.entries(fields)) {
    chunks.push(enc.encode(
      `--${BOUNDARY}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n`
      + `${value}\r\n`));
  }
  chunks.push(enc.encode(
    `--${BOUNDARY}\r\nContent-Disposition: form-data; name="file"; `
    + `filename="${filename.replace(/"/g, "")}"\r\n`
    + `Content-Type: application/pdf\r\n\r\n`));
  chunks.push(fileBytes);
  chunks.push(enc.encode(`\r\n--${BOUNDARY}--\r\n`));

  const body = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0));
  let at = 0;
  for (const c of chunks) { body.set(c, at); at += c.length; }
  return body;
}

function authHeaders() {
  const auth = Zotero.Prefs.get("extensions.rhapsode.server_auth", true) || "";
  // btoa() takes a Latin-1 string: a password with any non-ASCII character
  // either throws InvalidCharacterError or silently encodes the wrong bytes.
  // Base64 the UTF-8 encoding instead, which is what the server decodes.
  if (!auth) return {};
  const bytes = new TextEncoder().encode(String(auth));
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return { Authorization: "Basic " + btoa(bin) };
}

function repoPath() {
  // The plugin lives at <repo>/zotero-plugin/, so the repo (and the
  // `rhapsode` launcher) is rootURI's parent when dev-installed.
  try {
    const uri = Services.io.newURI(rootURI);
    if (uri.schemeIs("file")) {
      return uri.QueryInterface(Ci.nsIFileURL).file.parent.path;
    }
  } catch (e) {}
  try {
    return Zotero.Prefs.get("extensions.rhapsode.repo", true)
           || Zotero.Prefs.get("extensions.paper2audio.repo", true) || null;
  } catch (e) {
    return null;
  }
}

async function serverAlive() {
  try {
    await Zotero.HTTP.request("GET", base() + "/api/library",
                              { timeout: 1500, headers: authHeaders() });
    return true;
  } catch (e) {
    return false;
  }
}

async function ensureServer() {
  if (isRemote()) {
    try {
      await Zotero.HTTP.request("GET", base() + "/api/library",
        { timeout: 8000, headers: authHeaders() });
      return;
    } catch (e) {
      throw new Error("Rhapsode server at " + base() + " is unreachable (" +
        (e.status || e.message) + "). Check extensions.rhapsode.server_url" +
        " and server_auth in the Config Editor.");
    }
  }
  if (await serverAlive()) return;
  const repo = repoPath();
  if (!repo) {
    throw new Error("Rhapsode server is not running, and the repo path "
      + "is unknown (set extensions.rhapsode.repo in the config editor)");
  }
  log("starting server from " + repo);
  const { Subprocess } =
    ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
  await Subprocess.call({
    command: repo + (Zotero.isWin ? "\\rhapsode.bat" : "/rhapsode"),
    arguments: ["--gui", "--no-open"],
  });
  for (let i = 0; i < 30; i++) {
    await Zotero.Promise.delay(500);
    if (await serverAlive()) return;
  }
  throw new Error("Rhapsode server did not come up (tried "
    + repo + "/rhapsode --gui --no-open)");
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
  if (isRemote()) {
    const path = att.getFilePath();
    if (!path) return null;          // same soft-skip as the local branch
    const meta = itemMeta(item);
    const resp = await Zotero.HTTP.request("POST", base() + "/api/papers", {
      body: multipartBody(await IOUtils.read(path), PathUtils.filename(path), {
        title: meta.title || "", authors: meta.authors || "",
        year: meta.year ? String(meta.year) : "", playlist: playlist || "",
      }),
      headers: { ...authHeaders(),
                 "Content-Type": "multipart/form-data; boundary=" + BOUNDARY },
      responseType: "text", timeout: 300000,
    });
    return JSON.parse(resp.responseText).id;
  }
  const path = await att.getFilePathAsync();
  if (!path) return null;
  const body = { path, ...itemMeta(item) };
  if (playlist) body.playlist = playlist;
  const resp = await Zotero.HTTP.request("POST", base() + "/api/papers/by-path", {
    headers: { "Content-Type": "application/json", ...authHeaders() },
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

async function sessionUrl(path) {
  // Ask the server, with our stored credentials, for a one-time login link so
  // the tab we open lands already signed in as the same account. Falls back to
  // the plain URL when there is no login gate (local, no password) or against
  // an older server — then the tab just shows the login page as before.
  try {
    const resp = await Zotero.HTTP.request("POST", base() + "/api/session-link", {
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: "{}", timeout: 15000,
    });
    const link = JSON.parse(resp.responseText).path;
    if (link) return base() + link + "?next=" + encodeURIComponent(path);
  } catch (e) {
    log("session-link failed (" + e + "); opening without auto-login");
  }
  return base() + path;
}

async function ensurePlaylist(name) {
  await Zotero.HTTP.request("POST", base() + "/api/playlists", {
    headers: { "Content-Type": "application/json", ...authHeaders() },
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
  openTab(win, await sessionUrl("/?playlist="
               + encodeURIComponent(rootPath.join(" / "))));
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
  openTab(win, await sessionUrl("/?play=" + encodeURIComponent(lastId)));
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
    type: "rhapsode",
    title: "Rhapsode",
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

async function _uninstallOldPlugin() {
  // the pre-rename plugin (paper2audio@saimai.lau) is a different add-on
  // ID, so it would coexist with this one: duplicate menus, two tabs.
  try {
    const { AddonManager } =
      ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const old = await AddonManager.getAddonByID("paper2audio@saimai.lau");
    if (old) {
      log("uninstalling old paper2audio plugin");
      await old.uninstall();
    }
  } catch (e) {
    log("old-plugin cleanup failed (harmless): " + e);
  }
}

async function startup({ rootURI: uri }) {
  rootURI = uri;
  log("startup, rootURI=" + rootURI);
  _uninstallOldPlugin();
  // A settings pane, so the server address and sign-in are discoverable.
  // The Config Editor can only show a pref that has a default, and even then
  // it cannot say that one wants a URL and the other "user:password".
  // Zotero unregisters the pane itself when the plugin shuts down.
  try {
    await Zotero.PreferencePanes.register({
      pluginID: "rhapsode@saimai.lau",
      src: rootURI + "prefs.xhtml",
      stylesheets: [rootURI + "prefs.css"],
      label: "Rhapsode",
    });
  } catch (e) {
    log("prefs pane registration failed: " + e);   // never block startup
  }
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
      Services.prompt.alert(win, "Rhapsode", String(err.message || err));
    });
  });
  menu.appendChild(sep);
  menu.appendChild(item);
}

function onMainWindowLoad({ window: win }) {
  _addMenuItem(win, "zotero-itemmenu", "rhapsode-menuitem",
               "Listen with Rhapsode", listen);
  _addMenuItem(win, "zotero-collectionmenu", "rhapsode-colmenuitem",
               "Listen to collection with Rhapsode", listenCollection);
}

function onMainWindowUnload({ window: win }) {
  for (const id of ["rhapsode-menuitem", "rhapsode-menuitem-sep",
                    "rhapsode-colmenuitem", "rhapsode-colmenuitem-sep"]) {
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
