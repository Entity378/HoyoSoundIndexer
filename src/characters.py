# Combat VO is routed through anonymous containers, so a character survives only as the bank it ships in.
# That bank also holds the character's named events, which is what label_characters reads.

import re
from collections import Counter, defaultdict

from src.hashing import fnv1_32
from src.model import Kind, NameMatch
from src.vocabulary import (
    CHARACTER_EVENT_PATTERNS, CHARACTER_GROUP_NAME, COMBAT_ACTIONS, SPEAKER_FOLDER_PREFIX,
)

# A node defined in more banks than this is shared plumbing, not one character's subtree.
_CHARACTER_BANK_LIMIT = 8
_MAX_CHARACTER_WALK = 32
# The synthetic Character tag gets its id hashed off its name, the way a real sync would.
CHARACTER_GROUP_ID = fnv1_32("HSI_" + CHARACTER_GROUP_NAME)
# The slot also catches action words like ParryAid, which unlike names recur in other events' tails.
# A token stays a character while its tail count is at most this ratio of its slot count.
_CHARACTER_TAIL_RATIO = 2
# A spelling that only starts an avatar's spellings is that avatar (marionette), from this length on.
_ROSTER_PREFIX_MIN = 5
_SPEAKER_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

ACTION_LABELS = dict(COMBAT_ACTIONS)
ACTION_ORDER = {key: i for i, (key, _label) in enumerate(COMBAT_ACTIONS)}
# Longest first, so attackbranch_a_charge wins over attackbranch_a.
_ACTION_KEYS = sorted(ACTION_LABELS, key=len, reverse=True)


