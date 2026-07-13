#!/usr/bin/env bash
# Build dist/paper2audio.xpi for Zotero's "Install Plugin From File".
# Bakes this machine's repo path into prefs.js so server autostart works
# even though packed plugins can't derive their location on disk.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$PLUGIN_DIR")"
DIST="$PLUGIN_DIR/dist"
mkdir -p "$DIST"

cat > "$PLUGIN_DIR/prefs.js" <<EOF
pref("extensions.paper2audio.repo", "$REPO");
pref("extensions.paper2audio.port", 7717);
EOF

rm -f "$DIST/paper2audio.xpi"
(cd "$PLUGIN_DIR" && zip -q "$DIST/paper2audio.xpi" \
  manifest.json bootstrap.js prefs.js)
echo "Built: $DIST/paper2audio.xpi"
