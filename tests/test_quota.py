# tests/test_quota.py
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import server

BLOCK_MSG = ("Over your shared-compute quota. Attach your own Modal in "
             "Settings, or ask an admin to raise your cap.")


def _worker():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("bob", "bob's long password")
    w = server.Worker(lib, "af_heart", 1.0, 150,
                      tts_cfg={"backend": "modal"}, users=users, secret_key=None)
    return lib, users, w


def test_over_cap_blocks_an_operator_job():
    lib, users, w = _worker()
    # bob already has 3.0 operator hours on the shelf, cap is 3.0
    lib.data["papers"]["done"] = {"id": "done", "owner": "bob",
                                  "billed": "operator", "duration": 10800,
                                  "status": "ready"}
    lib.data["order"].append("done")
    lib.save()
    users.set_quota("bob", "tts_hours", 3.0)
    assert w._quota_block("bob", "operator") == BLOCK_MSG


def test_under_cap_does_not_block():
    lib, users, w = _worker()
    users.set_quota("bob", "tts_hours", 3.0)
    assert w._quota_block("bob", "operator") is None


def test_self_billed_is_never_blocked():
    lib, users, w = _worker()
    lib.data["papers"]["done"] = {"id": "done", "owner": "bob",
                                  "billed": "operator", "duration": 10800,
                                  "status": "ready"}
    lib.data["order"].append("done")
    lib.save()
    users.set_quota("bob", "tts_hours", 3.0)
    assert w._quota_block("bob", "self") is None


def test_no_cap_never_blocks():
    lib, users, w = _worker()
    lib.data["papers"]["done"] = {"id": "done", "owner": "bob",
                                  "billed": "operator", "duration": 999999,
                                  "status": "ready"}
    lib.data["order"].append("done")
    lib.save()
    assert w._quota_block("bob", "operator") is None
