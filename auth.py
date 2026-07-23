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
import json
import re
import threading
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


def issue(secret, user="", ttl=DEFAULT_TTL, epoch=""):
    """'<user>.<epoch>.<expiry>.<signature>'.

    The signature covers all three together, so none can be edited alone. The
    epoch is the account's current generation: rotating it (a password change,
    a deletion, a recreated name) invalidates every session that account had,
    which a username alone could never express.
    """
    exp = str(int(time.time() + ttl))
    payload = f"{_b64(user.encode())}.{_b64(epoch.encode())}.{exp}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(sig)}"


def session_claims(token, secret):
    """(user, epoch) this token authenticates, or None. None for anything
    malformed, wrongly signed, or expired — a caller that gets a name back
    may trust it completely."""
    try:
        user_b64, epoch_b64, exp, sig = str(token).split(".", 3)
        payload = f"{user_b64}.{epoch_b64}.{exp}"
        expect = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), expect):
            return None           # signature checked BEFORE trusting anything
        if int(exp) <= time.time():
            return None
        return _unb64(user_b64).decode(), _unb64(epoch_b64).decode()
    except Exception:
        return None


def session_user(token, secret):
    claims = session_claims(token, secret)
    return claims[0] if claims else None


def valid(token, secret):
    """True only for a well-formed, correctly signed, unexpired token."""
    return session_user(token, secret) is not None


# ------------------------------------------------------------------ users

USERS_FILE = "users.json"
INVITE_TTL = 14 * 24 * 3600            # a link that lingers forever is a key


def _now():
    return int(time.time())


