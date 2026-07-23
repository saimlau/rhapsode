# tests/test_gen_key.py
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import secretbox
from config import DEFAULTS


def test_defaults_have_a_secrets_section():
    assert DEFAULTS["secrets"] == {"key": ""}


def test_gen_key_prints_a_loadable_key():
    out = subprocess.check_output(
        [sys.executable, os.path.join(ROOT, "rhapsode.py"), "--gen-key"],
        text=True).strip()
    # the printed key round-trips through load_key (32 bytes)
    assert len(secretbox.load_key(out)) == 32