def normalize_character(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


# (codename as spelled, lowercase suffix) of an event named after a character, else None.
def character_event_parts(name):
    low = name.lower()
    for pattern in CHARACTER_EVENT_PATTERNS:
        hit = pattern.match(low)
        if hit:
            return name[hit.start(1):hit.end(1)], low[hit.end():]
    return None


def _voice_folders(name):
    out = []
    head = len(SPEAKER_FOLDER_PREFIX)
    for part in re.split(r"[\\/]", name)[:-1]:
        if part.lower().startswith(SPEAKER_FOLDER_PREFIX) and len(part) > head:
            out.append(part[head:])
    return out


# HSR voice files have no Vo_ folder: the file name starts with the category.
# The speaker is the word before the take number, as in chapter4_67_mar7th_128.
def _voice_leaf_parts(name):
    stem = re.split(r"[\\/]", name)[-1]
    if stem.lower().endswith(".wem"):
        stem = stem[:-4]
    words = stem.split("_")
    take = max((i for i, word in enumerate(words) if word.isdigit()), default=-1)
    while take > 1 and words[take - 1].isdigit():
        take -= 1
    if take < 2 or not _SPEAKER_WORD_RE.match(words[take - 1]):
        return "", ""
    return words[0].lower(), words[take - 1]


# The last Vo_ folder unless it is also the category, else the word before the take number.
def speaker_of_path(name):
    folders = _voice_folders(name)
    if folders:
        return folders[-1] if len(folders) >= 2 else ""
    return _voice_leaf_parts(name)[1]


def category_of_path(name):
    folders = _voice_folders(name)
    if folders:
        return folders[0]
    return _voice_leaf_parts(name)[0]


# Codename -> the best casing the event names spell it with, for tokens that live in the slot.
def _character_slots(matches):
    slots, tails, casing = Counter(), Counter(), {}
    for m in matches:
        if m.kind not in Kind.EVENTS:
            continue
        low = m.name.lower()
        for pattern in CHARACTER_EVENT_PATTERNS:
            hit = pattern.match(low)
            if not hit:
                continue
            token = hit.group(1)
            slots[token] += 1
            original = m.name[hit.start(1):hit.end(1)]
            best = casing.get(token)
            if best is None or _capitals(original) > _capitals(best):
                casing[token] = original
            for tail in low[hit.end():].split("_"):
                if tail:
                    tails[tail] += 1
            break
    return {token: casing[token] for token, count in slots.items()
            if tails[token] <= _CHARACTER_TAIL_RATIO * count}


def _capitals(text):
    return sum(c.isupper() for c in text)


# A bank naming two characters names neither: it is shared content.
def _characters_by_bank(index, matches):
    vocabulary = _character_slots(matches)
    if not vocabulary:
        return {}
    name_of = {m.hash_id: m.name for m in matches if m.kind in Kind.EVENTS}
    bank_tokens = defaultdict(set)
    for event_id, banks in index.event_banks.items():
        name = name_of.get(event_id)
        if not name:
            continue
        low = name.lower()
        for pattern in CHARACTER_EVENT_PATTERNS:
            hit = pattern.match(low)
            if hit and hit.group(1) in vocabulary:
                for bank in banks:
                    bank_tokens[bank].add(hit.group(1))
                break
    return {bank: vocabulary[next(iter(tokens))]
            for bank, tokens in bank_tokens.items() if len(tokens) == 1}


# The character of the nearest ancestor living in few enough banks to be private to it.
def _characters_by_wem(index, by_bank):
    nodes_of_wem = defaultdict(set)
    for node, sources in index.node_sources.items():
        for wem_id in sources:
            nodes_of_wem[wem_id].add(node)
    characters = {}
    for wem_id, nodes in nodes_of_wem.items():
        found = None
        for node in nodes:
            found = _character_above(index, by_bank, node)
            if found:
                break
        if found:
            characters[wem_id] = found
    return characters


def _character_above(index, by_bank, node):
    current = node
    for _ in range(_MAX_CHARACTER_WALK):
        banks = index.object_banks.get(current)
        if banks and len(banks) <= _CHARACTER_BANK_LIMIT:
            tokens = {by_bank[bank] for bank in banks if bank in by_bank}
            if len(tokens) == 1:
                return next(iter(tokens))
        parents = index.parents.get(current)
        if not parents:
            return None
        current = next(iter(parents))
    return None


# Fills avatar_names, character_banks and wem_characters, and tags each attributed wem.
# Rows matching a character value are renamed to the shown spelling, so every view agrees.
def label_characters(index, matches, avatar_names=None, by_prefix=True):
    # Kept even when no bank names a character, as on GI and HSR: the list and the export need it.
    aliases = character_aliases(index, matches, avatar_names, by_prefix)
    index.avatar_names = dict(aliases)
    by_bank = _characters_by_bank(index, matches)
    if not by_bank:
        return []
    characters = _characters_by_wem(index, by_bank)
    index.character_banks = by_bank
    index.wem_characters = characters
    if not characters:
        return []
    values = {}
    for wem_id, token in characters.items():
        token = aliases.get(token.lower(), token)
        characters[wem_id] = token
        value_id = fnv1_32(token)
        values.setdefault(value_id, token)
        index.wem_tags.setdefault(wem_id, set()).add((CHARACTER_GROUP_ID, value_id))
    index.group_values.setdefault(CHARACTER_GROUP_ID, set()).update(values)
    index.sync_ids.setdefault(CHARACTER_GROUP_ID, Kind.SWITCH_GROUP)
    named = set()
    for m in matches:
        if m.hash_id in values:
            m.name = values[m.hash_id]
            named.add(m.hash_id)
    out = [NameMatch(CHARACTER_GROUP_NAME, Kind.SWITCH_GROUP, [], CHARACTER_GROUP_ID)]
    for value_id, token in values.items():
        index.sync_ids.setdefault(value_id, Kind.SWITCH)
        if value_id not in named:
            out.append(NameMatch(token, Kind.SWITCH, [], value_id))
    return out


def _roster_prefix_owner(roster, key):
    if len(key) < _ROSTER_PREFIX_MIN:
        return None
    owners = {display for code, display in roster.items() if code != key and code.startswith(key)}
    return next(iter(owners)) if len(owners) == 1 else None


# The shown spelling has the most capitals, then the most uses, and is never just the first met.
# An export reloads its rows in another order, and the list must come back the same.
def _voice_speakers(matches):
    seen = defaultdict(Counter)
    for m in matches:
        if m.kind == Kind.EXTERNAL:
            speaker = speaker_of_path(m.name)
            if speaker:
                seen[normalize_character(speaker)][speaker] += 1
    return {key: max(spellings, key=lambda s: (_capitals(s), spellings[s], s))
            for key, spellings in seen.items()}


# The avatar switch group is the one whose value names overlap the event codenames most.
# The Character group is skipped, since it lists the codenames and would always win.
def _avatar_switch_values(index, matches, casing):
    name_of = {m.hash_id: m.name for m in matches if m.kind in Kind.VALUES}
    codenames = set(casing)
    avatars, best = {}, 0
    for group_id, values in index.group_values.items():
        if group_id == CHARACTER_GROUP_ID:
            continue
        names = {name_of[v].lower(): name_of[v] for v in values if v in name_of}
        hit = len(names.keys() & codenames)
        if hit > best:
            avatars, best = names, hit
    return avatars if best * 2 >= len(casing) else {}


# The online roster wins, being the only source that knows unagi and miyabi are one person.
# Codenames then follow the avatar switch values, where a strict prefix (jane of JaneDoe) is the same avatar.
def character_aliases(index, matches, avatar_names=None, by_prefix=True):
    roster = dict(avatar_names or {})
    # Prefix guessing uses the online roster only, since an exported roster is already spelled out.
    starts = roster if by_prefix else {}
    aliases = {}
    casing = _character_slots(matches)
    if casing:
        avatars = _avatar_switch_values(index, matches, casing)

        # The event names spell a character consistently, a switch value whatever candidate cracked it.
        def display(low):
            return casing.get(low) or avatars.get(low) or low

        for code, spelled in casing.items():
            if code in roster:
                aliases[code] = roster[code]
            elif code in avatars:
                aliases[code] = display(code)
            else:
                longer = [low for low in avatars if low.startswith(code) and len(low) > len(code)]
                aliases[code] = (display(longer[0]) if len(longer) == 1
                                 else _roster_prefix_owner(starts, code) or spelled)
    # A slot word the tail count refused is still a character when the roster knows it, as Trigger is.
    for m in matches:
        if m.kind in Kind.EVENTS:
            parts = character_event_parts(m.name)
            if parts and parts[0].lower() in roster:
                aliases.setdefault(parts[0].lower(), roster[parts[0].lower()])
    for key, spelled in _voice_speakers(matches).items():
        if key not in aliases:
            aliases[key] = roster.get(key) or _roster_prefix_owner(starts, key) or spelled
    return aliases


# The codename in its name, else the one owner of all its wems, else the speaker of a voice path.
def character_of_match(index, m, aliases):
    if m.kind in Kind.EVENTS:
        parts = character_event_parts(m.name)
        if parts:
            return aliases.get(parts[0].lower(), "")
        owners = {index.wem_characters.get(w) for w in m.wem_ids}
        if len(owners) == 1:
            owner = next(iter(owners))
            return aliases.get(owner.lower(), owner) if owner else ""
        return ""
    if m.kind == Kind.EXTERNAL:
        speaker = speaker_of_path(m.name)
        return aliases.get(normalize_character(speaker), speaker)
    return ""


# The known combat action its suffix starts with, else the suffix's first token, or a voice's category.
def action_of_match(m):
    if m.kind == Kind.EXTERNAL:
        return category_of_path(m.name)
    parts = character_event_parts(m.name)
    if not parts:
        return ""
    suffix = parts[1]
    for key in _ACTION_KEYS:
        if suffix == key or suffix.startswith(key + "_"):
            return key
    return suffix.split("_")[0]


def action_label(key):
    label = ACTION_LABELS.get(key)
    return f"{label}  ({key})" if label else key


def action_sort_key(key):
    return (ACTION_ORDER.get(key, len(ACTION_ORDER)), key)
