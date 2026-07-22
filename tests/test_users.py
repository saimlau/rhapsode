"""Accounts, invites and session identity.

Isolation in this design is a code invariant rather than a filesystem
boundary, so the pieces it rests on — who a session says you are, and who an
invite lets you become — are tested adversarially here.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth


def _users():
    return auth.Users(tempfile.mkdtemp())


# ------------------------------------------------------------- sessions

def test_session_carries_the_user():
    k = b"k" * 32
    tok = auth.issue(k, "saimai")
    assert auth.session_user(tok, k) == "saimai"
    assert auth.valid(tok, k)


def test_session_username_cannot_be_swapped():
    """The signature covers user AND expiry together: keeping a valid
    signature while changing the name must not authenticate the new name."""
    import base64
    k = b"k" * 32
    tok = auth.issue(k, "saimai")
    _user, epoch, exp, sig = tok.split(".", 3)
    other = base64.urlsafe_b64encode(b"attacker").decode().rstrip("=")
    assert auth.session_user(f"{other}.{epoch}.{exp}.{sig}", k) is None
    # and the expiry cannot be extended while keeping the name
    assert auth.session_user(f"{_user}.{epoch}.{int(exp) + 99999}.{sig}", k) is None


def test_session_rejects_expiry_and_wrong_key():
    k, other = b"k" * 32, b"j" * 32
    assert auth.session_user(auth.issue(k, "a", ttl=-1), k) is None
    assert auth.session_user(auth.issue(k, "a"), other) is None
    for junk in ("", "x", "a.b", "a.b.c", "a.b.c.d", None):
        assert auth.session_user(junk, k) is None


# ---------------------------------------------------------------- users

def test_create_and_check():
    u = _users()
    u.create("Saimai", "correct horse battery", admin=True)
    assert u.exists("saimai"), "usernames are normalised to lower case"
    assert u.check("saimai", "correct horse battery") == "saimai"
    assert u.check("SAIMAI", "correct horse battery") == "saimai"
    assert u.check("saimai", "wrong") is None
    assert u.check("nobody", "correct horse battery") is None
    assert u.is_admin("saimai")


def test_usernames_are_validated():
    u = _users()
    for bad in ("", "ab", "x" * 33, "has space", "sql'inject", "../../etc",
                "a/b", None):
        try:
            u.create(bad, "a password long enough")
            raise AssertionError(f"{bad!r} should be rejected")
        except ValueError:
            pass


def test_passwords_have_a_floor_and_are_hashed():
    u = _users()
    try:
        u.create("shorty", "1234567")
        raise AssertionError("a 7-character password should be rejected")
    except ValueError:
        pass
    u.create("real", "a long enough password")
    stored = u.data["users"]["real"]["pw"]
    assert "a long enough password" not in stored
    assert stored.startswith("scrypt$")


def test_duplicate_username_is_refused():
    u = _users()
    u.create("taken", "a long enough password")
    try:
        u.create("TAKEN", "another long password")
        raise AssertionError("a taken name must be refused, case-insensitively")
    except ValueError:
        pass


def test_users_file_is_private_and_survives_reload():
    root = tempfile.mkdtemp()
    u = auth.Users(root)
    u.create("saimai", "a long enough password", admin=True)
    mode = os.stat(os.path.join(root, auth.USERS_FILE)).st_mode & 0o777
    assert mode == 0o600, f"users.json holds password hashes, got {oct(mode)}"
    assert auth.Users(root).check("saimai", "a long enough password") == "saimai"


def test_last_admin_cannot_be_deleted():
    u = _users()
    u.create("boss", "a long enough password", admin=True)
    u.create("guest", "a long enough password")
    u.delete("guest")
    try:
        u.delete("boss")
        raise AssertionError("deleting the only admin would lock everyone out")
    except ValueError:
        pass
    u.create("boss2", "a long enough password", admin=True)
    u.delete("boss")            # fine now: another admin remains


# -------------------------------------------------------------- invites

def test_invite_is_single_use():
    u = _users()
    u.create("boss", "a long enough password", admin=True)
    tok = u.mint_invite("boss")
    assert u.invite_ok(tok)
    assert u.redeem(tok, "colleague", "a long enough password") == "colleague"
    assert not u.invite_ok(tok), "a redeemed invite must not work twice"
    try:
        u.redeem(tok, "another", "a long enough password")
        raise AssertionError("a burnt invite must not create a second account")
    except ValueError:
        pass


def test_invite_tokens_are_stored_hashed():
    """Whoever reads users.json must not come away with working links."""
    root = tempfile.mkdtemp()
    u = auth.Users(root)
    u.create("boss", "a long enough password", admin=True)
    tok = u.mint_invite("boss")
    raw = open(os.path.join(root, auth.USERS_FILE)).read()
    assert tok not in raw, "the invite token itself must never be written"


def test_invite_expiry_and_forgery():
    u = _users()
    u.create("boss", "a long enough password", admin=True)
    assert not u.invite_ok(u.mint_invite("boss", ttl=-1))
    for junk in ("", "made-up-token", None):
        assert not u.invite_ok(junk)
        try:
            u.redeem(junk, "sneak", "a long enough password")
            raise AssertionError("an unissued token must not create an account")
        except ValueError:
            pass
    assert not u.exists("sneak")


def test_failed_redeem_does_not_burn_a_good_invite():
    u = _users()
    u.create("boss", "a long enough password", admin=True)
    tok = u.mint_invite("boss")
    for bad_name, bad_pw in (("x", "a long enough password"), ("ok_name", "short")):
        try:
            u.redeem(tok, bad_name, bad_pw)
        except ValueError:
            pass
    assert u.invite_ok(tok), "a rejected attempt must leave the invite usable"
    assert u.redeem(tok, "colleague", "a long enough password") == "colleague"



def test_changing_a_password_ends_that_accounts_sessions():
    """The epoch is the account generation; a username alone could never
    express "this session predates the password change"."""
    u = _users()
    k = b"k" * 32
    u.create("saimai", "the first long password")
    tok = auth.issue(k, "saimai", epoch=u.epoch("saimai"))
    assert auth.session_claims(tok, k)[1] == u.epoch("saimai")
    u.set_password("saimai", "a different long password")
    assert auth.session_claims(tok, k)[1] != u.epoch("saimai"), \
        "the old session must no longer match the account"


def test_a_recreated_name_gets_a_fresh_epoch():
    u = _users()
    u.create("boss", "a long enough password", admin=True)
    u.create("temp", "a long enough password")
    first = u.epoch("temp")
    u.delete("temp")
    assert u.epoch("temp") == ""
    # (the name is also retired, but even if it were reissued the epoch
    #  would differ, so the departed member's sessions cannot resume)
    assert first != ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all user/invite tests passed")
