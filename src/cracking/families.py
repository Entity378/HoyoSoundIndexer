# Unnamed events cracked from their families, like play_vo_char_<agent>_<action>.
# Each slot is refilled with its family's fillers, and missing suffixes are probed in the candidate pool.

from collections import Counter, defaultdict

from src.hashing import FNV32_OFFSET, fnv32_feed
from src.model import Kind, NameMatch
from src.vocabulary import KNOWN_EVENT_TEMPLATES

_FAMILY_SUFFIX_MAXLEN = 32
# Probing costs one pass over the candidates per prefix and filler, so only the strongest are probed.
_FAMILY_SUFFIX_PREFIXES = 5
_FAMILY_SUFFIX_PROBES = 4
# The strongest family is probed with every filler, since a suffix few agents share is never in a sample.
_FAMILY_SUFFIX_FULL_PREFIXES = 1
_TEMPLATE_TOKENS = (2, 12)
_TEMPLATE_TOKEN_MAXLEN = 24
_TEMPLATE_MIN_FILLERS = 4
_STRONG_TEMPLATE_FILLERS = 8
_MAX_PREFIX_FILLERS = 6000
_SYNC_FILLER_MIN_SHARED = 4
_SYNC_FILLER_MIN_SHARE = 0.5
_SYNC_FILLER_MAXLEN = 24
_PROGRESS_EVERY_TEMPLATES = 2000


# attackbranch_a_charge_end -> attackbranch_a_charge, attackbranch_a, attackbranch.
def _suffix_stems(suffix):
    parts = suffix.split("_")
    return ["_".join(parts[:i]) for i in range(1, len(parts))]


# Every candidate and every underscore tail of it (Play_vo_char_oshint -> oshint), deduped on lowercase.
def suffix_candidates(candidates):
    out = {}
    for name in candidates:
        parts = name.split("_")
        for i in range(len(parts)):
            tail = "_".join(parts[i:])
            if 2 < len(tail) <= _FAMILY_SUFFIX_MAXLEN and not any(c.isdigit() for c in tail):
                out.setdefault(tail.lower(), tail)
    return list(out.values())


def _member_head(prefix, filler):
    return fnv32_feed(f"{prefix}_{filler}_".encode())


# Refilling never finds an action no member has a named event for, like ZZZ's battleswitch lines.
# Probe hits are replayed over every filler, and two fillers must agree or a collision invented them.
def discover_family_suffixes(templates, ranked_prefixes, unresolved, candidates, progress=None):
    if not candidates or not unresolved:
        return {}
    probes = _suffix_probes(templates, ranked_prefixes)
    if not probes:
        return {}
    total_probes = sum(len(picked) for _prefix, picked, _fillers in probes)
    tails = [(tail, tail.lower().encode()) for tail in suffix_candidates(candidates)]
    known_suffixes = defaultdict(set)
    for prefix, suffix in templates:
        known_suffixes[prefix].add(suffix)
    out = {}
    done = 0
    for prefix, picked, fillers in probes:
        seen_tails = set()
        for filler in picked:
            done += 1
            if progress:
                progress(done, total_probes, f"Probing {prefix}_{filler}_* for new suffixes...")
            state = _member_head(prefix, filler)
            for tail, encoded in tails:
                if fnv32_feed(encoded, state) in unresolved:
                    seen_tails.add(tail.lower())
        confirmed = _truncated_suffixes(templates, prefix, known_suffixes[prefix], unresolved)
        for tail in sorted(seen_tails):
            encoded = tail.encode()
            agreed = 0
            for filler in fillers:
                if fnv32_feed(encoded, _member_head(prefix, filler)) in unresolved:
                    agreed += 1
                    if agreed >= 2:
                        confirmed.add(tail)
                        break
        out[prefix] = confirmed
    return out


def _suffix_probes(templates, ranked_prefixes):
    probes = []
    for rank, prefix in enumerate(ranked_prefixes[:_FAMILY_SUFFIX_PREFIXES]):
        if prefix.count("_") < 1:
            continue
        seen = Counter()
        for (other, _suffix), members in templates.items():
            if other == prefix:
                seen.update(members)
        if rank < _FAMILY_SUFFIX_FULL_PREFIXES:
            picked = sorted(seen)
        else:
            ranked = sorted(seen.items(), key=lambda item: (-item[1], item[0]))
            picked = [filler for filler, _n in ranked[:_FAMILY_SUFFIX_PROBES]]
        if len(picked) >= 2:
            probes.append((prefix, picked, set(seen)))
    return probes


# Refilling never shortens a tail, so the truncations of each suffix are tried on the filler owning it.
def _truncated_suffixes(templates, prefix, known, unresolved):
    confirmed = set()
    for suffix in known:
        owners = templates.get((prefix, suffix), ())
        for stem in _suffix_stems(suffix):
            if stem in known or stem in confirmed:
                continue
            encoded = stem.encode()
            for filler in owners:
                if fnv32_feed(encoded, _member_head(prefix, filler)) in unresolved:
                    confirmed.add(stem)
                    break
    return confirmed


# (prefix, suffix) -> the tokens seen in the slot between them.
def _build_templates(names):
    templates = defaultdict(set)
    low_tokens, high_tokens = _TEMPLATE_TOKENS
    for name in sorted(names):
        tokens = name.lower().split("_")
        if not low_tokens <= len(tokens) <= high_tokens:
            continue
        for i, token in enumerate(tokens):
            if token and len(token) <= _TEMPLATE_TOKEN_MAXLEN:
                templates[("_".join(tokens[:i]), "_".join(tokens[i + 1:]))].add(token)
    for template in KNOWN_EVENT_TEMPLATES:
        head, _slot, tail = template.partition("{}")
        templates.setdefault((head.strip("_"), tail.strip("_")), set())
    return templates


