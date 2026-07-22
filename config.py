"""config.toml loading for Rhapsode.

Precedence: CLI flags > config.toml (repo root, gitignored) > DEFAULTS.
"""

from pathlib import Path

# tomllib is stdlib from Python 3.11. On 3.9/3.10 the identical parser is
# available as the `tomli` package — the rest of Rhapsode uses no 3.11-only
# syntax, so falling back keeps those interpreters working rather than
# failing with a bare "No module named 'tomllib'" at the first run.
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        import sys
        v = ".".join(str(n) for n in sys.version_info[:3])
        raise SystemExit(
            f"error: reading config.toml needs Python 3.11+ (you have {v}).\n"
            f"  Either use a newer Python, or install the backport into this\n"
            f"  environment:  {sys.executable} -m pip install tomli") from None

DEFAULTS = {
    "library": {"path": "~/PaperAudio"},
    "tts": {"voice": "af_heart", "speed": 1.0, "m4a_bitrate": "48k",
            "park_after_s": 300, "unload_after_s": 1800,
            "backend": "local", "modal_endpoint": "",
            "modal_token_id": "", "modal_token_secret": ""},
    "render": {"dpi": 150},
    "llm": {"enabled": False, "runner": "auto",
            "model": "", "timeout_s": 120, "ollama_url": "",
            "ollama_keep_alive": "30m", "ollama_num_ctx": 16384,
            "ollama_think": False, "ollama_window_chars": 15000,
            "window_chars": 100000,
            "api_provider": "", "api_key": "", "api_base_url": "",
            "cache": True, "cache_dir": ""},
    "auth": {"password_hash": ""},
    "gui": {"port": 7717, "open": True, "idle_exit_min": 0},
    "grobid": {"enabled": True, "url": "http://127.0.0.1:8070",
               "autostart": True, "home": None, "idle_stop_s": 600},
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
