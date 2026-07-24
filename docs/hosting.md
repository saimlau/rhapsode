# Hosting on a server

Rhapsode normally runs on the machine you listen from. It can also run on a
small server instead, so a paper keeps synthesizing while your laptop is
closed and every device reaches the same library.

The server does no synthesis itself. It extracts text, queues papers, and
serves the read-along; the GPU work goes to [Modal](backends.md), which
starts a container per batch and bills only while it runs. A €4/month VPS
with 2 vCPU and no GPU is enough.

!!! note "One password by default"
    Out of the box this is single-tenant: one password, one shared library.
    [Named accounts with private shelves](#accounts-for-a-few-colleagues) are
    a separate switch, off unless you turn it on.

## What you need

- A server with a public IP, 2 GB RAM, and room for the audio (roughly
  40 MB per hour of narration)
- A domain you control, for HTTPS
- A [Modal](https://modal.com) account for TTS and, optionally, LLM
  extraction

## Install

Rhapsode needs no GPU libraries on the server, so skip torch and kokoro —
they are what make the install large.

```bash
adduser --system --group --home /srv/rhapsode rhapsode
git clone https://github.com/saimlau/rhapsode /opt/rhapsode
cd /opt/rhapsode
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn requests pymupdf numpy python-multipart
apt install ffmpeg          # required: audio encoding
```

Create `/opt/rhapsode/config.toml`:

```toml
[library]
root = "/srv/rhapsode/library"

[tts]
backend = "modal"
modal_endpoint = "https://<you>--rhapsode-tts-kokorotts-tts.modal.run"
modal_token_id = "wk-..."
modal_token_secret = "ws-..."

[llm]
enabled = true
runner = "api"
api_base_url = "https://<you>--rhapsode-llm-serve.modal.run/v1"
api_key = "..."
model = "google/gemma-4-12B-it"

[auth]
password_hash = "scrypt$..."
```

The file holds credentials that spend money. Keep it private:

```bash
chown rhapsode:rhapsode /opt/rhapsode/config.toml
chmod 600 /opt/rhapsode/config.toml
```

Generate the password hash with:

```bash
.venv/bin/python -c "import auth; print(auth.hash_password('your password'))"
```

Deploy the Modal apps from your own machine, once —
`modal deploy modal_app.py` for TTS and `modal deploy modal_llm_app.py` for
extraction. See [Compute backends](backends.md).

## Run it

`/etc/systemd/system/rhapsode.service`:

```ini
[Unit]
Description=Rhapsode library server
After=network.target

[Service]
User=rhapsode
WorkingDirectory=/opt/rhapsode
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/rhapsode/.venv/bin/python rhapsode.py --gui --no-open
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`PYTHONUNBUFFERED=1` matters more than it looks: without it, progress lines
sit in a pipe buffer and `journalctl` shows nothing while a paper generates.

```bash
systemctl enable --now rhapsode
```

The server listens on `127.0.0.1:7717` — never expose it directly. It has no
TLS, and its login is designed to sit behind one.

## HTTPS

Put nginx in front. The parts that matter:

```nginx
server {
    listen 443 ssl http2;
    server_name rhapsode.example.com;

    ssl_certificate     /etc/nginx/ssl/rhapsode.pem;
    ssl_certificate_key /etc/nginx/ssl/rhapsode.key;

    client_max_body_size 100m;      # theses are large PDFs

    location / {
        proxy_pass http://127.0.0.1:7717;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;   # see trust_proxy
        proxy_buffering off;        # progress is streamed; buffering stalls it
        proxy_read_timeout 3600s;   # a long paper holds the connection
    }
}
```

If you enable accounts, add rate limiting. Every failed credential costs a
deliberate scrypt hash — at the login form, on the join page, and in the HTTP
Basic header that machine clients send on ordinary requests. The app throttles all three itself, keyed on the connection's source. Behind
a proxy that source is the proxy, so set `[auth] trust_proxy = true` **and**
the `X-Forwarded-For $remote_addr` line above together — the app then throttles
per real client. Leave trust_proxy off and the header is ignored (unspoofable,
but all users share one bucket). nginx `limit_req` is the cheaper first line
regardless. Also stop logging invite tokens:

```nginx
# in the http { } block
limit_req_zone $binary_remote_addr zone=rhapsode_auth:1m rate=10r/m;

# in the server { } block
location = /login { limit_req zone=rhapsode_auth burst=5 nodelay;
                    proxy_pass http://127.0.0.1:7717; }
location /join/  { limit_req zone=rhapsode_auth burst=5 nodelay;
                   access_log off;      # the invite token is in the URL
                   proxy_pass http://127.0.0.1:7717; }
```

Two settings are easy to miss. Without `proxy_buffering off` the progress
stream arrives in chunks and the queue looks frozen. Without a raised
`client_max_body_size` a large PDF upload fails with 413 rather than a
useful error.

On nginx 1.24 and earlier, HTTP/2 goes on the `listen` line as shown; the
standalone `http2 on;` directive only exists from 1.25.

For certificates, either Let's Encrypt via certbot, or — if the domain is on
Cloudflare with the proxy enabled — a Cloudflare Origin CA certificate,
which lasts fifteen years and needs no renewal.

## Logging in

Visiting any page redirects to a login form. A successful login sets an
HMAC-signed, HttpOnly session cookie; the signing key is generated on first
run and stored `0600` in the library root, so restarts keep you logged in
but deleting it ends every session.

Machine clients send HTTP Basic with the same password instead — that is how
the Zotero plugin authenticates, and it works for `curl` too:

```bash
curl -u "you:password" https://rhapsode.example.com/api/library
```

If no `password_hash` is configured, authentication is off entirely and a
localhost install behaves exactly as it always has.

## Accounts for a few colleagues

Everything above is single-tenant: one password, one shared library. Named
accounts are a separate switch.

!!! warning "Back up before you flip it"
    The first start with `multiuser = true` rewrites `library.json` in
    place. Copy `library.json` and `users.json` somewhere off the server
    first — the automatic `.bak` is overwritten by the very next save.

```toml
[auth]
password_hash = "scrypt$..."
multiuser = true
admin_user = "saimai"
```

On the next start the server creates `admin_user` from the password you
already use — so the flip cannot lock you out — and marks every existing
paper as theirs, **private**. Colleagues see nothing until you share
something with them.

### Inviting someone

Visit `/admin` (admins only). "Create an invite link" produces a single-use
URL valid for 14 days; the invitee opens it, chooses their own username and
password, and lands in an empty shelf. You never handle their password.

Anyone holding that link can create an account, so send it the way you would
send a password — and revoke it from the same page if it goes astray. It
travels in a URL, so it also lands in browser history and the web server's
access log; the nginx snippet below stops logging it.

### What each person can see

| | their own papers | a shared paper | someone else's private paper |
| --- | --- | --- | --- |
| read | yes | yes | no — 404, indistinguishable from a wrong id |
| regenerate, rename, delete | yes | no, only its owner | no |
| share | yes | no | no |

Admins see and may change everything. Sharing a paper does not hand over
control of it.

Removing an account does **not** remove its papers: they keep an owner who
can no longer sign in, and the username is retired so it can never be
claimed by someone else (it would otherwise inherit that shelf). Reassign
anything you still want first.

### Limits

Each non-admin account is capped at 200 papers, because every paper is GPU
time on **your** Modal account. Raise `PAPERS_PER_USER` in `server.py` if
that is wrong for your group. There is no bandwidth or disk quota beyond
that — invite people you would lend your laptop to.

### Per-user Modal, and per-user quotas

By default every paper is synthesised on **your** Modal account — you pay for
the whole group. Phase 5 adds two ways to change that: a colleague can attach
their own Modal so their papers bill to them, and you can cap how much of
*your* compute each person spends.

Turn it on by adding an encryption key. Generate one:

```bash
.venv/bin/python rhapsode.py --gen-key
```

Paste the printed line into `config.toml`:

```toml
[secrets]
key = "<base64 of 32 bytes>"
```

The key encrypts each user's stored Modal token (AES-256-GCM) so a copy of the
library alone can never decrypt one. That is also the catch:

!!! danger "Back the key up separately from the library"
    The key lives **only** in `config.toml`, never in the library. If you lose
    it, every attached Modal profile becomes permanently unrecoverable — users
    would have to re-enter their tokens. Back `config.toml` up somewhere other
    than where you back up the library, so a single lost disk cannot take both.

With no `[secrets] key` set, this feature is simply off: the settings page says
so, per-user Modal is unavailable, and everything runs on the operator account
exactly as before. Restart after adding the key.

**Users attach their own credentials at `/settings`.** A signed-in colleague
opens the library footer's Settings link. Two independent forms let them attach
their own **TTS** (narration) Modal endpoint + token and their own **LLM**
(extraction) OpenAI-compatible base URL + API key; either, both, or neither. A
**Test** button on each runs a tiny probe against *their* own endpoint to
confirm it works — the only thing that ever contacts a backend on a click, and
only their container, their cost. From then on the papers they generate bill to
them and are never counted against any cap. Secrets are write-only: the page
shows only that a credential is attached and its last four characters, never the
value. Saving one form never disturbs the other, and each **Clear** removes only
its own credential.

**You set per-user audio-hour caps on the People page** (`/admin`). Each account
shows papers, audio-hours, whether they self-host, and their operator-hours as
`used / cap`. "Set cap" (or "Change") sets a ceiling in audio hours; leave it
blank for unlimited. The cap counts only operator-billed hours — a user on their
own Modal is never gated.

The ceiling is soft. When a user is over their cap, their next operator-billed
paper is not synthesised: it shows in the queue as **`blocked`** with the reason
*"Over your shared-compute quota. Attach your own Modal in Settings, or ask an
admin to raise your cap."* The paper stays and is regenerable — it turns into
real audio the moment the user attaches their own Modal (their jobs stop
counting) or you raise the cap. The job that crosses the line may overshoot by
one paper; that is expected for a soft ceiling.

### Rolling it back

Set `multiuser = false` and restart. The single password works again and
every paper stays where it is. Existing sessions are not invalidated by the
change, so delete `.session_secret` in the library root at the same time if
you are rolling back because an account was compromised.

## Point Zotero at it

In Zotero's Config Editor set `extensions.rhapsode.server_url` to your
domain and `extensions.rhapsode.server_auth` to `user:password`. Details in
[the plugin guide](zotero.md). Requires plugin 0.3.2 or newer.

## Operating it

```bash
journalctl -u rhapsode -f          # follow the queue
systemctl restart rhapsode         # after a git pull
```

A restart interrupts the paper being narrated, but does not lose it:
completed audio is checkpointed to disk and the paper resumes where it
stopped rather than starting over.

To update:

```bash
cd /opt/rhapsode
sudo -u rhapsode git pull
systemctl restart rhapsode
```

The library is a directory of self-contained papers plus `library.json`.
Backing up `/srv/rhapsode/library` backs up everything; `library.json` is
also written with a `.bak` alongside it, and can be rebuilt from the paper
directories if both are lost.
