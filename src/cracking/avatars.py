# Unnamed events cracked around the codenames their names spell, like HSR's ev_vo_avatar_<action>_<avatar>.
# A family several avatars share is refilled with every codename, and the edits linking named events are replayed.

from collections import Counter, defaultdict

from src.hashing import FNV32_OFFSET, fnv1_32, fnv32_feed
from src.model import Kind, NameMatch

_ROUNDS = 3
# Measured against decoy ids: with these floors no rule hit a decoy on HSR or GI, and one run in three on ZZZ.
_FAMILY_MIN_CODENAMES = 4
_LEARNED_CODENAME_MIN_FAMILIES = 3
_LEARNED_CODENAME_MINLEN = 3
_EDIT_MIN_PAIRS = 10
_EDIT_MAX = 12
_EDIT_MIN_HITS = 10
# Some battle lines number their takes before the codename: ev_vo_avatar_skill_maze_01_danhengil.
_TAKES_BEFORE_CODENAME = ("01", "02", "03")


# codenames are those of the roster and of the event slots, and the strong families teach more of them.
def crack_avatar_events(index, matches, codenames, progress=None):
    known = {m.hash_id: m.name.lower() for m in matches if m.kind in Kind.EVENTS}
    unresolved = (set(index.event_actions) | set(index.dialogue_children)) - set(known)
    codenames = set(codenames)
    if not unresolved or not codenames:
        return []
    names = set(known.values())
    found = {}
    for round_number in range(_ROUNDS):
        if progress:
            progress(round_number, _ROUNDS, f"Cracking avatar events ({len(found)} found)...")
        codenames |= _learned_codenames(names, codenames, _families(names, codenames))
        hits = _replay_edits(names, unresolved, codenames)
        hits.update(_refill_families(_families(names, codenames), sorted(codenames), unresolved))
        if not hits:
            break
        for hashed, name in hits.items():
            unresolved.discard(hashed)
            found[hashed] = name
            names.add(name)
    out = []
    for hashed, name in sorted(found.items()):
        kind = Kind.EVENT if hashed in index.event_actions else Kind.DIALOGUE_EVENT
        out.append(NameMatch(name, kind, sorted(index.wems_for_event(hashed)), hashed))
    return out


# (head, tail) -> the codenames seen between them; a name splits once per codename it spells.
def _families(names, codenames):
    families = defaultdict(set)
    for name in names:
        words = name.split("_")
        for i, word in enumerate(words):
            if word in codenames:
                families[("_".join(words[:i]), "_".join(words[i + 1:]))].add(word)
    return families


# A word filling the codename slot of several strong families is a codename too, like HSR's playerboy.
def _learned_codenames(names, codenames, families):
    strong = {key for key, members in families.items() if len(members) >= _FAMILY_MIN_CODENAMES}
    seen = Counter()
    for name in names:
        words = name.split("_")
        for i, word in enumerate(words):
            if (word not in codenames and len(word) >= _LEARNED_CODENAME_MINLEN and not word.isdigit()
                    and ("_".join(words[:i]), "_".join(words[i + 1:])) in strong):
                seen[word] += 1
    return {word for word, n in seen.items() if n >= _LEARNED_CODENAME_MIN_FAMILIES}


def _refill_families(families, codenames, unresolved):
    hits = {}
    for (head, tail), members in sorted(families.items()):
        if len(members) < _FAMILY_MIN_CODENAMES:
            continue
        state = fnv32_feed(f"{head}_".encode()) if head else FNV32_OFFSET
        suffix = f"_{tail}".encode() if tail else b""
        for codename in codenames:
            if codename in members:
                continue
            hashed = fnv32_feed(codename.encode() + suffix, state)
            if hashed in unresolved and hashed not in hits:
                hits[hashed] = "_".join(part for part in (head, codename, tail) if part)
    return hits


# Two named events one word apart, like ev_archive_vo_avatar_die_kafka and ev_vo_avatar_die_kafka, teach an edit.
# Each edit is replayed both ways on every take variant, and kept only while it keeps hitting.
def _replay_edits(names, unresolved, codenames):
    edits = _learned_edits(names)
    hits_by_edit = defaultdict(dict)
    for name in sorted(names):
        for i, words in enumerate(_take_variants(name.split("_"), codenames)):
            variants = [(None, words)] if i else []
            for edit in edits:
                edited = _edited(words, edit)
                if edited is not None:
                    variants.append((edit, edited))
            for edit, variant in variants:
                candidate = "_".join(variant)
                hashed = fnv1_32(candidate)
                if hashed in unresolved:
                    hits_by_edit[edit].setdefault(hashed, candidate)
    hits = {}
    for edit_hits in hits_by_edit.values():
        if len(edit_hits) >= _EDIT_MIN_HITS:
            for hashed, candidate in edit_hits.items():
                hits.setdefault(hashed, candidate)
    return hits


# The name as it is, then without its take number, or with one inserted before a closing codename.
def _take_variants(words, codenames):
    variants = [words]
    if len(words) > 2 and words[-1].isdigit():
        variants.append(words[:-1])
    elif len(words) > 2 and words[-1] in codenames:
        variants.extend(words[:-1] + [take, words[-1]] for take in _TAKES_BEFORE_CODENAME)
    return variants


# The edit's word deleted where it stands, else inserted there; None past the end of the name.
def _edited(words, edit):
    word, position = edit
    if position < len(words) and words[position] == word:
        return words[:position] + words[position + 1:]
    if position <= len(words):
        return words[:position] + [word] + words[position:]
    return None


# (word, position) pairs that delete one word of a named event into another named event.
def _learned_edits(names):
    pairs = Counter()
    for name in names:
        words = name.split("_")
        for i, word in enumerate(words):
            if not word.isdigit() and "_".join(words[:i] + words[i + 1:]) in names:
                pairs[(word, i)] += 1
    ranked = sorted((edit for edit, n in pairs.items() if n >= _EDIT_MIN_PAIRS),
                    key=lambda edit: (-pairs[edit], edit))
    return ranked[:_EDIT_MAX]
