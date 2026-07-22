"""Auth primitives: hashing, signing, expiry, and tamper resistance."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth


def test_password_roundtrip_and_rejection():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("", h)


def test_same_password_hashes_differently():
    """Random salt: two hashes of one password must not be equal."""
    a = auth.hash_password("pw")
    b = auth.hash_password("pw")
    assert a != b
    assert auth.verify_password("pw", a) and auth.verify_password("pw", b)


def test_malformed_hash_is_false_not_an_exception():
    for bad in ("", "garbage", "scrypt$x$y$z$q$r", "md5$1$1$1$aa$bb", None):
        assert auth.verify_password("pw", bad) is False


def test_token_valid_then_expired():
    secret = b"k" * 32
    assert auth.valid(auth.issue(secret, ttl=60), secret)
    assert not auth.valid(auth.issue(secret, ttl=-1), secret)


def test_token_rejects_tampering_and_wrong_key():
    """Tokens are '<user>.<expiry>.<signature>' — the signature covers the
    user and the expiry together, so neither can be edited on its own."""
    secret, other = b"k" * 32, b"j" * 32
    tok = auth.issue(secret, "saimai", ttl=60)
    assert not auth.valid(tok, other), "signed with a different key"
    user, exp, sig = tok.split(".", 2)
    forged = f"{user}.{int(exp) + 99999}.{sig}"   # extend your own expiry
    assert not auth.valid(forged, secret), "expiry is inside the signature"
    import base64
    other_user = base64.urlsafe_b64encode(b"root").decode().rstrip("=")
    assert not auth.valid(f"{other_user}.{exp}.{sig}", secret), \
        "the username is inside the signature too"
    for junk in ("", "abc", "1.2.3", "9999999999.zzzz", None):
        assert auth.valid(junk, secret) is False


def test_secret_persists_and_is_private():
    root = tempfile.mkdtemp()
    s1 = auth.load_secret(root)
    s2 = auth.load_secret(root)
    assert s1 == s2 and len(s1) >= 32, "must be stable across calls"
    mode = os.stat(os.path.join(root, ".session_secret")).st_mode & 0o777
    assert mode == 0o600, f"secret must be private, got {oct(mode)}"


def test_sessions_survive_restart_but_not_a_new_secret():
    root = tempfile.mkdtemp()
    tok = auth.issue(auth.load_secret(root), ttl=60)
    assert auth.valid(tok, auth.load_secret(root)), "restart keeps sessions"
    os.remove(os.path.join(root, ".session_secret"))
    assert not auth.valid(tok, auth.load_secret(root)), "new key ends sessions"




def test_basic_header_accepted_by_middleware():
    """Machine clients (the Zotero plugin) authenticate with HTTP Basic
    against the same password; browsers use the session cookie."""
    import base64, tempfile
    from pathlib import Path
    from fastapi.testclient import TestClient
    import server
    lib = server.Library(Path(tempfile.mkdtemp()))
    w = server.Worker(lib, "af_heart", 1.0, 150)
    app = server.create_app(lib, w, {"password_hash": auth.hash_password("pw")})
    c = TestClient(app)
    hdr = {"Authorization": "Basic " + base64.b64encode(b"any:pw").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"any:nope").decode()}
    assert c.get("/api/library").status_code == 401
    assert c.get("/api/library", headers=bad).status_code == 401
    assert c.get("/api/library", headers=hdr).status_code == 200
    assert c.get("/api/library", headers=hdr).status_code == 200  # cached path


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all auth tests passed")
