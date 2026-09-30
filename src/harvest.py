# Candidate names from the game's own files: blkdec decrypts the .blk asset blocks, the rest is scanned raw.

import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from blkdec import iter_blk_blocks

from src.config import read_cache, write_cache
from src.games import GAME_PROFILES, ZZZ_DATA_BLOCK_DIRS, persistent_twins
from src.vocabulary import DEFAULT_HARVEST_PREFIXES, HARVEST_PREFIXES_BY_GAME
from src.voice.from_harvest import HarvestedVoice
from src.voice.paths import VO_LEAF_RE, VO_PATH_RE, VO_PREFIX_RE

# Raw files are read in 16 MB chunks overlapping by the longest name, keeping each worker's RAM flat.
_HARVEST_CHUNK = 1 << 24
_HARVEST_OVERLAP = 256
_MIN_WORKERS = 2
_MAX_WORKERS = 8
_PROGRESS_EVERY_FILES = 5
# UTF-16 text of ASCII collapses to ASCII once isolated NULs go, so "P\0l\0a\0y" reads "Play".
_ISOLATED_NUL_RE = re.compile(rb"(?<=[\x01-\xff])\x00(?=[\x01-\xff])")
# Any identifier, since state and switch names have no fixed prefix.
IDENTIFIER_RE = re.compile(rb"[A-Za-z][A-Za-z0-9_]{2,79}")
CACHE_KIND = "harvest"


@dataclass
class HarvestResult:
    names: list = field(default_factory=list)
    voice: HarvestedVoice = field(default_factory=HarvestedVoice)
    cancelled: bool = False
    # When the harvest ran, which tells a saved harvest from a fresh one.
    harvested: str = ""


# The last harvest of each game, which takes minutes to redo and changes only with a game update.
# Its voice keys are those of a saved voice data file, so --vo-in reads it as well.
def save_harvest(game, folder, result):
    return write_cache(CACHE_KIND, game, {"harvested": result.harvested, "folder": str(folder),
                                          "names": result.names, **result.voice.to_json()})


def load_saved_harvest(game):
    doc = read_cache(CACHE_KIND, game)
    if doc is None or not isinstance(doc.get("names"), list):
        return None
    return HarvestResult(names=[name for name in doc["names"] if isinstance(name, str)],
                         voice=HarvestedVoice.from_json(doc), harvested=doc.get("harvested", ""))


def default_harvest_prefixes(game):
    return HARVEST_PREFIXES_BY_GAME.get(game, DEFAULT_HARVEST_PREFIXES)


def _harvest_pattern(prefixes):
    alternatives = b"|".join(re.escape(p.strip().encode()) for p in prefixes if p.strip())
    return re.compile(rb"(?<![0-9A-Za-z_])(?:" + alternatives + rb")_[0-9A-Za-z_]{2,120}",
                      re.IGNORECASE)


# A decrypted string is preceded by its int32 length, which cuts the match before the next string.
def _names_from_data(data, pattern):
    found = set()
    for hit in pattern.finditer(data):
        start, stop = hit.start(), hit.end()
        if start >= 4:
            declared = int.from_bytes(data[start - 4:start], "little")
            if 4 <= declared < stop - start:
                stop = start + declared
        found.add(data[start:stop].decode("ascii", "ignore"))
    return found


# The regexes run on lowercased bytes, and each hit is sliced out of the original to keep its casing.
def _voice_data_from(data):
    low = data.lower()
    prefixes = {data[m.start():m.end()].decode("ascii", "ignore") for m in VO_PREFIX_RE.finditer(low)}
    sources = {data[m.start():m.end()].decode("ascii", "ignore") for m in VO_PATH_RE.finditer(low)}
    sources |= {data[m.start():m.end()].decode("ascii", "ignore") for m in VO_LEAF_RE.finditer(low)}
    return prefixes, sources


# Block by block, never joining the payload: one GI file exceeds 160 MB and workers run in parallel.
# None when blkdec does not know the container, so the caller falls back to the raw scan.
def _harvest_blk(path, pattern, game):
    names, prefixes, sources = set(), set(), set()
    seen_any = False
    for block in iter_blk_blocks(path, game):
        if not block:
            continue
        seen_any = True
        data = _ISOLATED_NUL_RE.sub(b"", block)
        names |= _names_from_data(data, pattern)
        block_prefixes, block_sources = _voice_data_from(data)
        prefixes |= block_prefixes
        sources |= block_sources
    if not seen_any:
        return None
    return names, prefixes, sources


