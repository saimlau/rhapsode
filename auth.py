"""Single-user session auth for the hosted server.

Replaces nginx HTTP Basic (which the browser renders itself, so it can't be
styled) with a login form and a signed session cookie. Standard library only —
no new dependency for something this security-sensitive.

Password: scrypt with a per-password random salt, compared in constant time.
Session: an HMAC-SHA256 signed "<expiry>.<signature>" token. The signing key
lives beside the library (never in git), so sessions survive restarts but a
stolen cookie expires.

    python -c "import auth; print(auth.hash_password('...'))"
puts the hash in config.toml under [auth] password_hash.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

SCRYPT_N, SCRYPT_R, SCRYPT_P, DKLEN = 2 ** 14, 8, 1, 32
COOKIE = "rhapsode_session"
DEFAULT_TTL = 30 * 24 * 3600           # 30 days


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(txt):
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def hash_password(password, salt=None):
    """'scrypt$n$r$p$salt$hash' — self-describing so parameters can change."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, dklen=DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password, stored):
    """Constant-time check. False (never an exception) on anything malformed."""
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n),
                            r=int(r), p=int(p), dklen=len(_unb64(want)))
        return hmac.compare_digest(dk, _unb64(want))
    except Exception:
        return False


def load_secret(library_root):
    """Signing key beside the library: generated once, 0600, never in git.
    Losing it only logs sessions out."""
    path = Path(library_root) / ".session_secret"
    try:
        raw = path.read_bytes()
        if len(raw) >= 32:
            return raw
    except OSError:
        pass
    raw = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    except OSError:
        pass          # in-memory only: sessions then end with the process
    return raw


def issue(secret, ttl=DEFAULT_TTL):
    exp = str(int(time.time() + ttl))
    sig = hmac.new(secret, exp.encode(), hashlib.sha256).digest()
    return f"{exp}.{_b64(sig)}"


def valid(token, secret):
    """True only for a well-formed, correctly signed, unexpired token."""
    try:
        exp, sig = str(token).split(".", 1)
        expect = hmac.new(secret, exp.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), expect):
            return False          # signature checked BEFORE trusting expiry
        return int(exp) > time.time()
    except Exception:
        return False
