# Kokoro as a system voice (Linux)

`speechd/install-speechd-voice.sh` registers Kokoro as a user-level
[Speech Dispatcher](https://freebsoft.org/speechd) voice — no root, config
under `~/.config/speech-dispatcher/`. It then appears in the voice list of
any speechd-aware app: Zotero's built-in read-aloud, Firefox reader mode,
Orca, `spd-say`, and friends.

```bash
speechd/install-speechd-voice.sh
spd-say -o kokoro 'Hello from Kokoro'
```

!!! note
    The voice synthesizes through the local Rhapsode server, so
    `rhapsode --gui` must be running (with `[gui] idle_exit_min` +
    `packaging/rhapsode.service`, it can start on demand and exit when
    idle).

## Voices

The module maps speechd's generic voice slots to Kokoro ids:

| speechd voice | Kokoro id |
|---|---|
| FEMALE1 | `af_heart` |
| FEMALE2 | `af_bella` |
| MALE1 | `am_michael` |
| MALE2 | `am_fenrir` |

Pick them in the client app's voice settings, or
`spd-say -o kokoro -y FEMALE2 '…'`.

## Rate mapping

speechd rates (−100…100) map exponentially to Kokoro speed 0.5×–2×, so the
rate slider feels linear. Two environment variables tune the hook script:
`RHAPSODE_PORT` if your server isn't on 7717, and `RHAPSODE_PLAYER` to
replace the default `aplay -q`.

## How the install works

A user-level `speechd.conf` completely replaces the system one, and any
explicit `AddModule` disables speechd's module auto-detection — so the
installer copies the system config and re-lists the stock modules
(espeak-ng etc.) before adding Kokoro. Nothing else on the system changes;
delete `~/.config/speech-dispatcher/` to undo everything.
