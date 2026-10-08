import json
from dataclasses import dataclass, field
from datetime import datetime

from src.config import online_cache_file
from src.online.fetch import (
    fetch_audio_labels, fetch_avatar_roster, fetch_event_names, fetch_state_candidates,
    fetch_voice_paths, fetch_zzz_music,
)
from src.online.sources import source_for


# Old caches lack state_candidates and the roster until the next Update.
@dataclass
class OnlineData:
    game: str
    label: str = ""
    updated: str = ""
    voice_paths: list = field(default_factory=list)
    names: list = field(default_factory=list)
    id_names: dict = field(default_factory=dict)
    state_candidates: list = field(default_factory=list)
    roster: dict = field(default_factory=dict)

    @classmethod
    def from_meta(cls, game, meta):
        return cls(game=game,
                   label=meta.get("label", ""),
                   updated=meta.get("updated", ""),
                   voice_paths=meta.get("voice_paths", []),
                   names=meta.get("names", []),
                   id_names=meta.get("id_names", {}),
                   state_candidates=meta.get("state_candidates", []),
                   roster=meta.get("avatar_names", {}))

    def to_meta(self):
        return {"game": self.game, "label": self.label, "updated": self.updated,
                "voice_count": len(self.voice_paths), "names_count": len(self.names),
                "labels_count": len(self.id_names), "candidates_count": len(self.state_candidates),
                "avatar_count": len(self.roster),
                "voice_paths": self.voice_paths, "names": self.names, "id_names": self.id_names,
                "state_candidates": self.state_candidates, "avatar_names": self.roster}


def has_online_cache(game):
    return online_cache_file(game).exists()


def read_online_cache(game):
    try:
        meta = json.loads(online_cache_file(game).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(meta, dict) or "names" not in meta:
        return None
    return OnlineData.from_meta(game, meta)


# From the cache unless forced, and config is the settings dict for a source overridden there.
def load_online_data(game, config=None, force=False, progress=None):
    source = source_for(game, config)
    if not source:
        raise RuntimeError(f"no online data source configured for {game}")
    if not force:
        cached = read_online_cache(game)
        if cached is not None:
            return cached
    data = download_online_data(game, source, progress)
    cache = online_cache_file(game)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data.to_meta()), encoding="utf-8")
    return data


def download_online_data(game, source, progress=None):
    voice_paths = sorted(set(fetch_voice_paths(game, source, progress)))
    names, id_names, roster = set(), {}, {}
    if source.get("music_cfg"):
        music_names, music_ids, roster = fetch_zzz_music(source, progress)
        names.update(music_names)
        id_names.update(music_ids)
    if source.get("audio_subtree"):
        audio_names, audio_ids = fetch_audio_labels(source, progress)
        names.update(audio_names)
        id_names.update(audio_ids)
    if source.get("event_files") or source.get("event_subtrees"):
        names.update(fetch_event_names(source, progress))
    # A moved table or a missing text map costs the roster only, never the rest of the update.
    if source.get("avatar_textmap"):
        try:
            roster = fetch_avatar_roster(source, progress)
        except Exception:
            roster = {}
    state_candidates = fetch_state_candidates(source, progress)
    return OnlineData(game=game, label=source.get("label", ""),
                      updated=datetime.now().strftime("%Y-%m-%d %H:%M"),
                      voice_paths=voice_paths, names=sorted(names), id_names=id_names,
                      state_candidates=state_candidates, roster=roster)
