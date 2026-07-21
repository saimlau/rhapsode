#!/usr/bin/env bash
# Build dist/rhapsode.xpi for Zotero's "Install Plugin From File".
# Bakes this machine's repo path into prefs.js so server autostart works
# even though packed plugins can't derive their location on disk.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$PLUGIN_DIR")"
DIST="$PLUGIN_DIR/dist"
mkdir -p "$DIST"

if [ "${1:-}" = "--release" ]; then
  # public build: no personal repo path — users set extensions.rhapsode.repo
  # themselves (or start the server manually) for autostart
  cat > "$PLUGIN_DIR/prefs.js" <<EOF
pref("extensions.rhapsode.port", 7717);
// Every pref the settings pane binds needs a default: Zotero renders the
// literal string "undefined" in a field whose pref does not exist.
pref("extensions.rhapsode.repo", "");
pref("extensions.rhapsode.server_url", "");
pref("extensions.rhapsode.server_auth", "");
EOF
else
  cat > "$PLUGIN_DIR/prefs.js" <<EOF
pref("extensions.rhapsode.repo", "$REPO");
pref("extensions.rhapsode.port", 7717);
pref("extensions.rhapsode.server_url", "");
pref("extensions.rhapsode.server_auth", "");
EOF
fi

rm -f "$DIST/rhapsode.xpi"
(cd "$PLUGIN_DIR" && zip -q "$DIST/rhapsode.xpi" \
  manifest.json bootstrap.js prefs.js prefs.xhtml prefs.css)
echo "Built: $DIST/rhapsode.xpi"