# Families that enumerate an entity refill their slot across many suffixes, which ranks them first.
def _rank_prefixes(templates):
    strength = defaultdict(int)
    for (prefix, _suffix), fillers in templates.items():
        if len(fillers) >= _STRONG_TEMPLATE_FILLERS:
            strength[prefix] += len(fillers)
    return [prefix for prefix, _n in sorted(strength.items(), key=lambda item: -item[1])]


# A slot mostly filled with switch or state values takes all of them, reaching agents with no event yet.
def _fillers_by_prefix(templates, sync_fillers):
    fillers_by_prefix = defaultdict(set)
    for (prefix, _suffix), fillers in templates.items():
        if len(fillers) >= _TEMPLATE_MIN_FILLERS:
            fillers_by_prefix[prefix] |= fillers
    for fillers in fillers_by_prefix.values():
        shared = fillers & sync_fillers
        if len(shared) >= _SYNC_FILLER_MIN_SHARED and len(shared) >= len(fillers) * _SYNC_FILLER_MIN_SHARE:
            fillers |= sync_fillers
    return {prefix: sorted(fillers) for prefix, fillers in fillers_by_prefix.items()}


# Two rounds: the second rebuilds the templates around what the first found, probed suffixes included.
def crack_event_families(index, matches, progress=None, candidates=()):
    known = {m.hash_id: m.name for m in matches if m.kind in Kind.EVENTS}
    unresolved = (set(index.event_actions) | set(index.dialogue_children)) - set(known)
    if not unresolved or not known:
        return []
    sync_fillers = {m.name.lower() for m in matches
                    if m.kind in Kind.VALUES and "_" not in m.name and len(m.name) <= _SYNC_FILLER_MAXLEN}
    neighbourhood = _Neighbourhood(index)
    out = []
    for round_number in range(2):
        templates = _build_templates(set(known.values()) | {m.name for m in out})
        fillers_by_prefix = _fillers_by_prefix(templates, sync_fillers)
        if round_number == 0:
            discovered = discover_family_suffixes(
                templates, _rank_prefixes(templates), unresolved, candidates, progress)
            for prefix, suffixes in discovered.items():
                for suffix in sorted(suffixes):
                    templates.setdefault((prefix, suffix), set())
        hits = _refill_templates(templates, fillers_by_prefix, unresolved, progress)
        name_of = dict(known)
        name_of.update((m.hash_id, m.name) for m in out)
        for hashed, found in hits.items():
            name = found[0][0] if len(found) == 1 else neighbourhood.best_name(hashed, found, name_of)
            unresolved.discard(hashed)
            kind = Kind.EVENT if hashed in index.event_actions else Kind.DIALOGUE_EVENT
            out.append(NameMatch(name, kind, sorted(index.wems_for_event(hashed)), hashed))
    return out


# Returns hash -> [(name, family head)] of the distinct names that hit, several of them being a collision.
def _refill_templates(templates, fillers_by_prefix, unresolved, progress):
    hits = {}
    done = 0
    for (prefix, suffix), members in templates.items():
        done += 1
        fillers = fillers_by_prefix.get(prefix)
        if not fillers or len(fillers) > _MAX_PREFIX_FILLERS or prefix.count("_") < 1:
            continue
        head_state = fnv32_feed((prefix + "_").encode()) if prefix else FNV32_OFFSET
        tail = ("_" + suffix).encode() if suffix else b""
        for filler in fillers:
            if filler in members:
                continue
            hashed = fnv32_feed(filler.encode() + tail, head_state)
            if hashed in unresolved:
                name = "_".join(part for part in (prefix, filler, suffix) if part)
                found = hits.setdefault(hashed, [])
                # Every template splitting the same name at another slot produces it again.
                if all(name != other for other, _head in found):
                    found.append((name, f"{prefix}_{filler}_"))
        if progress and done % _PROGRESS_EVERY_TEMPLATES == 0:
            progress(done, len(templates), f"Cracking event families ({len(hits)} found)...")
    return hits


# Settles a 32-bit collision with the name whose family the events around the target share.
# ZZZ event 3350544055 sits among nassellaria's foley, so it is not zhenzhen's skill event.
class _Neighbourhood:
    def __init__(self, index):
        self.index = index
        self.events_by_target = None
        self.children_of = None

    def _build(self):
        index = self.index
        self.events_by_target = defaultdict(set)
        for event_id, action_ids in index.event_actions.items():
            for action_id in action_ids:
                action = index.actions.get(action_id)
                if action and action.target:
                    self.events_by_target[action.target].add(event_id)
        self.children_of = defaultdict(set)
        for node, parents in index.parents.items():
            for parent in parents:
                self.children_of[parent].add(node)

    # The other events targeting the event's own targets or their siblings.
    def events_around(self, event_id):
        if self.events_by_target is None:
            self._build()
        index = self.index
        targets = set()
        for action_id in index.event_actions.get(event_id, ()):
            action = index.actions.get(action_id)
            if action and action.target:
                targets.add(action.target)
        nodes = set(targets)
        for target in targets:
            for parent in index.parents.get(target, ()):
                nodes |= self.children_of.get(parent, set())
        events = set()
        for node in nodes:
            events |= self.events_by_target.get(node, set())
        events.discard(event_id)
        return events

    # The first name found wins a tie.
    def best_name(self, event_id, found, name_of):
        around = [name_of[other].lower() for other in self.events_around(event_id) if other in name_of]
        return max(found, key=lambda item: sum(1 for name in around if name.startswith(item[1])))[0]
