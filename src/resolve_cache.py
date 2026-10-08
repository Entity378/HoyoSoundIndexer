# The last cracked resolve of each game, restored by id in a second or two instead of one or two minutes.
# Its key covers the game's ids, the name sources and the tool's own code, so any change cracks again.

import hashlib
import sys
from array import array
from datetime import datetime
from pathlib import Path

from src.config import read_cache, write_cache
from src.names_io import ExportedNames
from src.pipeline import resolve_all_matches

CACHE_KIND = "names"
# Bumped when the saved layout changes.
_FORMAT = 1


# The saved rows keep the resolve's own order, which the restore gives back as it was.
class SavedResolve(ExportedNames):
    def entries(self):
        for row in self.doc.get("rows", []):
            if isinstance(row, list) and len(row) == 3:
                yield row[0], row[1], row[2]

    def saved_on(self):
        return self.doc.get("saved", "")


def resolve_key(index, names, names_file, online, harvested_voice):
    digest = hashlib.sha256()
    for ids in (index.bank_ids, index.object_ids, index.sync_ids, index.wem_locations,
                index.external_locations):
        digest.update(array("Q", sorted(ids)).tobytes())
    _feed(digest, "names", names)
    if names_file:
        stat = Path(names_file).stat()
        _feed(digest, "names file", [Path(names_file).resolve(), stat.st_size, stat.st_mtime_ns])
    if online is not None:
        _feed(digest, "voice paths", online.voice_paths)
        _feed(digest, "labels", [f"{oid}={name}" for oid, name in sorted(online.id_names.items())])
        _feed(digest, "state candidates", online.state_candidates)
        _feed(digest, "roster", [f"{code}={name}" for code, name in sorted(online.roster.items())])
    if harvested_voice is not None:
        _feed(digest, "voice prefixes", harvested_voice.prefixes)
        _feed(digest, "voice sources", harvested_voice.sources)
    _feed(digest, "code", _code_stamp())
    return digest.hexdigest()


# Each list goes in with its label and length, so moving a name from one list to another changes the key.
def _feed(digest, label, values):
    digest.update(f"\0{label}\0{len(values)}\0".encode())
    digest.update("\n".join(map(str, values)).encode("utf-8", "surrogatepass"))


# A new build may crack differently: the exe itself when frozen, else every module outside the GUI.
def _code_stamp():
    if getattr(sys, "frozen", False):
        stat = Path(sys.executable).stat()
        return [stat.st_size, stat.st_mtime_ns]
    root = Path(__file__).resolve().parent
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(root.rglob("*.py"))
            if "gui" not in path.relative_to(root).parts]


def load_saved_resolve(game, key):
    doc = read_cache(CACHE_KIND, game)
    if doc is None or doc.get("format") != _FORMAT or doc.get("key") != key:
        return None
    return SavedResolve(doc)


def save_resolve(game, key, result, index):
    return write_cache(CACHE_KIND, game, {
        "format": _FORMAT,
        "key": key,
        "saved": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "avatar_names": dict(sorted(index.character_names.items())),
        "avatar_codes": dict(sorted(index.avatar_codenames.items())),
        "unmatched": list(result.unmatched),
        "rows": [[m.name, m.kind, m.hash_id] for m in result.matches],
    })


# The unmatched names go through the match again, so they stay listed for the export.
def restore_resolve(index, saved, scan_root, progress=None, cancel=None):
    unmatched = [name for name in saved.doc.get("unmatched", []) if isinstance(name, str)]
    return resolve_all_matches(index, unmatched, scan_root, export=saved, progress=progress, cancel=cancel,
                               crack=False)
