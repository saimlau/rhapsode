# Zotero plugin

The plugin adds two context-menu actions inside Zotero (7, 8, and 9):

- **Listen with Rhapsode** on an item — sends its PDF to the local server
  and opens the read-along view in a Zotero tab.
- **Listen to collection with Rhapsode** on a collection — every PDF in it
  becomes part of a playlist named after the collection, with
  subcollections as their own indented "Parent / Child" playlists.

Zotero's own metadata (title, authors, year) is authoritative for papers
added this way — the library shows exactly what Zotero knows, not what PDF
extraction guessed.

## Install

Download the latest `rhapsode.xpi` from the
[releases page](https://github.com/saimlau/rhapsode/releases/latest), then
in Zotero: **Tools → Plugins → ⚙ → Install Plugin From File**. The plugin
auto-updates from future releases. If you had the old *paper2audio* plugin,
it is uninstalled automatically.

If a Rhapsode server is already running (default port 7717), the plugin
just uses it. To let the plugin **start the server itself**, tell it where
your checkout lives: **Zotero → Settings → Rhapsode → Local server**, and
give the path to the cloned repo. (A custom port sits beside it.)

### Using a hosted server

The plugin can send papers to a Rhapsode running on another machine instead
of on this one — see [Hosting on a server](hosting.md).

Open **Zotero → Settings → Rhapsode** and fill in **Hosted server**:

| Field | Value |
| --- | --- |
| Address | `https://rhapsode.example.com` |
| Sign-in | `user:password`, if the server asks for one |

Leave the address empty for local mode, which is the default and behaves
exactly as before. When it is set the plugin never starts a local server and
the Rhapsode folder is ignored — every upload and the library tab go to the
remote host, so your laptop does no synthesis and needs no GPU.

The same settings are `extensions.rhapsode.server_url` and
`server_auth` in the Config Editor, if you prefer editing them there.

Requires plugin 0.3.3 or newer for the settings pane; 0.3.2 has the prefs but
no UI for them.

For development, use `zotero-plugin/dev-install.sh` **while Zotero is
closed** — it registers the source directory directly and pre-sets the
repo path; `zotero-plugin/build-xpi.sh` builds the XPI.

## How it works

On first use the plugin checks for a running server and otherwise launches
`rhapsode --gui --no-open` itself (via `rhapsode.bat` on Windows), then
streams each PDF to the server with its Zotero metadata and opens the
library in a tab. Locally that upload is a path handoff (`/api/papers/by-path`
— the server reads the file in place); against a hosted server the PDF itself
is uploaded to `/api/papers`, since the remote machine cannot see your disk. Generation continues in the background — a
large collection is queued in seconds and synthesizes one paper at a time.

Combine with `[gui] idle_exit_min` in `config.toml` and the server also
goes away by itself when you stop listening.

## Troubleshooting

Turn on **Help → Debug Output Logging → View Output** in Zotero; the plugin
logs lines prefixed `[rhapsode]` covering server discovery, uploads, and
tab handling. After upgrading Rhapsode itself, restart the server (or quit
Zotero and let it relaunch one) so plugin and server versions match. See
also [Troubleshooting & FAQ](troubleshooting.md).
