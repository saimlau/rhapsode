import os
import sys
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


def test_run_accepts_secrets_cfg():
    assert "secrets_cfg" in inspect.signature(server.run).parameters


def test_create_app_accepts_secret_key():
    assert "secret_key" in inspect.signature(server.create_app).parameters