class Users:
    """Accounts and invite tokens, in a 0600 file beside the library.

    Invite tokens are stored HASHED, exactly like passwords: whoever reads
    users.json must not come away with working invite links. The token itself
    exists only in the URL the admin copies once.
    """

    def __init__(self, library_root):
        self.path = Path(library_root) / USERS_FILE
        self.data = {"users": {}, "invites": {}}
        # bumped on every mutation; callers that cache a verified credential
        # stamp it with this and re-verify when it moves
        self.revision = 0
        self.lock = threading.RLock()
        self.load()

    # -- persistence ---------------------------------------------------
    def load(self):
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            pass                    # first run: an empty store is correct
        except (OSError, ValueError) as e:
            # A truncated or unreadable file used to come back as "no accounts
            # yet", which makes the bootstrap recreate the admin from config
            # and orphans every paper to a username that no longer exists.
            # Refuse to start instead — the data is still on disk to recover.
            raise SystemExit(
                f"error: {self.path} is unreadable ({e}). Refusing to start "
                f"with an empty account table; restore it from backup.")
        self.data.setdefault("users", {})
        self.data.setdefault("invites", {})
        self.data.setdefault("retired", [])
        return self.data

    def save(self):
        # Serialised, and to a per-writer temp path. Two threads sharing one
        # fixed "users.tmp" raced: one os.replace consumed the file the other
        # was still writing, so the loser raised and its change was lost from
        # disk while surviving in memory.
        with self.lock:
            self.revision += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(
                f".{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)  # atomic; never a half-written file
            try:
                os.chmod(self.path, 0o600)  # it holds password hashes
            except OSError:
                pass

    # -- accounts ------------------------------------------------------
    def exists(self, name):
        return name in self.data["users"]

    def epoch(self, name):
        """The account's current generation, or "" if it does not exist."""
        return self.data["users"].get(name, {}).get("epoch", "")

    def is_admin(self, name):
        return bool(self.data["users"].get(name, {}).get("admin"))

    def names(self):
        return sorted(self.data["users"])

    def admins(self):
        return [n for n in self.data["users"] if self.is_admin(n)]

    def create(self, name, password, admin=False, pw_hash=None):
        with self.lock:
            name = normalise_username(name)
            if not name:
                raise ValueError("username must be 3-32 characters: letters, "
                                 "digits, dot, dash or underscore")
            if self.exists(name) or name in self.data.get("retired", []):
                # a freed username is a claimable capability over the departed
                # member's shelf, because ownership is keyed on the name
                raise ValueError("that username is taken")
            if pw_hash is None:
                if len(password or "") < 8:
                    raise ValueError("password must be at least 8 characters")
                pw_hash = hash_password(password)
            self.data["users"][name] = {"pw": pw_hash, "admin": bool(admin),
                                        "created": _now(),
                                        "epoch": secrets.token_hex(8)}
            self.save()
            return name

    def check(self, name, password):
        """The username the credentials authenticate, or None. Always runs a
        scrypt hash so a missing account costs the same as a wrong password
        and cannot be told apart by timing."""
        entry = self.data["users"].get(normalise_username(name) or "")
        stored = entry["pw"] if entry else DUMMY_HASH
        ok = verify_password(password or "", stored)
        return normalise_username(name) if (ok and entry) else None

    def set_password(self, name, password):
        with self.lock:
            if len(password or "") < 8:
                raise ValueError("password must be at least 8 characters")
            self.data["users"][name]["pw"] = hash_password(password)
            # a new password ends every session issued under the old one
            self.data["users"][name]["epoch"] = secrets.token_hex(8)
            self.save()

    def set_modal_enc(self, name, blob):
        with self.lock:
            if name in self.data["users"]:
                self.data["users"][name]["modal_enc"] = blob
                self.save()

    def get_modal_enc(self, name):
        return self.data["users"].get(name, {}).get("modal_enc")

    def clear_modal_enc(self, name):
        with self.lock:
            self.data["users"].get(name, {}).pop("modal_enc", None)
            self.save()

    def has_modal(self, name):
        return bool(self.get_modal_enc(name))

    def set_quota(self, name, resource, value):
        with self.lock:
            u = self.data["users"].get(name)
            if u is None:
                return
            q = u.setdefault("quota", {})
            if value is None:
                q.pop(resource, None)
            else:
                q[resource] = float(value)
            if not q:
                u.pop("quota", None)
            self.save()

    def get_quota(self, name):
        return dict(self.data["users"].get(name, {}).get("quota", {}))

    def delete(self, name):
        with self.lock:
            if name not in self.data["users"]:
                raise ValueError("no such user")
            if self.is_admin(name) and len(self.admins()) == 1:
                raise ValueError("that is the only admin; promote another first")
            del self.data["users"][name]
            # Ownership is keyed on the username, so a freed name is a
            # claimable capability over the departed member's whole private
            # shelf — and invitees choose their own name. Retire it.
            self.data.setdefault("retired", []).append(name)
            self.save()

    # -- invites -----------------------------------------------------------
    def mint_invite(self, by, ttl=INVITE_TTL):
        """Returns the raw token — the only time it exists in clear."""
        with self.lock:
            token = secrets.token_urlsafe(24)
            self.data["invites"][_token_key(token)] = {
                "created": _now(), "expires": _now() + ttl,
                "by": by, "used_by": None}
            self.save()
            return token

    def invite_ok(self, token):
        inv = self.data["invites"].get(_token_key(token or ""))
        return bool(inv and not inv["used_by"] and inv["expires"] > _now())

    def redeem(self, token, name, password):
        """Create the account this invite is for, and burn the invite.

        Held under the lock end to end: checking the invite, creating the
        account and burning the link have to be one step, or two requests
        arriving together both pass the check and one link mints two accounts.
        """
        key = _token_key(token or "")
        with self.lock:
            if not self.invite_ok(token):
                raise ValueError("this invite link is invalid, used or expired")
            # claim the invite BEFORE creating the account: if creation fails
            # the claim is rolled back below, but no concurrent caller can slip
            # between the check and the claim
            self.data["invites"][key]["used_by"] = "(claiming)"
            try:
                created = self.create(name, password)
            except Exception:
                # only un-burn if the account really was NOT created: create()
                # inserts the user before it saves, so a failing save left a
                # live account behind a "usable" single-use invite
                if not self.exists(normalise_username(name)):
                    self.data["invites"][key]["used_by"] = None
                raise
            self.data["invites"][key]["used_by"] = created
            self.data["invites"][key]["used_at"] = _now()
            self.save()
            return created

    def revoke_invite(self, token_key):
        """Withdraw an unused invite by its stored key. A link that cannot be
        withdrawn is a bearer credential with a two-week life — and it travels
        in a URL, so it lands in browser history, chat logs and nginx's
        access log."""
        with self.lock:
            inv = self.data["invites"].get(token_key)
            if not inv:
                raise ValueError("no such invite")
            if inv["used_by"]:
                raise ValueError("that invite has already been used")
            del self.data["invites"][token_key]
            self.save()

    def open_invites(self):
        return [{"key": k, "by": v["by"], "created": v["created"],
                 "expires": v["expires"]}
                for k, v in self.data["invites"].items()
                if not v["used_by"] and v["expires"] > _now()]


def _token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
# a real scrypt hash of nothing in particular: verifying against it makes an
# unknown username cost the same as a known one
DUMMY_HASH = hash_password("rhapsode-timing-equaliser")


def normalise_username(name):
    name = (name or "").strip().lower()
    return name if USERNAME_RE.match(name) else ""
