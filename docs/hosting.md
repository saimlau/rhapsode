# Hosting on a server

Rhapsode normally runs on the machine you listen from. It can also run on a
small server instead, so a paper keeps synthesizing while your laptop is
closed and every device reaches the same library.

The server does no synthesis itself. It extracts text, queues papers, and
serves the read-along; the GPU work goes to [Modal](backends.md), which
starts a container per batch and bills only while it runs. A €4/month VPS
with 2 vCPU and no GPU is enough.

!!! note "One account, several devices"
    This is single-tenant: one password, one shared library. Everyone who
    logs in sees the same papers. Per-user accounts and private shelves are
    not built yet.

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
        proxy_buffering off;        # progress is streamed; buffering stalls it
        proxy_read_timeout 3600s;   # a long paper holds the connection
    }
}
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