def _harvest_file(path, prefixes, game=None):
    pattern = _harvest_pattern(prefixes)
    if game and path.lower().endswith((".blk", ".block")):
        try:
            result = _harvest_blk(path, pattern, game)
            if result is not None:
                return result
        except Exception:
            pass
    found = set()
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(_HARVEST_CHUNK)
                if not chunk:
                    break
                data = tail + chunk
                found.update(hit.group().decode("ascii", "ignore") for hit in pattern.finditer(data))
                tail = data[-_HARVEST_OVERLAP:]
    except Exception:
        pass
    return found, set(), set()


# The Persistent twin is harvested too, or a chunk of the newest event names is missed.
def harvest_folder(folder, prefixes, progress=None, cancel=None, game=None):
    game = game if game in GAME_PROFILES else None
    roots = [Path(folder)] + persistent_twins(folder)
    files = sorted({p for root in roots for p in root.rglob("*") if p.is_file()})
    names, voice_prefixes, voice_sources = set(), set(), set()
    workers = max(_MIN_WORKERS, min(_MAX_WORKERS, (os.cpu_count() or 4) - 1))
    done = 0
    cancelled = False
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_harvest_file, str(p), prefixes, game): p for p in files}
        for future in as_completed(futures):
            if cancel and cancel():
                pool.shutdown(wait=False, cancel_futures=True)
                cancelled = True
                break
            file_names, file_prefixes, file_sources = future.result()
            names |= file_names
            voice_prefixes |= file_prefixes
            voice_sources |= file_sources
            done += 1
            if progress and (done % _PROGRESS_EVERY_FILES == 0 or done == len(files)):
                progress(done, len(files), f"Harvesting ({len(names)} names, {len(voice_sources)} voice)")
    if progress:
        progress(len(files), len(files), "Harvest cancelled" if cancelled else "Harvest done")
    return HarvestResult(sorted(names), HarvestedVoice(sorted(voice_prefixes), sorted(voice_sources)),
                         cancelled, datetime.now().strftime("%Y-%m-%d %H:%M"))


# The il2cpp metadata holds the C# string literals, plaintext on ZZZ, where code-set state names live.
def _metadata_files(root):
    dirs = [root / "il2cpp_data" / "Metadata"]
    try:
        dirs += [d / "il2cpp_data" / "Metadata"
                 for d in root.iterdir() if d.is_dir() and d.name.endswith("_Data")]
    except Exception:
        pass
    files = []
    for directory in dirs:
        if directory.is_dir():
            files += sorted(directory.glob("*.dat"))
    return files


# The candidate pool for music states and switch values, read on every scan with Crack on.
def harvest_local_state_names(scan_root, progress=None, cancel=None):
    root = Path(scan_root)
    seen = set()
    data_blocks_dir = next((root.joinpath(*parts) for parts in ZZZ_DATA_BLOCK_DIRS
                            if root.joinpath(*parts).is_dir()), None)
    blk_files = sorted(data_blocks_dir.glob("*.blk")) if data_blocks_dir else []
    for i, blk in enumerate(blk_files):
        if cancel and cancel():
            break
        if progress:
            progress(i, len(blk_files), f"Reading data blocks for state names ({len(seen)})...")
        try:
            for block in iter_blk_blocks(str(blk), "ZZZ"):
                seen.update(hit.group() for hit in IDENTIFIER_RE.finditer(block))
        except Exception:
            continue
    for dat in _metadata_files(root):
        if cancel and cancel():
            break
        if progress:
            progress(0, 1, f"Reading {dat.name} for state names ({len(seen)})...")
        try:
            seen.update(hit.group() for hit in IDENTIFIER_RE.finditer(dat.read_bytes()))
        except Exception:
            continue
    names = []
    for raw in seen:
        try:
            names.append(raw.decode("ascii"))
        except UnicodeDecodeError:
            pass
    if progress and (blk_files or names):
        progress(1, 1, f"State candidates: {len(names)}")
    return sorted(names)
