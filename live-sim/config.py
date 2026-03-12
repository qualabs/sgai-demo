"""
config.py — Layered configuration loader.

Priority (highest → lowest):
  1. Environment variables (including anything loaded from .env)
  2. config.yaml keys
  3. Default values passed to get()

Usage:
    import config
    config.load()
    config.get("SERVER_PORT", 8000)
    config.get_int("SERVER_PORT", 8000)
    config.get_bool("DEBUG", False)
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

_cfg: dict = {}
_BASE_DIR = Path(__file__).parent


def load(env_file=None, yaml_file=None) -> None:
    """Load .env into os.environ, then load config.yaml. Safe to call multiple times."""
    global _cfg

    # 1. Load .env → os.environ (override=False so real env vars win)
    load_dotenv(dotenv_path=env_file or _BASE_DIR / ".env", override=False)

    # 2. Load YAML into _cfg (lowest priority)
    path = yaml_file or _BASE_DIR / "config.yaml"
    _cfg = {}
    if Path(path).exists():
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
            if isinstance(raw, dict):
                _cfg = {str(k): v for k, v in raw.items()}


def get(key: str, default=None):
    """Return value for key. os.environ takes precedence over config.yaml."""
    if key in os.environ:
        return os.environ[key]
    return _cfg.get(key, default)


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get(key, default))
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    val = get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return bool(val)
    return str(val).lower() in ("1", "true", "yes", "on")
