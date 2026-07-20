#!/usr/bin/env bash
# Dev-install the Rhapsode Zotero plugin into the local Zotero profile.
# Run while Zotero is CLOSED; then start Zotero normally.
set -euo pipefail

# match the executable name exactly (-x): a full-cmdline match (-f) would
# match this script's own path, which contains "zotero"
if pgrep -x zotero-bin > /dev/null 2>&1 || pgrep -x zotero > /dev/null 2>&1; then
  echo "Zotero appears to be running — quit it first, then re-run this."
  exit 1
fi

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE=$(ls -d "$HOME"/.zotero/zotero/*.default* 2>/dev/null | head -1)
if [ -z "$PROFILE" ]; then
  echo "No Zotero profile found under ~/.zotero/zotero/"
  exit 1
fi

mkdir -p "$PROFILE/extensions"
# remove any pre-rename install so the two IDs don't coexist
rm -f "$PROFILE/extensions/paper2audio@saimai.lau" \
      "$PROFILE/extensions/paper2audio@saimai.lau.xpi"
# and any released .xpi of THIS id — a packaged install and a source proxy
# share the id, so leaving both would make which one loads undefined
rm -f "$PROFILE/extensions/rhapsode@saimai.lau.xpi"
printf '%s' "$PLUGIN_DIR" > "$PROFILE/extensions/rhapsode@saimai.lau"

# Force Zotero to rescan extensions on next launch
sed -i '/extensions.lastAppBuildId/d;/extensions.lastAppVersion/d' \
  "$PROFILE/prefs.js"

echo "Installed proxy: $PROFILE/extensions/rhapsode@saimai.lau -> $PLUGIN_DIR"
echo "Start Zotero, then right-click a paper: 'Listen with Rhapsode'."
