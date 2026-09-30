import json
import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "Hoyo Sound Indexer"


# Read at call time, so a test run can point LOCALAPPDATA somewhere else.
def config_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HoyoSoundIndexer"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "HoyoSoundIndexer"


def config_file():
    return config_dir() / "config.json"


def cache_file(kind, game):
    return config_dir() / "cache" / f"{kind}_{game}.json"


def online_cache_file(game):
    return cache_file("voice", game)


# None when missing or unreadable, which every caller treats as no cache.
def read_cache(kind, game):
    try:
        doc = json.loads(cache_file(kind, game).read_text(encoding="utf-8"))
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


# Written aside and swapped in, so a crash mid-write never leaves half a cache behind.
def write_cache(kind, game, doc):
    path = cache_file(kind, game)
    temp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError:
        return False
    return True


def tools_dir():
    return config_dir() / "tools"


def load_config():
    try:
        config = json.loads(config_file().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return config if isinstance(config, dict) else {}


def save_config(config):
    try:
        path = config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception:
        pass


def hidden_window_kwargs():
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startup}
