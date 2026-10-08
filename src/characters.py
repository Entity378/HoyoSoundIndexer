# Who the characters are, which rows belong to them and which action each row plays.
# ZZZ combat VO hangs off anonymous containers, so there a character survives only as the bank it ships in.

import re
from collections import Counter, defaultdict

from src.hashing import fnv1_32
from src.model import Kind, NameMatch
from src.vocabulary import (
    CHARACTER_GROUP_NAME, CHARACTER_SLOT_PATTERNS, EVENT_VERB_WORDS, GI_VO_ACTIONS, HSR_VO_ACTIONS, HSR_VO_HEADS,
    SPEAKER_FOLDER_PREFIX, ZZZ_VO_ACTIONS,
)

# The synthetic Character tag gets its id hashed off its name, the way a real sync would.
CHARACTER_GROUP_ID = fnv1_32("HSI_" + CHARACTER_GROUP_NAME)
# A node defined in more banks than this is shared plumbing, not one character's subtree.
_CHARACTER_BANK_LIMIT = 8
_MAX_CHARACTER_WALK = 32
# The slot also catches action words like ParryAid, which unlike names recur in other events' tails.
# A token stays a character while its tail count is at most this ratio of its slot count.
_CHARACTER_TAIL_RATIO = 2
# A spelling that only starts an avatar's spellings is that avatar (marionette), from this length on.
_GUESS_MINLEN = 5
# A voice folder this share of the voiced avatars has is part of every avatar's set: GI's gameplay, ZZZ's galgame.
_COMMON_FOLDER_SHARE = 0.9
_SPEAKER_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_NAME_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")

_ACTION_TABLES = (ZZZ_VO_ACTIONS, HSR_VO_ACTIONS, GI_VO_ACTIONS)
ACTION_LABELS = {key: label for table in _ACTION_TABLES for key, label in table}
ACTION_ORDER = {key: i for i, key in enumerate(key for table in _ACTION_TABLES for key, _label in table)}
# Longest first, so attackbranch_a_charge wins over attackbranch_a.
_SLOT_ACTION_KEYS = sorted((key for key, _label in ZZZ_VO_ACTIONS), key=len, reverse=True)


# First word -> (words, key), longest first, since the lookup runs on every voice file, 338k of them on GI.
def _by_first_word(table):
    keys = defaultdict(list)
    for key, _label in table:
        words = tuple(key.split("_"))
        keys[words[0]].append((words, key))
    return {first: sorted(options, key=lambda item: -len(item[0])) for first, options in keys.items()}


_EVENT_ACTION_KEYS = _by_first_word(HSR_VO_ACTIONS)
_FILE_ACTION_KEYS = _by_first_word(GI_VO_ACTIONS)


