# ZZZ plays nearly all of its BGM through one event and picks the track with states.
# So each music-switch leaf becomes a row named after the states on its path, outermost first.

from src.model import Kind, NameMatch
from src.vocabulary import FILLER_STATE_NAMES


# Nested switches compose their paths, and leaves with the same label merge into one row.
def music_branch_matches(index, matches):
    trees = index.music_trees
    if not trees:
        return []
    needed = set()
    for tree in trees.values():
        for keys, _target in tree.leaves:
            needed.update(key for key in keys if key)
    # The first name of an id is the one the deduped list keeps, so labels spell states like their rows.
    key_names = {}
    for m in matches:
        if m.hash_id in needed:
            key_names.setdefault(m.hash_id, m.name)
    if not key_names:
        return []
    per_target = {}

    def expand(oid, prefix, seen):
        for keys, target in trees[oid].leaves:
            path = prefix + list(keys)
            if target in trees and target not in seen:
                expand(target, path, seen | {target})
            elif target:
                _label_target(index, per_target, target, path, key_names)

    leaf_targets = {target for tree in trees.values() for _keys, target in tree.leaves}
    for oid in trees:
        if oid not in leaf_targets:
            expand(oid, [], {oid})
    rows = {}
    for target, (label, _named, wems) in sorted(per_target.items()):
        row = rows.get(label)
        if row is None:
            rows[label] = [target, set(wems)]
        else:
            row[1] |= wems
    out = [NameMatch(label, Kind.MUSIC_BRANCH, sorted(wems), target)
           for label, (target, wems) in rows.items()]
    out.sort(key=lambda m: m.name.lower())
    return out


# Default and boolean states only pad the path, so they go unless nothing else names it.
# The same node reached along two paths keeps its most specific label.
def _label_target(index, per_target, target, path, key_names):
    resolved = [key_names[key] for key in path if key in key_names]
    resolved = [name for name in resolved if name.lower() not in FILLER_STATE_NAMES] or resolved
    if not resolved:
        return
    wems = index.wems_under(target)
    if not wems:
        return
    label = " / ".join(resolved)
    row = per_target.get(target)
    if row is None:
        per_target[target] = [label, len(resolved), wems]
        return
    if len(resolved) > row[1]:
        row[0], row[1] = label, len(resolved)
    row[2] |= wems
