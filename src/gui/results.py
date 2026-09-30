# The result list as plain data, free of Qt: rows, tags, id lookups and the filter.

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from src.characters import (
    CHARACTER_GROUP_ID, action_of_match, character_of_match, normalize_character,
)
from src.model import Kind, NameMatch
from src.vocabulary import VO_EVENT_PATTERN

TYPE_BUCKETS = (("all", "All"), ("vo", "Voice"), ("sfx", "SFX"),
                ("music", "Music"), ("bank", "Bank"), ("sync", "State / other"))
# A group routing a wem on nearly all its values makes a wall of names, as ZZZ's 71 partners do.
# Past this share of a group's values the cell lists what does not select the wem.
TAG_COLLAPSE_MIN_VALUES = 8
TAG_COLLAPSE_SHARE = 0.75
TAG_COLLAPSE_MAX_REST = 5
MAX_ID_LOOKUPS = 100
TAGGED_ROW_LABEL = "(wems tagged with the character, no event of their own)"


def bucket_of_kind(kind):
    if kind == Kind.EXTERNAL:
        return "vo"
    if kind in Kind.EVENTS:
        return "sfx"
    if kind in (Kind.MUSIC_SEGMENT, Kind.MUSIC, Kind.MUSIC_BRANCH):
        return "music"
    if kind == Kind.BANK:
        return "bank"
    return "sync"


def bucket_of_match(m):
    if m.kind in Kind.EVENTS and VO_EVENT_PATTERN.match(m.name.lower()):
        return "vo"
    return bucket_of_kind(m.kind)


# The first path segment: GI separates with a backslash, HSR and ZZZ with a slash.
def language_of_match(m):
    if m.kind != Kind.EXTERNAL:
        return ""
    head = re.split(r"[\\/]", m.name, maxsplit=1)[0]
    return head if head and not head.lower().endswith(".wem") else ""


# Only ASCII digits, since "²" passes isdigit() but int() refuses it.
def typed_id(text):
    return int(text) if text.isascii() and text.isdigit() else None


# (mode, ids): none prints the values, all prints none, except the few missing, partial counts them.
def collapse_tag_values(values, known):
    known = [value for value in known if value]
    picked = set(values)
    if len(known) < TAG_COLLAPSE_MIN_VALUES or len(picked) < len(known) * TAG_COLLAPSE_SHARE:
        return "none", values
    rest = [value for value in known if value not in picked]
    if not rest:
        return "all", []
    if len(rest) <= TAG_COLLAPSE_MAX_REST:
        return "except", rest
    return "partial", rest


# A tag is a (group id, value id) pair, rendered with the resolved names.
# The raw ids stay in the search text, so a number from another tool finds the same rows.
class TagRenderer:
    def __init__(self, index, matches):
        self.index = index
        self.name_of = {}
        for m in matches:
            self.name_of.setdefault(m.hash_id, m.name)
        self.group_of_value = defaultdict(set)
        for group_id, values in index.group_values.items():
            for value_id in values:
                self.group_of_value[value_id].add(group_id)

    def label(self, sync_id):
        return self.name_of.get(sync_id) or str(sync_id)

    def tags_of_match(self, m):
        if m.kind in Kind.GROUPS:
            return [(m.hash_id, value_id) for value_id in sorted(self.index.group_values.get(m.hash_id, ()))]
        if m.kind in Kind.VALUES:
            return [(group_id, m.hash_id) for group_id in sorted(self.group_of_value.get(m.hash_id, ()))]
        tags = set(self.index.event_tags.get(m.hash_id, ()))
        for wem_id in m.wem_ids:
            tags |= self.index.wem_tags.get(wem_id, set())
        return sorted(tags)

    def tags_of_wem(self, wem_id):
        return sorted(self.index.wem_tags.get(wem_id, ()))

    # With collapse off every value is spelled out, which is what the search text indexes.
    def render(self, tags, collapse=True):
        if not tags:
            return ""
        values_by_group = {}
        for group_id, value_id in tags:
            values = values_by_group.setdefault(group_id, [])
            if value_id and value_id not in values:
                values.append(value_id)
        parts = []
        for group_id, values in values_by_group.items():
            label = self.label(group_id)
            mode, shown = "none", values
            if collapse and values:
                mode, shown = collapse_tag_values(values, self.index.group_values.get(group_id, ()))
            if mode == "all":
                label += "=*"
            elif mode == "except":
                label += "=* except " + "|".join(self.label(v) for v in shown)
            elif mode == "partial":
                label += f"=* ({len(values)} of {len(values) + len(shown)})"
            elif values:
                label += "=" + "|".join(self.label(v) for v in values)
            parts.append(label)
        return "; ".join(parts)

    # A group row lists its own values, which are its content and never collapse.
    def match_text(self, m, tags=None):
        tags = self.tags_of_match(m) if tags is None else tags
        return self.render(tags, collapse=m.kind not in Kind.GROUPS)

    def wem_text(self, wem_id):
        return self.render(self.tags_of_wem(wem_id))

    def search_text(self, tags):
        if not tags:
            return ""
        ids = {str(sync_id) for pair in tags for sync_id in pair if sync_id}
        return self.render(tags, collapse=False).lower() + " " + " ".join(sorted(ids))


# size adds up the biggest copy of each wem, and duration is the longest wem's, -1 when none is known.
@dataclass(eq=False, slots=True)
class ResultRow:
    match: NameMatch
    name_lower: str
    bucket: str
    language: str
    character: str = ""
    action: str = ""
    tag_text: str = ""
    tag_search: str = ""
    size: int = 0
    duration: int = -1


