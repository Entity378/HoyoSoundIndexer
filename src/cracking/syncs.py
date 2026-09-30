# Unnamed state, switch and group ids, cracked from code-side concatenations like BGM_Combat.

from collections import defaultdict

from src.hashing import fnv1_32, fnv32_feed
from src.model import Kind, NameMatch
from src.vocabulary import GROUP_DOMAINS, GROUP_WRAPPERS, SYNC_TAILS, TAG_PREFIXES

# Beyond this many prefixes the per-group pass costs more than it finds.
_GROUP_PREFIX_BUDGET = 12
_GROUP_VALUE_MAX_LEN = 40
_TEMPLATE_TOKENS = (2, 10)
_TEMPLATE_MIN_FILLERS = 3


# The values of SwitchGroup_AvatarSwitchAidAttackType are AidAttack_CommonAid, AidAttack_ParryAid and so on.
# So a group's value prefixes come from its own name, peeled one wrapper at a time.
def group_value_prefixes(group_name):
    stem = group_name
    for wrapper in GROUP_WRAPPERS:
        if stem.lower().startswith(wrapper):
            stem = stem[len(wrapper):]
            break
    stems = set()
    for candidate in (stem, stem[:-4] if stem.lower().endswith("type") else ""):
        if not candidate:
            continue
        stems.add(candidate)
        for domain in GROUP_DOMAINS:
            if candidate.lower().startswith(domain):
                rest = candidate[len(domain):].strip("_")
                if rest:
                    stems.add(rest)
                    if rest.lower().endswith("type"):
                        stems.add(rest[:-4])
    return {stem + "_" for stem in stems if len(stem) > 2}


# candidates is the harvested and online identifier pool, and the first name to hit an id keeps it.
def crack_sync_names(index, candidates, matches, progress=None):
    matched_ids = {m.hash_id for m in matches}
    wanted = {sid for sid in index.sync_ids if sid not in matched_ids}
    if not wanted:
        return []
    found = {}

    def take(name, hashed):
        if hashed in wanted and hashed not in found:
            found[hashed] = name

    # Encoded once, since lower().encode() over a 700k pool costs more than the hashing.
    tails = [(candidate, candidate.lower().encode()) for candidate in candidates]
    _tag_prefix_pass(tails, take)
    _group_prefix_pass(index, matches, tails, wanted, found, progress)
    sync_names = {m.name for m in matches if m.hash_id in index.sync_ids} | set(found.values())
    _template_pass(sync_names, take)
    _tail_word_pass(sync_names | set(found.values()), take)
    return [NameMatch(name, index.sync_ids.get(hashed, Kind.STATE), [], hashed)
            for hashed, name in found.items()]


def _tag_prefix_pass(tails, take):
    prefix_states = [(prefix, fnv32_feed(prefix.lower().encode())) for prefix in TAG_PREFIXES]
    for candidate, tail in tails:
        for prefix, state in prefix_states:
            take(prefix + candidate, fnv32_feed(tail, state))


# A prefix is tried only on its own group's values, so a 32-bit collision cannot name a stranger.
# Only bare identifiers follow a group prefix, which halves the candidate pool.
def _group_prefix_pass(index, matches, tails, wanted, found, progress):
    leaves = [pair for pair in tails if "_" not in pair[0] and len(pair[0]) <= _GROUP_VALUE_MAX_LEN]
    named_groups = {m.hash_id: m.name for m in matches if m.kind in Kind.GROUPS}
    targets_by_prefix = defaultdict(set)
    ranked = sorted(index.group_values.items(),
                    key=lambda item: -len([v for v in item[1] if v in wanted]))
    for group_id, values in ranked:
        group_name = named_groups.get(group_id)
        targets = {v for v in values if v in wanted} if group_name else set()
        if not targets:
            continue
        for prefix in sorted(group_value_prefixes(group_name)):
            if prefix not in targets_by_prefix and len(targets_by_prefix) >= _GROUP_PREFIX_BUDGET:
                continue
            targets_by_prefix[prefix] |= targets
    for done, (prefix, targets) in enumerate(targets_by_prefix.items()):
        if progress:
            progress(done, len(targets_by_prefix), f"Cracking {prefix}* tags ({len(found)} found)...")
        state = fnv32_feed(prefix.lower().encode())
        for candidate, tail in leaves:
            hashed = fnv32_feed(tail, state)
            if hashed in targets and hashed not in found:
                found[hashed] = prefix + candidate


# A token seen in the slot of a crowded template is tried in the slot of every other template.
def _template_pass(sync_names, take):
    templates = defaultdict(set)
    low_tokens, high_tokens = _TEMPLATE_TOKENS
    for name in sorted(sync_names):
        tokens = name.lower().split("_")
        if low_tokens <= len(tokens) <= high_tokens:
            for i, token in enumerate(tokens):
                if token:
                    templates[("_".join(tokens[:i]), "_".join(tokens[i + 1:]))].add(token)
    fillers = set()
    for members in templates.values():
        if len(members) >= _TEMPLATE_MIN_FILLERS:
            fillers |= members
    fillers = sorted(fillers)
    for (prefix, suffix), members in templates.items():
        for filler in fillers:
            if filler in members:
                continue
            name = "_".join(part for part in (prefix, filler, suffix) if part)
            take(name, fnv1_32(name))


def _tail_word_pass(stems, take):
    for stem in sorted(stems):
        for tail_word in SYNC_TAILS:
            name = f"{stem}_{tail_word}"
            take(name, fnv1_32(name))
