#!/usr/bin/env bash
# Speech Dispatcher generic-module hook: text on stdin -> Kokoro via the
# local paper2audio server -> speakers. Args: $1 = speechd rate (-100..100),
# $2 = voice id (from AddVoice in kokoro.conf).
RATE_RAW="${1:-0}"
VOICE="${2:-af_heart}"
PORT="${RHAPSODE_PORT:-${PAPER2AUDIO_PORT:-7717}}"
PLAYER="${RHAPSODE_PLAYER:-${PAPER2AUDIO_PLAYER:-aplay -q}}"

# speechd rate -100..100 -> speed 0.5..2.0 (exponential feels linear)
SPEED=$(awk "BEGIN { printf \"%.2f\", 2 ^ ($RATE_RAW / 100) }")

TEXT=$(cat)
[ -z "$TEXT" ] && exit 0

# A server with a password answers 401, and without -f curl pipes that error
# page straight to the audio player as if it were a WAV. RHAPSODE_AUTH is
# "user:password"; unset, this behaves exactly as before on a local server.
AUTH_ARGS=()
[ -n "${RHAPSODE_AUTH:-}" ] && AUTH_ARGS=(-u "$RHAPSODE_AUTH")

curl -sf --max-time 120 "${AUTH_ARGS[@]}" \
  --data-urlencode "text=$TEXT" \
  --data-urlencode "rate=$SPEED" \
  --data-urlencode "voice=$VOICE" \
  "http://127.0.0.1:$PORT/tts" | $PLAYER