# Wem id -> the size of its biggest copy, the one playback and export use.
def _biggest_sizes(index):
    sizes = {}
    for table in (index.wem_locations, index.external_locations):
        for wem_id, locations in table.items():
            biggest = max(location.size for location in locations)
            if biggest > sizes.get(wem_id, 0):
                sizes[wem_id] = biggest
    return sizes


# lookups are the wem and bank ids matching a typed number, sync_rows a typed sync id nothing names.
@dataclass
class Selection:
    rows: list = field(default_factory=list)
    lookups: list = field(default_factory=list)
    sync_rows: list = field(default_factory=list)


class ResultModel:
    def __init__(self, index, matches):
        self.index = index
        self.matches = matches
        self.tags = TagRenderer(index, matches)
        self.aliases = dict(index.avatar_names)
        self.wem_sizes = _biggest_sizes(index)
        self.rows = [self._row(m) for m in matches]
        # The rows of each bucket and character in list order, so a filter on either walks only those.
        self.rows_by_bucket = defaultdict(list)
        self.rows_by_character = defaultdict(list)
        for row in self.rows:
            self.rows_by_bucket[row.bucket].append(row)
            if row.character:
                self.rows_by_character[row.character].append(row)
        self.character_wems = defaultdict(set)
        for wem_id, owner in index.wem_characters.items():
            self.character_wems[self.aliases.get(owner.lower(), owner)].add(wem_id)
        self._build_id_lookup()

    def _row(self, m):
        tags = self.tags.tags_of_match(m)
        return ResultRow(match=m, name_lower=m.name.lower(), bucket=bucket_of_match(m),
                         language=language_of_match(m),
                         character=character_of_match(self.index, m, self.aliases),
                         action=action_of_match(m), tag_text=self.tags.match_text(m, tags),
                         tag_search=self.tags.search_text(tags), size=self.size_of(m.wem_ids),
                         duration=self.duration_of(m.wem_ids))

    def size_of(self, wem_ids):
        return sum(self.wem_sizes.get(wem_id, 0) for wem_id in wem_ids)

    def duration_of(self, wem_ids):
        durations = self.index.wem_durations
        return max((durations.get(wem_id, -1) for wem_id in wem_ids), default=-1)

    # Every wem and bank id as text, so the search box can match a piece of a number.
    def _build_id_lookup(self):
        self.wems_by_bank = defaultdict(set)
        wem_ids = set(self.index.wem_locations) | set(self.index.external_locations)
        for wem_id in wem_ids:
            for location in self.index.locations_of(wem_id):
                if location.bnk_id:
                    self.wems_by_bank[location.bnk_id].add(wem_id)
        self.id_strings = [(str(i), i) for i in wem_ids | set(self.wems_by_bank)]

    def filter(self, text, bucket="all", character="", languages=(), audio_only=False):
        text = text.strip().lower()
        as_id = typed_id(text)
        id_hits = {i for s, i in self.id_strings if text in s} if as_id is not None else set()
        if character:
            candidates = self.rows_by_character.get(character, ())
        elif bucket != "all":
            candidates = self.rows_by_bucket.get(bucket, ())
        else:
            candidates = self.rows
        selection = Selection()
        for row in candidates:
            match = row.match
            if bucket != "all" and row.bucket != bucket:
                continue
            if character and row.character != character:
                continue
            if row.language and row.language not in languages:
                continue
            if audio_only and not match.wem_ids:
                continue
            if text and not (text in row.name_lower
                             or (as_id is not None and text in str(match.hash_id))
                             or (id_hits and not id_hits.isdisjoint(match.wem_ids))
                             or text in row.tag_search):
                continue
            selection.rows.append(row)
        selection.lookups = sorted(id_hits)[:MAX_ID_LOOKUPS]
        if as_id is not None and as_id in self.index.sync_ids and as_id not in self.tags.name_of:
            selection.sync_rows = [self.sync_lookup_row(as_id)]
        return selection

    def sync_lookup_row(self, sync_id):
        kind = self.index.sync_ids[sync_id]
        m = NameMatch(f"({kind.lower()} {sync_id})", kind, sorted(self.index.sync_wems.get(sync_id, ())),
                      sync_id)
        return ResultRow(match=m, name_lower=m.name.lower(), bucket=bucket_of_match(m), language="",
                         tag_text=self.tags.match_text(m), size=self.size_of(m.wem_ids),
                         duration=self.duration_of(m.wem_ids))

    # The wems only the character's tag reaches, so the list really is everything of that character.
    def tagged_row(self, character, rows):
        covered = {wem_id for row in rows for wem_id in row.match.wem_ids}
        rest = sorted(self.character_wems.get(character, set()) - covered)
        if not rest:
            return None
        m = NameMatch(TAGGED_ROW_LABEL, Kind.TAGGED, rest, CHARACTER_GROUP_ID)
        return ResultRow(match=m, name_lower=m.name.lower(), bucket=bucket_of_match(m), language="",
                         size=self.size_of(rest), duration=self.duration_of(rest))

    def bucket_counts(self):
        return Counter(row.bucket for row in self.rows)

    def language_counts(self):
        return Counter(row.language for row in self.rows if row.language)

    def character_counts(self):
        return Counter(row.character for row in self.rows if row.character)

    # Other names skip longer forms (normahollowell for Norma) but keep real ones (zhenzhen for Ye Shunguang).
    # The search text holds every spelling, so heizo finds Shikanoin Heizou.
    def character_spellings(self):
        others, spellings = defaultdict(set), defaultdict(set)
        for code, display in self.aliases.items():
            spellings[display].add(code)
            canonical = normalize_character(display)
            if code != canonical and canonical not in code:
                others[display].add(code)
        texts = {name: " ".join([name.lower()] + sorted(codes)) for name, codes in spellings.items()}
        return others, texts
