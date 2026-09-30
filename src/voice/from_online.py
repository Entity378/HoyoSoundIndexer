# Voice names from the full paths the online tables list, then from the paths they skip, extrapolated.

import re
from collections import defaultdict

from src.hashing import fnv64_feed
from src.vocabulary import VO_ONLINE_LANGS, VO_SUFFIXES
from src.voice.paths import speaker_of_folder, split_voice_path, voice_tail

# ZZZ uses '<lang>/Ex/', GI '<lang>\' and HSR '<lang>/voice/' with the _m/_f protagonist suffix.
_HEAD_SHAPES = {
    "GI": (("{lang}\\", False),),
    "SR": (("{lang}/voice/", True),),
    "ZZZ": (("{lang}/Ex/", False),),
}
_DEFAULT_HEAD_SHAPES = (("{lang}\\", False), ("{lang}/voice/", True))
_HEAD_SAMPLE = 4000
_PROGRESS_EVERY_PATHS = 4000
_TAKE_SUFFIX_RE = re.compile(r"_(\d{2,3})(\.wem)?$", re.I)
# Takes are probed up to the highest one seen plus the margin, and at least up to the minimum.
_MIN_TAKES_PROBED = 15
_TAKE_PROBE_MARGIN = 4
_MAX_EXTRAPOLATED_CANDIDATES = 40_000_000


def _online_heads(game, languages):
    heads = []
    for lang in languages:
        for shape, gendered in _HEAD_SHAPES.get(game, _DEFAULT_HEAD_SHAPES):
            heads.append((shape.format(lang=lang), lang, gendered))
    return heads


def _suffixes(gendered):
    return VO_SUFFIXES if gendered else ("",)


# Returns {external id: (full hashed path, language)}.
def resolve_online_voices(game, voice_paths, external_ids, progress=None):
    external_ids = set(external_ids)
    paths = [path for path in voice_paths if path]
    heads = _online_heads(game, VO_ONLINE_LANGS.get(game, VO_ONLINE_LANGS["GI"]))
    live = _live_heads(heads, paths, external_ids)

    resolved = {}
    total = len(paths)
    for i, path in enumerate(paths):
        path_low = path.lower()
        for head, lang, gendered, state in live:
            for suffix in _suffixes(gendered):
                hashed = fnv64_feed(voice_tail(path_low, suffix), state)
                if hashed in external_ids and hashed not in resolved:
                    resolved[hashed] = (head + voice_tail(path, suffix).decode(), lang)
        if progress and i % _PROGRESS_EVERY_PATHS == 0:
            progress(i, total, f"Resolving voice names ({len(resolved)} found)")
    extrapolated = extrapolate_voice_names(live, paths, external_ids, resolved, progress)
    if progress:
        progress(total, total, f"Voice names: {len(resolved)} resolved ({extrapolated} extrapolated)")
    return resolved


