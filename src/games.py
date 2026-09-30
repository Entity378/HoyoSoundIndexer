import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GameProfile:
    label: str
    blk_format: str
    blocks_folder: tuple
    folder_keywords: tuple


GAME_PROFILES = {
    "ZZZ": GameProfile("Zenless", "mhy0/mhy1",
                       ("ZenlessZoneZero_Data", "StreamingAssets", "Blocks"),
                       ("zenlesszonezero", "zenless", "zzz")),
    "GI": GameProfile("Genshin", "Blb3",
                      ("GenshinImpact_Data", "StreamingAssets", "AssetBundles", "blocks"),
                      ("genshin", "yuanshen")),
    "SR": GameProfile("Star Rail", "mr0k",
                      ("StarRail_Data", "StreamingAssets", "Asb", "Windows"),
                      ("starrail", "star rail", "hkrpg", "honkai")),
}
GAME_ORDER = ("ZZZ", "GI", "SR")
DEFAULT_GAME = "ZZZ"
# ZZZ keeps its config tables in a few data blocks whose strings name the music states.
ZZZ_DATA_BLOCK_DIRS = (
    ("ZenlessZoneZero_Data", "Persistent", "Blocks", "Data"),
    ("Persistent", "Blocks", "Data"),
)
_LAUNCHER_GAME_DIRS = (("HoYoPlay", "games"), ("miHoYo Launcher", "games"))
_SIBLING_SEARCH_DEPTH = 5


def detect_game_from_path(text):
    low = (text or "").lower()
    for game in GAME_ORDER:
        if any(keyword in low for keyword in GAME_PROFILES[game].folder_keywords):
            return game
    return None


def _install_in(directory, keywords):
    try:
        for child in directory.iterdir():
            if child.is_dir() and any(keyword in child.name.lower() for keyword in keywords):
                return child
    except Exception:
        pass
    return None


# The games sit side by side under one launcher, so the parents of a known folder come first.
def locate_game_folder(game, near=""):
    keywords = GAME_PROFILES[game].folder_keywords
    for parent in list(Path(near or ".").parents)[:_SIBLING_SEARCH_DEPTH]:
        found = _install_in(parent, keywords)
        if found is not None:
            return str(found)
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if not base:
            continue
        for parts in _LAUNCHER_GAME_DIRS:
            root = Path(base).joinpath(*parts)
            if root.is_dir():
                found = _install_in(root, keywords)
                if found is not None:
                    return str(found)
    return ""


def blocks_folder(install_root, game):
    candidate = Path(install_root or ".").joinpath(*GAME_PROFILES[game].blocks_folder)
    return candidate if candidate.is_dir() else None


PERSISTENT_PRIORITY_PCKS = {"patch.pck", "hotfix.pck"}


# Downloaded and hotfix content lands in the Persistent twin of a StreamingAssets folder.
def persistent_twins(root):
    parts = list(Path(root).parts)
    lowered = [part.lower() for part in parts]
    if "streamingassets" in lowered:
        i = lowered.index("streamingassets")
        candidate = Path(*parts[:i], "Persistent", *parts[i + 1:])
        if candidate.is_dir():
            return [candidate]
    return []


# A pck found in both roots is read from Persistent only for Patch and Hotfix.
def dedupe_pck_files(paths):
    groups = {}
    for path in paths:
        parts = [part.lower() for part in path.parts]
        marker, key = None, str(path).lower()
        for root_name in ("streamingassets", "persistent"):
            if root_name in parts:
                marker, key = root_name, "/".join(parts[parts.index(root_name) + 1:])
                break
        groups.setdefault(key, []).append((marker, path))
    kept, dropped = [], 0
    for candidates in groups.values():
        if len(candidates) > 1:
            name = candidates[0][1].name.lower()
            preferred = "persistent" if name in PERSISTENT_PRIORITY_PCKS else "streamingassets"
            chosen = [path for marker, path in candidates if marker == preferred]
            kept.append((chosen or [path for _marker, path in candidates])[0])
            dropped += len(candidates) - 1
        else:
            kept.append(candidates[0][1])
    return sorted(kept), dropped
