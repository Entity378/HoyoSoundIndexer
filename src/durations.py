# Wem durations from the header of each wem's biggest copy, cached per game along with the copy's size.
# Reading GI's 430k headers from a cold disk took 45 s, where the cache takes 0.3 s.

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import read_cache, write_cache
from src.wwise.wem import HEADER_BYTES, wem_duration_ms

CACHE_KIND = "durations"
# A cold read waits on the disk, which serves a few parallel reads faster.
_READ_WORKERS = 4
# Bumped when the header parsing changes, so older caches are read again.
_FORMAT = 1


# Wem id -> milliseconds, for the wems whose codec gives one.
def read_durations(index, game, progress=None, cancel=None):
    biggest = _biggest_copies(index)
    cached = _load_cache(game)
    durations, pending = {}, defaultdict(list)
    for wem_id, location in biggest.items():
        entry = cached.get(wem_id)
        if entry is not None and entry[0] == location.size:
            durations[wem_id] = entry[1]
        else:
            pending[location.file_path].append((location.offset, location.size, wem_id))
    if pending:
        durations.update(_read_headers(pending, progress, cancel))
        if not (cancel and cancel()):
            _save_cache(game, biggest, durations)
    return {wem_id: ms for wem_id, ms in durations.items() if ms >= 0}


# The same copy locations_of puts first: the biggest, the earliest on a tie.
def _biggest_copies(index):
    biggest = {}
    for table in (index.wem_locations, index.external_locations):
        for wem_id, locations in table.items():
            for location in locations:
                known = biggest.get(wem_id)
                if known is None or location.size > known.size:
                    biggest[wem_id] = location
    return biggest


# A wem without a duration is kept as -1, so it is not read again on the next scan.
def _read_headers(pending, progress, cancel):
    total = sum(len(entries) for entries in pending.values())

    def read_file(path, entries):
        found = {}
        try:
            with open(path, "rb", buffering=0) as f:
                for offset, size, wem_id in sorted(entries):
                    if cancel and cancel():
                        break
                    f.seek(offset)
                    duration = wem_duration_ms(f.read(min(size, HEADER_BYTES)))
                    found[wem_id] = -1 if duration is None else duration
        except OSError:
            pass
        return found

    durations = {}
    with ThreadPoolExecutor(_READ_WORKERS) as pool:
        futures = [pool.submit(read_file, path, entries) for path, entries in pending.items()]
        for future in as_completed(futures):
            durations.update(future.result())
            if progress:
                progress(len(durations), total, f"Reading wem durations ({len(durations):,} of {total:,})...")
    return durations


# The cache is one flat list of (id, size, ms) triples.
def _load_cache(game):
    doc = read_cache(CACHE_KIND, game)
    if doc is None or doc.get("format") != _FORMAT or not isinstance(doc.get("wems"), list):
        return {}
    flat = doc["wems"]
    return {flat[i]: (flat[i + 1], flat[i + 2]) for i in range(0, len(flat) - 2, 3)}


def _save_cache(game, biggest, durations):
    flat = []
    for wem_id, location in biggest.items():
        duration = durations.get(wem_id)
        if duration is not None:
            flat += (wem_id, location.size, duration)
    write_cache(CACHE_KIND, game, {"format": _FORMAT, "wems": flat})
