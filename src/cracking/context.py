# The last unnamed syncs, cracked from the words of whatever touches them.
# Groups are often named like a touching event minus its verb, or like a sibling with a paired word.

import itertools
from collections import Counter, defaultdict

from src.hashing import fnv1_32
from src.model import Kind, NameMatch
from src.vocabulary import NAME_PAIRS, NAME_SKIP_TOKENS, NAME_VERBS

_TOKEN_LEN = (2, 14)
# Only the first context tokens are combined: alone, in pairs, in triples and with the commonest words.
_CONTEXT_TOKENS = 25
_TRIPLE_TOKENS = 14
_GLOBAL_PAIR_TOKENS = 12
_GLOBAL_TOKENS = 500
_GLOBAL_TOKEN_LEN = (2, 12)


# Two rounds, so what the first one names becomes context for the second.
def crack_context_names(index, matches):
    name_of = {m.hash_id: m.name for m in matches}
    wanted = {sid for sid in index.sync_ids if sid not in name_of}
    if not wanted:
        return []
    events_of, pair_of = _action_context(index)
    siblings = _selector_siblings(index)
    low, high = _GLOBAL_TOKEN_LEN
    global_tokens = Counter(token for name in name_of.values() for token in name.lower().split("_")
                            if low <= len(token) <= high)
    top_global = [token for token, _count in global_tokens.most_common(_GLOBAL_TOKENS)]

    out = []
    for _round in range(2):
        for match in out:
            name_of.setdefault(match.hash_id, match.name)
        wanted -= {match.hash_id for match in out}
        for sid in list(wanted):
            context_names = [name_of[eid] for eid in events_of.get(sid, ()) if eid in name_of]
            related = pair_of.get(sid, set()) | siblings.get(sid, set())
            # Two hops reach the sibling values of the same group.
            for other in list(related):
                related |= pair_of.get(other, set())
            context_names += [name_of[other] for other in related if other != sid and other in name_of]
            if not context_names:
                continue
            for candidate in _candidates(context_names, top_global):
                if fnv1_32(candidate) == sid:
                    out.append(NameMatch(candidate, index.sync_ids.get(sid, Kind.STATE), [], sid))
                    break
    return out


# Sync id -> the events touching it, and sync id -> the other syncs the same action touches.
def _action_context(index):
    action_events = defaultdict(set)
    for event_id, action_ids in index.event_actions.items():
        for action_id in action_ids:
            action_events[action_id].add(event_id)
    events_of = defaultdict(set)
    pair_of = defaultdict(set)
    for action_id, sync_ids in index.action_syncs.items():
        for sid in sync_ids:
            events_of[sid] |= action_events.get(action_id, set())
            pair_of[sid].update(other for other in sync_ids if other != sid)
    return events_of, pair_of


def _selector_siblings(index):
    siblings = defaultdict(set)
    for trees in (index.music_trees, index.dialogue_trees):
        for tree in trees.values():
            members = {group for group in tree.groups if group}
            for keys, _target in tree.leaves:
                members.update(key for key in keys if key)
            for member in members:
                siblings[member] |= members - {member}
    for container in index.switch_containers.values():
        members = {container.group_id} | {value for value in container.assignments if value}
        for member in members:
            siblings[member] |= members - {member}
    return siblings


def _context_tokens(context_names):
    low, high = _TOKEN_LEN
    tokens = []
    for name in context_names:
        for token in name.lower().split("_"):
            if low <= len(token) <= high and token not in NAME_SKIP_TOKENS and token not in tokens:
                tokens.append(token)
    return tokens[:_CONTEXT_TOKENS]


# Most plausible first, since the first candidate that hits wins.
def _candidates(context_names, top_global):
    tokens = _context_tokens(context_names)
    for name in context_names:
        low = name.lower()
        for verb in NAME_VERBS:
            if low.startswith(verb):
                low = low[len(verb):]
                break
        parts = low.split("_")
        for start in range(len(parts)):
            for stop in range(start + 1, len(parts) + 1):
                yield "_".join(parts[start:stop])
    for name in context_names:
        low = name.lower()
        for half_a, half_b in NAME_PAIRS:
            if low.endswith("_" + half_a) or low == half_a:
                yield low[: -len(half_a)] + half_b if "_" in low else half_b
            if low.endswith("_" + half_b) or low == half_b:
                yield low[: -len(half_b)] + half_a if "_" in low else half_a
    yield from tokens
    for a, b in itertools.permutations(tokens, 2):
        yield a + "_" + b
        yield a + b
    for a, b, c in itertools.permutations(tokens[:_TRIPLE_TOKENS], 3):
        yield a + "_" + b + "_" + c
        yield a + b + c
        yield a + "_" + b + c
    for a in tokens[:_GLOBAL_PAIR_TOKENS]:
        for common in top_global:
            yield a + "_" + common
            yield common + "_" + a
            yield a + common
