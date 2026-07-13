"""config.toml loading for paper2audio.

Precedence: CLI flags > config.toml (repo root, gitignored) > DEFAULTS.
"""

import tomllib
from pathlib import Path

DEFAULTS = {
    "library": {"path": "~/PaperAudio"},
    "tts": {"voice": "af_heart", "speed": 1.0},
    "render": {"dpi": 150},
    "gui": {"port": 7717},
    "grobid": {"enabled": True, "url": "http://127.0.0.1:8070",
               "autostart": True, "home": None},
}


def load_config():
    """DEFAULTS overlaid with config.toml where present."""
    cfg = {section: dict(values) for section, values in DEFAULTS.items()}
    path = Path(__file__).resolve().parent / "config.toml"
    if path.is_file():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            if section in cfg and isinstance(values, dict):
                cfg[section].update(values)
    return cfg


def library_path(cfg):
    return Path(cfg["library"]["path"]).expanduser()
