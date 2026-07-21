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
// Remote mode: point the plugin at a hosted Rhapsode instead of localhost.
// Declared empty so both appear in Zotero's Config Editor — a pref with no
// default cannot be found there, let alone edited.
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
  manifest.json bootstrap.js prefs.js)
echo "Built: $DIST/rhapsode.xpi"