# The heads that hit an external on an even sample of the paths, all of them when none hits.
def _live_heads(heads, paths, external_ids):
    head_states = {head: fnv64_feed(head.lower().encode()) for head, _lang, _gendered in heads}
    step = max(1, len(paths) // _HEAD_SAMPLE)
    sample = paths[::step][:_HEAD_SAMPLE]
    live = []
    for head, lang, gendered in heads:
        state = head_states[head]
        for path in sample:
            path_low = path.lower()
            if any(fnv64_feed(voice_tail(path_low, suffix), state) in external_ids
                   for suffix in _suffixes(gendered)):
                live.append((head, lang, gendered, state))
                break
    if not live:
        live = [(head, lang, gendered, head_states[head]) for head, lang, gendered in heads]
    return live


# GI's two-digit takes and HSR's three-digit ids are kept apart, since their caps differ.
def _take_families(voice_paths):
    families = defaultdict(lambda: defaultdict(set))
    for path in voice_paths:
        take = _TAKE_SUFFIX_RE.search(path)
        if take:
            shape = (len(take.group(1)), take.group(2) or "")
            families[path[:take.start()]][shape].add(int(take.group(1)))
    return families


# The line patterns of the speaker folders (vo_heroine_<pattern>) and each speaker's path prefix.
def _speaker_patterns(voice_paths):
    speaker_prefixes = {}
    patterns = set()
    for path in voice_paths:
        folder, leaf = split_voice_path(path)
        if not folder:
            continue
        speaker = speaker_of_folder(folder)
        if speaker and leaf.lower().startswith("vo_" + speaker + "_"):
            cut = len(speaker) + 4
            patterns.add(leaf[cut:])
            speaker_prefixes[folder] = path[:len(folder) + 1] + leaf[:cut]
    return speaker_prefixes, patterns


def _take_caps(families):
    take_cap = defaultdict(int)
    for base, shapes in families.items():
        folder = split_voice_path(base)[0]
        for shape, numbers in shapes.items():
            key = (folder, shape)
            take_cap[key] = max(take_cap[key], max(numbers))
    for key in take_cap:
        digits = key[1][0]
        take_cap[key] = min(max(_MIN_TAKES_PROBED, take_cap[key] + _TAKE_PROBE_MARGIN), 10 ** digits - 1)
    return take_cap


def _take_tails(shape, suffixes):
    digits, extension = shape
    out = []
    for number in range(0, 10 ** digits):
        take = "_%0*d%s" % (digits, number, extension)
        out.append([(voice_tail(take, suffix).decode(), voice_tail(take, suffix)) for suffix in suffixes])
    return out


# Every speaker's line pattern on every other speaker, and takes renumbered up to the family maximum.
# A candidate is kept only when it hits an id actually present, so there are no false positives.
def extrapolate_voice_names(live, voice_paths, external_ids, resolved, progress=None):
    missing = set(external_ids) - set(resolved)
    if not missing or not live:
        return 0
    speaker_prefixes, patterns = _speaker_patterns(voice_paths)
    families = _take_families(voice_paths)
    if not patterns and not families:
        return 0
    take_cap = _take_caps(families)

    found = 0
    budget = _MAX_EXTRAPOLATED_CANDIDATES
    steps = len(live) * (len(speaker_prefixes) + len(families))
    done = 0
    for head, lang, gendered, state in live:
        suffixes = _suffixes(gendered)
        pattern_tails = [(voice_tail(pattern, suffix).decode(), voice_tail(pattern.lower(), suffix))
                         for pattern in sorted(patterns) for suffix in suffixes]
        take_tails = {}
        for _folder, shape in take_cap:
            if shape not in take_tails:
                take_tails[shape] = _take_tails(shape, suffixes)

        for prefix in speaker_prefixes.values():
            if budget <= 0 or not missing:
                break
            prefix_state = fnv64_feed(prefix.lower().encode(), state)
            for name, tail in pattern_tails:
                budget -= 1
                hashed = fnv64_feed(tail, prefix_state)
                if hashed in missing:
                    missing.discard(hashed)
                    resolved[hashed] = (head + prefix + name, lang)
                    found += 1
            done += 1
            if progress and done % 200 == 0:
                progress(done, steps, f"Extrapolating voice names ({found} found)")

        for base, shapes in families.items():
            if budget <= 0 or not missing:
                break
            folder = split_voice_path(base)[0]
            base_state = fnv64_feed(base.lower().encode(), state)
            for shape, numbers in shapes.items():
                for number in range(1, take_cap[(folder, shape)] + 1):
                    if number in numbers:
                        continue
                    for name, tail in take_tails[shape][number]:
                        budget -= 1
                        hashed = fnv64_feed(tail, base_state)
                        if hashed in missing:
                            missing.discard(hashed)
                            resolved[hashed] = (head + base + name, lang)
                            found += 1
            done += 1
            if progress and done % 500 == 0:
                progress(done, steps, f"Extrapolating voice names ({found} found)")
    if progress:
        progress(steps, steps, f"Extrapolated voice names: {found}")
    return found
