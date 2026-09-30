from src.hashing import fnv1_32, fnv1_64, fnv64_feed
from src.model import Kind, NameMatch

_PROGRESS_EVERY_NAMES = 2000


# Syncs and named objects are tried only for names nothing else claimed.
def match_names(names, index, progress=None):
    matches = []
    unmatched = []
    event_ids = set(index.event_actions) | set(index.dialogue_children)
    total = len(names)
    for i, name in enumerate(names):
        h32 = fnv1_32(name)
        h64 = fnv1_64(name)
        found = False
        if h32 in event_ids:
            kind = Kind.EVENT if h32 in index.event_actions else Kind.DIALOGUE_EVENT
            matches.append(NameMatch(name, kind, sorted(index.wems_for_event(h32)), h32))
            found = True
        if h32 in index.bank_ids:
            matches.append(NameMatch(name, Kind.BANK, [], h32))
            found = True
        if h32 in index.wem_locations:
            matches.append(NameMatch(name, Kind.DIRECT_WEM, [h32], h32))
            found = True
        external_id = _external_id(name, h64, index)
        if external_id is not None:
            matches.append(NameMatch(name, Kind.EXTERNAL, [external_id], external_id))
            found = True
        if not found:
            kind = index.sync_ids.get(h32) or index.named_objects.get(h32)
            if kind:
                matches.append(NameMatch(name, kind, [], h32))
                found = True
        if not found:
            unmatched.append(name)
        if progress and i % _PROGRESS_EVERY_NAMES == 0:
            progress(i, total, "Matching names...")
    return matches, unmatched


# An exported voice name is its path without the extension, so its id is the hash of path + ".wem".
def _external_id(name, h64, index):
    if h64 in index.external_locations:
        return h64
    if name.lower().endswith(".wem"):
        return None
    with_extension = fnv64_feed(b".wem", h64)
    return with_extension if with_extension in index.external_locations else None


# Online id tables name objects whose id is not a name hash, like GI's music segments.
def match_online_labels(id_names, index):
    out = []
    for key, entry in id_names.items():
        try:
            oid = int(key)
        except (TypeError, ValueError):
            continue
        if oid not in index.event_actions and oid not in index.object_ids:
            continue
        name, category = entry if isinstance(entry, list) and len(entry) == 2 else (entry, "Audio")
        if isinstance(name, str) and name and isinstance(category, str):
            out.append(NameMatch(name, category, index.wems_for_object(oid), oid))
    return out


# State rows get the wems their id selects, so searching a state lands on a playable row.
def attach_sync_wems(index, matches):
    if not index.sync_wems:
        return
    for m in matches:
        if not m.wem_ids and m.kind in Kind.SYNCS and m.hash_id in index.sync_wems:
            m.wem_ids = sorted(index.sync_wems[m.hash_id])


# The first row of a (kind, id) wins, so the order the pipeline appends in decides the name kept.
def dedupe_matches(matches):
    seen = set()
    unique = []
    for m in matches:
        key = (m.kind, m.hash_id)
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def prune_unmatched(matches, unmatched):
    resolved = {m.name for m in matches}
    return [name for name in unmatched if name not in resolved]