def normalize_character(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


# The lowercase words of a name, each also without its trailing digits (Kazuha02, tingyun1).
def name_words(name):
    words = []
    for word in _NAME_WORD_SPLIT_RE.split(name.lower()):
        if word:
            words.append(word)
            bare = word.rstrip("0123456789")
            if bare and bare != word:
                words.append(bare)
    return words


# (codename as spelled, lowercase suffix) of an event with a character slot, else None.
def slot_parts(name):
    low = name.lower()
    for pattern in CHARACTER_SLOT_PATTERNS:
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


def _voice_stem(name):
    stem = re.split(r"[\\/]", name)[-1]
    return stem[:-4] if stem.lower().endswith(".wem") else stem


# (category, speaker) of a voice file name, the speaker being the word before the take number.
# HSR files have no Vo_ folder and say it all there, as in chapter4_67_mar7th_128.
def _file_parts(name):
    words = _voice_stem(name).split("_")
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
    return _file_parts(name)[1]


def category_of_path(name):
    folders = _voice_folders(name)
    if folders:
        return folders[0]
    return _file_parts(name)[0]


# Codename -> the best casing the event names spell it with, for the tokens living in a character slot.
def slot_codenames(matches):
    counts, tails, casing = Counter(), Counter(), {}
    for m in matches:
        if m.kind not in Kind.EVENTS:
            continue
        low = m.name.lower()
        for pattern in CHARACTER_SLOT_PATTERNS:
            hit = pattern.match(low)
            if not hit:
                continue
            token = hit.group(1)
            counts[token] += 1
            spelled = m.name[hit.start(1):hit.end(1)]
            best = casing.get(token)
            if best is None or _capitals(spelled) > _capitals(best):
                casing[token] = spelled
            for tail in low[hit.end():].split("_"):
                if tail:
                    tails[tail] += 1
            break
    return {token: casing[token] for token, count in counts.items()
            if tails[token] <= _CHARACTER_TAIL_RATIO * count}


def _capitals(text):
    return sum(c.isupper() for c in text)


# Speaker -> its shown spelling, and speaker -> its voice folders.
# The shown spelling has the most capitals, then the most uses, so an export reloaded in another order agrees.
def _voice_speakers(matches):
    seen, folders = defaultdict(Counter), defaultdict(set)
    for m in matches:
        if m.kind == Kind.EXTERNAL:
            speaker = speaker_of_path(m.name)
            if speaker:
                key = normalize_character(speaker)
                seen[key][speaker] += 1
                folders[key].add(category_of_path(m.name).lower())
    spellings = {key: max(names, key=lambda s: (_capitals(s), names[s], s)) for key, names in seen.items()}
    return spellings, dict(folders)


# Every spelling met -> the name shown for it, the roster's name winning: only it knows unagi is miyabi.
# Codenames then follow the avatar switch values, where a strict prefix (jane of JaneDoe) is the same avatar.
def _character_names(index, matches, roster, guess, slots, speakers, folders):
    # Guessing needs the online roster, since an exported one is already spelled out.
    guessed = roster if guess else {}
    avatar_folders, common = _avatar_folders(guessed, folders)
    names = {}
    if slots:
        switch_names = _avatar_switch_values(index, matches, slots)

        # The event names spell a character consistently, a switch value whatever candidate cracked it.
        def shown(low):
            return slots.get(low) or switch_names.get(low) or low

        for codename, spelled in slots.items():
            if codename in roster:
                names[codename] = roster[codename]
            elif codename in switch_names:
                names[codename] = shown(codename)
            else:
                longer = [low for low in switch_names if low.startswith(codename) and len(low) > len(codename)]
                names[codename] = (shown(longer[0]) if len(longer) == 1
                                   else _prefix_owner(guessed, codename) or spelled)
    # A slot word the tail count refused is still a character when the roster knows it, as Trigger is.
    for m in matches:
        if m.kind in Kind.EVENTS:
            parts = slot_parts(m.name)
            if parts and parts[0].lower() in roster:
                names.setdefault(parts[0].lower(), roster[parts[0].lower()])
    for key, spelled in speakers.items():
        if key not in names:
            names[key] = (roster.get(key) or _prefix_owner(guessed, key)
                          or _misspelt_owner(guessed, key, folders.get(key, set()), avatar_folders, common)
                          or spelled)
    return names


# The avatar switch group is the one whose value names overlap the event codenames most.
# The Character group is skipped, since it lists the codenames and would always win.
def _avatar_switch_values(index, matches, slots):
    name_of = {m.hash_id: m.name for m in matches if m.kind in Kind.VALUES}
    best_names, best = {}, 0
    for group_id, values in index.group_values.items():
        if group_id == CHARACTER_GROUP_ID:
            continue
        names = {name_of[v].lower(): name_of[v] for v in values if v in name_of}
        hit = len(names.keys() & slots.keys())
        if hit > best:
            best_names, best = names, hit
    return best_names if best * 2 >= len(slots) else {}


def _prefix_owner(roster, key):
    if len(key) < _GUESS_MINLEN:
        return None
    owners = {shown for codename, shown in roster.items() if codename != key and codename.startswith(key)}
    return next(iter(owners)) if len(owners) == 1 else None


# A speaker one letter off an avatar who lacks a folder nearly every avatar has, and holding it, is that avatar.
# The game misspelt the folder: GI's Emilie fights as VO_gameplay\VO_emelie, ZZZ's Orphie & Magus as vo_bruxas.
def _misspelt_owner(roster, key, folders, avatar_folders, common):
    if len(key) < _GUESS_MINLEN:
        return None
    owners = {shown for codename, shown in roster.items()
              if len(codename) >= _GUESS_MINLEN and _one_edit(key, codename)}
    if len(owners) != 1:
        return None
    owner = next(iter(owners))
    return owner if folders & (common - avatar_folders.get(owner, set())) else None


def _one_edit(a, b):
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if abs(len(a) - len(b)) != 1:
        return False
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return any(short == long[:i] + long[i + 1:] for i in range(len(long)))


# Shown name -> the voice folders of the avatar's own speakers, and the folders nearly every avatar has.
def _avatar_folders(roster, folders):
    avatar_folders = defaultdict(set)
    for key, names in folders.items():
        if key in roster:
            avatar_folders[roster[key]] |= names
    counts = Counter(name for names in avatar_folders.values() for name in names)
    common = {name for name, n in counts.items() if n >= _COMMON_FOLDER_SHARE * len(avatar_folders)}
    return avatar_folders, common


# The words naming an avatar in event names, anywhere in a voice path, and as a voice file's own speaker.
# A last-word codename needs a voiced character, or GI's Test Character would own every "character".
def _avatar_words(names, roster, slots, speakers):
    voiced = {names[key] for key in set(slots) | set(speakers) if key in names}
    codenames = {}
    for codename, name in roster.items():
        shown = names.get(codename) or name
        if not (_is_last_word(codename, shown) and shown not in voiced):
            codenames[codename] = shown
    avatars = set(codenames.values())
    # Anywhere in a voice path only voice spellings and full names count, since there summer is a season.
    voice_names = {key: names[key] for key in speakers if names.get(key) in avatars}
    for shown in sorted(avatars):
        voice_names.setdefault(normalize_character(shown), shown)
    # A first name alone belongs to others too: a bare sara speaks for Mondstadt's waitress, never Kujou Sara.
    file_speakers = {codename: shown for codename, shown in codenames.items() if not _is_last_word(codename, shown)}
    return codenames, voice_names, file_speakers


def _is_last_word(codename, shown):
    words = shown.split()
    return len(words) > 1 and normalize_character(words[-1]) == codename


# A bank naming two characters names neither: it is shared content.
def _characters_by_bank(index, matches, slots):
    if not slots:
        return {}
    name_of = {m.hash_id: m.name for m in matches if m.kind in Kind.EVENTS}
    bank_tokens = defaultdict(set)
    for event_id, banks in index.event_banks.items():
        name = name_of.get(event_id)
        if not name:
            continue
        low = name.lower()
        for pattern in CHARACTER_SLOT_PATTERNS:
            hit = pattern.match(low)
            if hit and hit.group(1) in slots:
                for bank in banks:
                    bank_tokens[bank].add(hit.group(1))
                break
    return {bank: slots[next(iter(tokens))] for bank, tokens in bank_tokens.items() if len(tokens) == 1}


# The character of the nearest ancestor living in few enough banks to be private to it.
def _characters_by_wem(index, by_bank):
    nodes_of_wem = defaultdict(set)
    for node, sources in index.node_sources.items():
        for wem_id in sources:
            nodes_of_wem[wem_id].add(node)
    characters = {}
    for wem_id, nodes in nodes_of_wem.items():
        for node in nodes:
            found = _character_above(index, by_bank, node)
            if found:
                characters[wem_id] = found
                break
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


# Fills the index's character names, avatar words and bank attribution, and tags the attributed wems.
# Without the online roster an export brings back the names it saved, and nothing is guessed.
def label_characters(index, matches, roster=None, saved_names=None, saved_codenames=None):
    guess = bool(roster)
    slots = slot_codenames(matches)
    speakers, folders = _voice_speakers(matches)
    names = _character_names(index, matches, dict(roster if guess else saved_names or {}), guess, slots,
                             speakers, folders)
    index.character_names = dict(names)
    index.avatar_codenames, index.avatar_voice_names, index.avatar_file_speakers = _avatar_words(
        names, dict(roster if guess else saved_codenames or {}), slots, speakers)
    by_bank = _characters_by_bank(index, matches, slots)
    if not by_bank:
        return []
    characters = _characters_by_wem(index, by_bank)
    index.character_banks = by_bank
    index.wem_characters = characters
    if not characters:
        return []
    return _character_tags(index, matches, characters, names)


# The Character group and its values, with each value's id hashed off the shown name.
# Rows already naming a value are respelled to it, so every view agrees.
def _character_tags(index, matches, characters, names):
    values = {}
    for wem_id, token in characters.items():
        token = names.get(token.lower(), token)
        characters[wem_id] = token
        value_id = fnv1_32(token)
        values.setdefault(value_id, token)
        index.wem_tags.setdefault(wem_id, set()).add((CHARACTER_GROUP_ID, value_id))
    index.group_values.setdefault(CHARACTER_GROUP_ID, set()).update(values)
    index.sync_ids.setdefault(CHARACTER_GROUP_ID, Kind.SWITCH_GROUP)
    respelled = set()
    for m in matches:
        if m.hash_id in values:
            m.name = values[m.hash_id]
            respelled.add(m.hash_id)
    out = [NameMatch(CHARACTER_GROUP_NAME, Kind.SWITCH_GROUP, [], CHARACTER_GROUP_ID)]
    for value_id, token in values.items():
        index.sync_ids.setdefault(value_id, Kind.SWITCH)
        if value_id not in respelled:
            out.append(NameMatch(token, Kind.SWITCH, [], value_id))
    return out


# The speaker or slot owner first, then every avatar the name spells as a word, then the one owner of its wems.
# A voice path is read with the voice names, since the internal codenames are plain words there (summer, Sunna).
def characters_of_match(index, m):
    names = index.character_names
    found = []
    if m.kind == Kind.EXTERNAL:
        speaker = speaker_of_path(m.name)
        if speaker:
            found.append(names.get(normalize_character(speaker), speaker))
        # An NPC folder's line names its speaker before the take number, at times by codename (liuyun, Xianyun).
        own_speaker = index.avatar_file_speakers.get(_file_parts(m.name)[1].lower())
        if own_speaker and own_speaker not in found:
            found.append(own_speaker)
        vocabulary = index.avatar_voice_names
    else:
        parts = slot_parts(m.name) if m.kind in Kind.EVENTS else None
        owner = names.get(parts[0].lower()) if parts else None
        if owner:
            found.append(owner)
        vocabulary = index.avatar_codenames
    for word in name_words(m.name):
        shown = vocabulary.get(word)
        if shown and shown not in found:
            found.append(shown)
    if m.kind in Kind.EVENTS:
        owners = {index.wem_characters.get(w) for w in m.wem_ids}
        owner = next(iter(owners)) if len(owners) == 1 else None
        if owner:
            owner = names.get(owner.lower(), owner)
            if owner not in found:
                found.append(owner)
    return tuple(found)


# A combat line goes by its action: a ZZZ slot suffix, the words after an HSR voice head, a GI file's middle.
# Anything else goes by a voice's category, the word after an event's verb (vo, sfx, archive) or the row's kind.
def action_of_match(m):
    low = m.name.lower()
    if m.kind == Kind.EXTERNAL:
        return _known_action(_voice_stem(low).split("_"), _FILE_ACTION_KEYS) or category_of_path(m.name)
    if m.kind not in Kind.EVENTS:
        return m.kind.lower()
    parts = slot_parts(m.name)
    if parts:
        suffix = parts[1]
        for key in _SLOT_ACTION_KEYS:
            if suffix == key or suffix.startswith(key + "_"):
                return key
        return suffix.split("_")[0]
    head = next((head for head in HSR_VO_HEADS if low.startswith(head)), None)
    if head:
        # The avatar comes last, before the take number, so an unknown action is what sits before it.
        words = low[len(head):].split("_")
        while len(words) > 1 and words[-1].isdigit():
            words.pop()
        return _known_action(words, _EVENT_ACTION_KEYS) or "_".join(words[:-1]) or words[0]
    return next((word for word in low.split("_") if word and word not in EVENT_VERB_WORDS), "")


# The longest known action the words spell anywhere.
def _known_action(words, keys):
    best, size = None, 0
    for i, word in enumerate(words):
        for key_words, key in keys.get(word, ()):
            if len(key_words) > size and tuple(words[i:i + len(key_words)]) == key_words:
                best, size = key, len(key_words)
                break
    return best


def action_label(key):
    label = ACTION_LABELS.get(key)
    return f"{label}  ({key})" if label else key


def action_sort_key(key):
    return (ACTION_ORDER.get(key, len(ACTION_ORDER)), key)
