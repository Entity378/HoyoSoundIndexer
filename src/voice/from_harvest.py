# Voice names rebuilt from the harvested folder prefixes and file names.
# The language head is calibrated on a sample, and a name is kept only when its hash is a real external.

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from src.hashing import fnv64_feed
from src.jsondoc import walk_strings
from src.vocabulary import VO_LANGUAGE_CANDIDATES, VO_LEAF_CATEGORIES, VO_SUFFIXES
from src.voice.paths import is_voice_file_path, voice_tail

_CALIBRATION_SAMPLE = 4000
_CALIBRATION_MAX_PREFIXES = 16
_CRACK_MAX_PREFIXES = 48
_PROGRESS_EVERY_SOURCES = 2000


# Saved and reloaded as {"vo_prefixes": [...], "vo_sources": [...]} by --vo-out, --vo-in and the GUI.
@dataclass
class HarvestedVoice:
    prefixes: list = field(default_factory=list)
    sources: list = field(default_factory=list)

    def to_json(self):
        return {"vo_prefixes": list(self.prefixes), "vo_sources": list(self.sources)}

    @classmethod
    def from_json(cls, doc):
        return cls(list(doc.get("vo_prefixes", [])), list(doc.get("vo_sources", [])))

    @classmethod
    def load(cls, path):
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")


# No language at all, then every candidate language in each game's path shape.
def vo_head_candidates(languages=VO_LANGUAGE_CANDIDATES):
    heads = [""]
    for lang in languages:
        heads.extend([f"{lang}/Ex/", f"{lang}\\", f"{lang}/", f"{lang}/voice/"])
    return heads


def vo_language_of_head(head):
    return head.rstrip("/\\").replace("/Ex", "") or "(no language)"


def _category_of_leaf(leaf):
    low = leaf.rsplit("/", 1)[-1].lower()
    for start, category in VO_LEAF_CATEGORIES:
        if low.startswith(start):
            return category
    return None


# 'VO_Galgame/Ver2_2/Vo_Belle/' -> ('vo_galgame', 'belle').
def parse_vo_prefix(prefix):
    parts = [part for part in prefix.strip("/").split("/") if part]
    if not parts:
        return None
    category = parts[0].lower()
    speaker = ""
    for part in reversed(parts[1:]):
        if part.lower().startswith("vo_"):
            speaker = part[3:].lower()
            break
    return category, speaker


def _prefix_indexes(prefixes):
    by_category, by_speaker = defaultdict(list), defaultdict(list)
    for prefix in prefixes:
        parsed = parse_vo_prefix(prefix)
        if not parsed:
            continue
        category, speaker = parsed
        entry = (prefix.lower(), speaker)
        by_category[category].append(entry)
        if speaker:
            by_speaker[speaker].append(entry)
    return by_category, by_speaker


# The category's prefixes naming the file's speaker, all of them when few, else any naming the speaker.
def _candidate_paths(source, by_category, by_speaker, max_candidates):
    leaf = source.lower()
    if "/" in leaf or "\\" in leaf:
        return [leaf]
    paths = []
    category = _category_of_leaf(leaf)
    pool = by_category.get(category) if category else None
    if pool:
        named = [prefix for prefix, speaker in pool if speaker and speaker in leaf]
        paths = named or ([prefix for prefix, _speaker in pool] if len(pool) <= max_candidates else [])
    if not paths:
        for speaker, entries in by_speaker.items():
            if speaker and speaker in leaf:
                paths.extend(prefix for prefix, _speaker in entries)
                if len(paths) >= max_candidates:
                    break
    out = []
    for prefix in paths[:max_candidates]:
        separator = "\\" if "\\" in prefix else "/"
        out.append(prefix.rstrip("/\\") + separator + leaf)
    return out


# Which (head, suffix) pairs the game uses, found by trying them all on a sample of real ids.
def calibrate_vo_heads(prefixes, sources, external_ids, sample=_CALIBRATION_SAMPLE,
                       max_candidates=_CALIBRATION_MAX_PREFIXES):
    by_category, by_speaker = _prefix_indexes(prefixes)
    head_states = {head: fnv64_feed(head.lower().encode()) for head in vo_head_candidates()}
    hits = Counter()
    for source in sources[:sample]:
        for path in _candidate_paths(source, by_category, by_speaker, max_candidates):
            for suffix in VO_SUFFIXES:
                tail = voice_tail(path, suffix)
                for head, state in head_states.items():
                    if fnv64_feed(tail, state) in external_ids:
                        hits[(head, suffix)] += 1
    return [combo for combo, _count in hits.most_common()], hits


# Returns {external id: (hashed path without .wem, language)}, keeping only real ids.
def crack_vo_names(prefixes, sources, external_ids, heads=None, progress=None,
                   max_candidates=_CRACK_MAX_PREFIXES):
    external_ids = set(external_ids)
    sources = list(sources)
    combos = heads
    if combos is None:
        if progress:
            progress(0, len(sources), "Calibrating voice path rule...")
        combos, _hits = calibrate_vo_heads(prefixes, sources, external_ids)
        if progress:
            found = ", ".join(f"{vo_language_of_head(h)}{s or ''}" for h, s in combos[:4]) or "none"
            progress(0, len(sources), f"Voice rule: {found}")
    if not combos:
        return {}

    by_category, by_speaker = _prefix_indexes(prefixes)
    head_states = {head: fnv64_feed(head.lower().encode()) for head, _suffix in combos}
    resolved = {}
    total = len(sources)
    for i, source in enumerate(sources):
        for path in _candidate_paths(source, by_category, by_speaker, max_candidates):
            for head, suffix in combos:
                state = head_states.get(head)
                if state is None:
                    continue
                hashed = fnv64_feed(voice_tail(path, suffix), state)
                if hashed in external_ids and hashed not in resolved:
                    resolved[hashed] = (head + path + suffix, vo_language_of_head(head))
        if progress and i % _PROGRESS_EVERY_SOURCES == 0:
            progress(i, total, f"Recovering voice names ({len(resolved)} found)")
    if progress:
        progress(total, total, f"Voice names: {len(resolved)} recovered")
    return resolved


# For games with no plaintext paths in the client: every .wem string of any field of the config files.
def import_vo_sources(path, progress=None):
    root = Path(path)
    files = sorted(root.rglob("*.json")) if root.is_dir() else [root]
    found = set()
    for i, file in enumerate(files):
        try:
            doc = json.loads(file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        found.update(text for text in walk_strings(doc) if is_voice_file_path(text))
        if progress and i % 200 == 0:
            progress(i, len(files), f"Importing voice paths ({len(found)} found)")
    if progress:
        progress(len(files), len(files), f"Imported {len(found)} voice paths")
    return sorted(found)
