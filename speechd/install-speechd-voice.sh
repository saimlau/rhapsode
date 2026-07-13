#!/usr/bin/env bash
# Register Kokoro (via paper2audio) as a user-level Speech Dispatcher voice.
# No root needed; config goes to ~/.config/speech-dispatcher/.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONF_DIR="$HOME/.config/speech-dispatcher"
mkdir -p "$CONF_DIR/modules"

sed "s|@REPO@|$REPO|g" "$REPO/speechd/kokoro.conf.in" \
  > "$CONF_DIR/modules/kokoro.conf"

# A user speechd.conf fully replaces the system one, so start from a copy
# to keep the existing modules (espeak-ng etc.) available to other apps.
if [ ! -f "$CONF_DIR/speechd.conf" ]; then
  cp /etc/speech-dispatcher/speechd.conf "$CONF_DIR/speechd.conf"
fi
# Any explicit AddModule disables speechd's module auto-detection, so the
# system modules must be re-listed or they'd vanish for every other app.
add_module() {
  grep -q "^AddModule \"$1\"" "$CONF_DIR/speechd.conf" || \
    echo "AddModule \"$1\" \"$2\" \"$3\"" >> "$CONF_DIR/speechd.conf"
}
[ -x /usr/lib/speech-dispatcher-modules/sd_espeak-ng ] && \
  add_module espeak-ng sd_espeak-ng espeak-ng.conf
[ -x /usr/lib/speech-dispatcher-modules/sd_openjtalk ] && \
  add_module openjtalk sd_openjtalk openjtalk.conf
add_module kokoro sd_generic kokoro.conf

# Restart the (on-demand) daemon so it picks up the new module.
# Process names are truncated to 15 chars, hence "speech-dispatch".
pkill -x speech-dispatch 2>/dev/null || pkill -x speech-dispatcher 2>/dev/null || true

echo "Kokoro registered with Speech Dispatcher."
echo "The paper2audio server must be running (paper2audio --gui)."
echo "Test with:  spd-say -o kokoro 'Hello from Kokoro'"
