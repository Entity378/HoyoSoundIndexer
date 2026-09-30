# The avatar roster: every spelling of a character -> the name players see.
# Field names are obfuscated and rotate every patch, so each row value is judged by its shape.

import re
from collections import Counter, defaultdict

from src.characters import normalize_character

_AVATAR_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{2,23}$")
# A resolved text longer than this is a description, not a name.
_AVATAR_NAME_MAX = 40
# Integers below this are ids, levels and counts, never text hashes.
_AVATAR_TEXT_KEY_MIN = 1 << 16
# From this length the last word of a display name is the given name the voice folders use (heizou).
_AVATAR_GIVEN_NAME_MIN = 5
_TEXT_MARKUP_RE = re.compile(r"<[^>]*>")
_FILE_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_VALUE_PARTS_RE = re.compile(r"[|/\\\s,;]+")


# Placeholders like {NICKNAME}, line breaks and description-length texts are not names.
def _avatar_name_text(texts, value):
    if isinstance(value, int) and abs(value) < _AVATAR_TEXT_KEY_MIN:
        return None
    text = texts.get(str(value))
    if not isinstance(text, str):
        return None
    text = _TEXT_MARKUP_RE.sub("", text).strip()
    if not text or len(text) > _AVATAR_NAME_MAX or "{" in text or "\n" in text or "\\n" in text:
        return None
    return text


# The value itself when it is one bare word, else the last word of each part (UI_AvatarIcon_Heizo).
# All capitals is an enum like WEAPON_BOW, not a codename.
def _avatar_code_words(value):
    for part in _VALUE_PARTS_RE.split(value):
        part = _FILE_EXTENSION_RE.sub("", part)
        word = part if _AVATAR_CODE_RE.match(part) else part.rsplit("_", 1)[-1]
        if _AVATAR_CODE_RE.match(word) and not word.isupper():
            yield word.lower()


# Test rows reuse a real avatar's icons but come after it, so the first row repeating a code most wins.
# A word most rows carry, or one no row repeats (Knight, Ice), belongs to nobody.
def _avatar_claim_owner(claims, total, repeated):
    displays = {display for _count, _row, display in claims}
    if len(displays) == 1:
        return next(iter(displays))
    if not repeated or len(claims) * 2 > total:
        return None
    best = max(count for count, _row, _display in claims)
    if best < 2:
        return None
    return min((row, display) for count, row, display in claims if count == best)[1]


# A row names one avatar: its code names claim first, then its normalized names, then its given name.
# The display is the shortest name the row resolves to, Miyabi and not Hoshimi Miyabi.
def avatar_roster(rows, texts):
    named = []
    for values in rows:
        shown = [text for text in (_avatar_name_text(texts, value) for value in values) if text]
        if not shown:
            continue
        codes = Counter(word for value in values if isinstance(value, str)
                        for word in _avatar_code_words(value))
        named.append((min(shown, key=lambda text: (len(text), text)), codes,
                      {normalize_character(text) for text in shown}))
    code_claims, name_claims, given_claims = defaultdict(list), defaultdict(list), defaultdict(list)
    for row, (display, codes, names) in enumerate(named):
        for key, count in codes.items():
            code_claims[key].append((count, row, display))
        for key in names:
            name_claims[key].append((1, row, display))
        words = display.split()
        given = normalize_character(words[-1]) if len(words) > 1 else ""
        if len(given) >= _AVATAR_GIVEN_NAME_MIN and given.isalpha():
            given_claims[given].append((1, row, display))
    roster = {}
    for claims, repeated in ((code_claims, True), (name_claims, False), (given_claims, False)):
        for key, entries in claims.items():
            if key and key not in roster:
                owner = _avatar_claim_owner(entries, len(named), repeated)
                if owner:
                    roster[key] = owner
    return roster
