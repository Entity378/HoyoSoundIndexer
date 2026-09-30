# The naming pass shared by the CLI and the GUI, where order matters.
# Dedupe keeps the first row of an id, and each cracker feeds on what the steps before it resolved.

from typing import NamedTuple

from src.characters import label_characters
from src.cracking.context import crack_context_names
from src.cracking.families import crack_event_families
from src.cracking.syncs import crack_sync_names
from src.harvest import harvest_local_state_names
from src.matching import (
    attach_sync_wems, dedupe_matches, match_names, match_online_labels, prune_unmatched,
)
from src.model import Kind, NameMatch
from src.music import music_branch_matches
from src.vocabulary import KNOWN_SYNC_NAMES
from src.voice.from_harvest import crack_vo_names
from src.voice.from_online import resolve_online_voices


# Raised between two steps once cancel() turns true.
class Cancelled(Exception):
    pass


class ResolveResult(NamedTuple):
    matches: list
    unmatched: list
    counts: dict


def _check(cancel):
    if cancel and cancel():
        raise Cancelled()


# export is the ExportedNames of the names file, and online the game's OnlineData.
# With crack off the harvest and the crackers are skipped, since a loaded export restores names by id.
def resolve_all_matches(index, names, scan_root, export=None, online=None, harvested_voice=None,
                        progress=None, cancel=None, crack=True):
    counts = {}
    matches, unmatched = match_names(names, index, progress=progress) if names else ([], [])
    _check(cancel)

    state_names = _state_candidates(scan_root, online, crack, counts, progress, cancel)
    _check(cancel)
    state_matches, _unmatched_states = match_names(state_names, index, progress=progress)
    matches.extend(state_matches)
    if crack:
        if progress:
            progress(0, 1, "Cracking unnamed tags...")
        cracked = crack_sync_names(index, state_names, matches, progress=progress)
        matches.extend(cracked)
        counts["cracked_tags"] = len(cracked)
    _check(cancel)

    # Restored rows go first, since an export records a resolution that already happened.
    if export is not None:
        matches = export.matches(index) + matches

    voices = _voice_matches(index, online, harvested_voice, progress)
    if voices is not None:
        matches.extend(voices)
        counts["voices"] = len(voices)
    _check(cancel)

    if online is not None and online.id_names:
        labels = match_online_labels(online.id_names, index)
        matches.extend(labels)
        counts["labels"] = len(labels)

    if crack:
        family_matches = crack_event_families(index, matches, progress=progress, candidates=state_names)
        matches.extend(family_matches)
        counts["family_events"] = len(family_matches)
        _check(cancel)
        if progress:
            progress(0, 1, "Cracking tags from context...")
        context_cracked = crack_context_names(index, matches)
        matches.extend(context_cracked)
        counts["context_tags"] = len(context_cracked)
        _check(cancel)

    branches = music_branch_matches(index, matches)
    matches.extend(branches)
    counts["music_branches"] = len(branches)

    # The online roster lists avatars; an export's is exact on its own data and no ground to guess from.
    online_roster = online.avatar_names if online is not None else {}
    avatar_names = online_roster or (export.avatar_names() if export is not None else {})
    matches.extend(label_characters(index, matches, avatar_names, by_prefix=bool(online_roster)))
    counts["characters"] = len(index.wem_characters)

    attach_sync_wems(index, matches)
    matches = dedupe_matches(matches)
    return ResolveResult(matches, prune_unmatched(matches, unmatched), counts)


def _state_candidates(scan_root, online, crack, counts, progress, cancel):
    if not crack:
        return sorted(KNOWN_SYNC_NAMES)
    local = harvest_local_state_names(scan_root, progress=progress, cancel=cancel)
    counts["local_state_candidates"] = len(local)
    online_candidates = online.state_candidates if online is not None else []
    return sorted(set(local) | set(online_candidates) | set(KNOWN_SYNC_NAMES))


# The online paths win when the source lists them (GI, HSR), being verified and complete.
# ZZZ lists none, so ticking Online names must not throw the harvested voice data away.
def _voice_matches(index, online, harvested_voice, progress):
    if not index.external_locations:
        return None
    external_ids = index.external_locations.keys()
    if online is not None and online.voice_paths:
        resolved = resolve_online_voices(online.game, online.voice_paths, external_ids, progress=progress)
    elif harvested_voice is not None:
        resolved = crack_vo_names(harvested_voice.prefixes, harvested_voice.sources, external_ids,
                                  progress=progress)
    elif online is not None:
        resolved = {}
    else:
        return None
    return [NameMatch(name, Kind.EXTERNAL, [ext_id], ext_id) for ext_id, (name, _lang) in resolved.items()]
