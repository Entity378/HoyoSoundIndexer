from dataclasses import dataclass
from pathlib import Path


# The kind of a resolved name, spelled as the Type column and the json export show it.
# The spelling is part of the export format, so a reload depends on it.
class Kind:
    EVENT = "Event"
    DIALOGUE_EVENT = "DialogueEvent"
    BANK = "Bank"
    # A wem whose own id is the hash of the name.
    DIRECT_WEM = "Direct WEM"
    # A streamed voice file, named after its path with a 64-bit id.
    EXTERNAL = "External"
    STATE = "State"
    SWITCH = "Switch"
    STATE_GROUP = "State group"
    SWITCH_GROUP = "Switch group"
    TRIGGER = "Trigger"
    GAME_PARAMETER = "Game parameter"
    # A music-switch leaf named after the states on its path.
    MUSIC_BRANCH = "Music branch"
    # Online labels for ids that are not name hashes: ZZZ track titles, GI music segments.
    MUSIC = "Music"
    MUSIC_SEGMENT = "MusicSegment"
    # The GUI row of the wems only a character tag reaches.
    TAGGED = "Tagged"

    EVENTS = (EVENT, DIALOGUE_EVENT)
    GROUPS = (STATE_GROUP, SWITCH_GROUP)
    VALUES = (STATE, SWITCH)
    SYNCS = (STATE, SWITCH, STATE_GROUP, SWITCH_GROUP)


# hash_id is the id the name matched, and wem_ids the wems it plays or selects.
@dataclass(eq=False, slots=True)
class NameMatch:
    name: str
    kind: str
    wem_ids: list
    hash_id: int = 0


# A "pck" location is a whole file of a package table, a "bnk" one a copy inside a bank.
# The bank copy is often only the short prefetch stub of a streamed file.
@dataclass(eq=False, slots=True)
class WemLocation:
    file_path: str
    bnk_id: int
    lang: str
    offset: int
    size: int
    kind: str

    def label(self):
        text = Path(self.file_path).name
        if "persistent" in self.file_path.lower():
            text = "Persistent/" + text
        if self.kind == "bnk":
            text += f" > bnk {self.bnk_id}"
        return text
