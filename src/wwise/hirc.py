# Readers for the HIRC objects the indexer needs.
# Layouts shift across Wwise versions, so most readers probe and validate instead of trusting offsets.

import struct
from typing import NamedTuple

HIRC_STATE = 0x01
HIRC_SOUND = 0x02
HIRC_ACTION = 0x03
HIRC_EVENT = 0x04
HIRC_RANSEQ = 0x05
HIRC_SWITCH = 0x06
HIRC_ACTORMIXER = 0x07
HIRC_AUDIO_BUS = 0x08
HIRC_LAYER = 0x09
HIRC_MUSIC_SEGMENT = 0x0A
HIRC_MUSIC_TRACK = 0x0B
HIRC_MUSIC_SWITCH = 0x0C
HIRC_MUSIC_RANSEQ = 0x0D
HIRC_ATTENUATION = 0x0E
HIRC_DIALOGUE_EVENT = 0x0F
HIRC_FX_SHARESET = 0x10
HIRC_FX_CUSTOM = 0x11
HIRC_AUX_BUS = 0x12
HIRC_AUDIO_DEVICE = 0x15

# Objects whose id is the hash of their name, unlike containers and sounds.
NAMED_OBJECT_TYPES = {
    HIRC_STATE: "State",
    HIRC_AUDIO_BUS: "Bus",
    HIRC_AUX_BUS: "Aux bus",
    HIRC_FX_SHARESET: "FX share set",
    HIRC_FX_CUSTOM: "FX",
    HIRC_AUDIO_DEVICE: "Audio device",
    HIRC_ATTENUATION: "Attenuation",
}
CONTAINER_TYPES = {HIRC_RANSEQ, HIRC_SWITCH, HIRC_ACTORMIXER, HIRC_LAYER}
MUSIC_NODE_TYPES = {HIRC_MUSIC_SEGMENT, HIRC_MUSIC_SWITCH, HIRC_MUSIC_RANSEQ}

ACTION_SET_STATE = 18
ACTION_SET_GAME_PARAM = 19
ACTION_RESET_GAME_PARAM = 20
ACTION_SET_SWITCH = 25
ACTION_TRIGGER = 29

ACTION_TYPE_NAMES = {
    1: "Stop", 2: "Pause", 3: "Resume", 4: "Play", 5: "PlayAndContinue",
    6: "Mute", 7: "Unmute", 8: "SetPitch", 9: "ResetPitch", 10: "SetVolume",
    11: "ResetVolume", 12: "SetBusVolume", 13: "ResetBusVolume",
    14: "SetLowPassFilter", 15: "ResetLowPassFilter", 16: "UseState",
    17: "UnuseState", 18: "SetState", 19: "SetGameParameter",
    20: "ResetGameParameter", 21: "StopEvent", 22: "PauseEvent",
    23: "ResumeEvent", 24: "Duck", 25: "SetSwitch", 26: "SetBypassEffect",
    27: "ResetBypassEffect", 28: "Break", 29: "Trigger", 30: "Seek",
    31: "Release", 32: "SetHighPassFilter", 33: "PlayEvent",
    34: "ResetPlaylist", 48: "ResetHighPassFilter",
}

# AkBankSourceData: pluginID(4) streamType(1) sourceID(4) mediaSize(4) sourceBits(1).
SOURCE_DATA_SIZE = 14
# AkTrackSrcInfo: trackID(4) sourceID(4) eventID(4) and four doubles.
TRACK_SRC_INFO_SIZE = 44
# Counts beyond these limits mean a field is not what the parser assumes.
_MAX_EVENT_ACTIONS = 1000
_MAX_PARENT_FX = 32
_MAX_TRACK_SOURCES = 100
_MAX_TRACK_PLAYLIST = 500
_MAX_TRACK_CLIPS = 200
_MAX_CLIP_POINTS = 10000
_MAX_TREE_DEPTH_DIALOGUE = 16
_MAX_TREE_DEPTH_MUSIC = 8
_MAX_SWITCH_ENTRIES = 4096
# A tree node is key(4), audio node or child index and count(4), weight(2), probability(2).
TREE_NODE_SIZE = 12
# A candidate music tree needs this share of its leaves pointing at known objects.
_MUSIC_TREE_MIN_VALID_LEAVES = 0.7
# A SwitchCntr ends with params of one of these sizes.
SWITCH_PARAM_SIZES = (14, 18)

# Offset of DirectParentID in NodeBaseParams without FX, and the size of an FX entry, per layout.
# calibrate_parent_variant picks the layout that fits the scanned data.
PARENT_VARIANTS = {"bus7": (7, 7), "bus6": (6, 7), "flat2": (2, 6)}


class Action(NamedTuple):
    type: int
    target: int


# Group type 0 is a switch group and 1 a state group; assignments map values to child nodes.
class SwitchContainer(NamedTuple):
    group_type: int
    group_id: int
    assignments: dict


# One group per level, and each leaf is (one key per level, selected audio node or 0).
class DecisionTree(NamedTuple):
    groups: tuple
    group_types: tuple
    leaves: list


# Yields (type, id, payload start, end), the payload starting right after the id.
def iter_hirc_objects(raw):
    if len(raw) < 4:
        return
    count = struct.unpack_from("<I", raw, 0)[0]
    pos = 4
    seen = 0
    total = len(raw)
    while seen < count and pos + 9 <= total:
        otype = raw[pos]
        osize = struct.unpack_from("<I", raw, pos + 1)[0]
        start = pos + 5
        end = start + osize
        if end > total or osize < 4:
            break
        oid = struct.unpack_from("<I", raw, start)[0]
        yield otype, oid, start + 4, end
        pos = end
        seen += 1


# The action count is a varint or a u32 depending on the Wwise version.
# Whichever one closes exactly on the object end is taken.
def parse_event_actions(raw, p, end):
    if p >= end:
        return []
    q = p
    count = 0
    shift = 0
    complete = False
    while q < end and shift < 32:
        byte = raw[q]
        count |= (byte & 0x7F) << shift
        q += 1
        if not byte & 0x80:
            complete = True
            break
        shift += 7
    if complete and q + count * 4 == end and count < _MAX_EVENT_ACTIONS:
        return list(struct.unpack_from(f"<{count}I", raw, q)) if count else []
    if p + 4 <= end:
        count = struct.unpack_from("<I", raw, p)[0]
        if p + 4 + count * 4 == end and count < _MAX_EVENT_ACTIONS:
            return list(struct.unpack_from(f"<{count}I", raw, p + 4))
    return []


def parse_action(raw, p, end):
    if p + 6 > end:
        return None
    return Action(raw[p + 1], struct.unpack_from("<I", raw, p + 2)[0])


# SetState and SetSwitch end with the group and value ids, after the property and modifier lists.
def parse_action_syncs(raw, p, end, action):
    if action.type == ACTION_TRIGGER:
        return [(action.target, "Trigger")] if action.target else []
    if action.type in (ACTION_SET_GAME_PARAM, ACTION_RESET_GAME_PARAM):
        return [(action.target, "Game parameter")] if action.target else []
    if action.type not in (ACTION_SET_STATE, ACTION_SET_SWITCH):
        return []
    q = p + 7
    if q >= end:
        return []
    q += 1 + raw[q] * 5
    if q >= end:
        return []
    q += 1 + raw[q] * 9
    if q + 8 > end:
        return []
    group, value = struct.unpack_from("<II", raw, q)
    if action.type == ACTION_SET_STATE:
        return [(group, "State group"), (value, "State")]
    return [(group, "Switch group"), (value, "Switch")]


# Returns (wem id, offset of the NodeBaseParams), the offset None when a plugin block hides it.
def parse_sound(raw, p, end):
    if p + SOURCE_DATA_SIZE > end:
        return None
    plugin = struct.unpack_from("<I", raw, p)[0]
    source_id = struct.unpack_from("<I", raw, p + 5)[0]
    q = p + SOURCE_DATA_SIZE
    if plugin & 0xF == 2:
        if q + 4 > end:
            return source_id, None
        params_size = struct.unpack_from("<I", raw, q)[0]
        q += 4
        if params_size < 0x10000:
            q += params_size
    return source_id, q


def parse_parent(raw, p, end, variant):
    base_offset, fx_size = PARENT_VARIANTS[variant]
    if p + 2 > end:
        return None
    num_fx = raw[p + 1]
    offset = p + base_offset
    if num_fx:
        if num_fx > _MAX_PARENT_FX:
            return None
        offset += 1 + num_fx * fx_size
    if offset + 4 > end:
        return None
    return struct.unpack_from("<I", raw, offset)[0]


# The wems come from the sources and the playlist, and the parent sits past the clip automation.
def parse_music_track(raw, p, end, variant):
    if p + 5 > end:
        return None
    num_sources = struct.unpack_from("<I", raw, p + 1)[0]
    if num_sources > _MAX_TRACK_SOURCES:
        return None
    q = p + 5
    if q + num_sources * SOURCE_DATA_SIZE > end:
        return None
    sources = set()
    for _ in range(num_sources):
        sources.add(struct.unpack_from("<I", raw, q + 5)[0])
        q += SOURCE_DATA_SIZE
    parent = None
    if q + 4 <= end:
        num_playlist = struct.unpack_from("<I", raw, q)[0]
        q += 4
        if num_playlist <= _MAX_TRACK_PLAYLIST and q + num_playlist * TRACK_SRC_INFO_SIZE <= end:
            for _ in range(num_playlist):
                sources.add(struct.unpack_from("<I", raw, q + 4)[0])
                q += TRACK_SRC_INFO_SIZE
            parent = _music_track_parent(raw, q, end, variant)
    # A zero source is an empty slot, never a wem.
    sources.discard(0)
    return sources, parent


def _music_track_parent(raw, q, end, variant):
    try:
        q += 4
        num_clips = struct.unpack_from("<I", raw, q)[0]
        q += 4
        if num_clips > _MAX_TRACK_CLIPS:
            return None
        for _ in range(num_clips):
            q += 8
            num_points = struct.unpack_from("<I", raw, q)[0]
            q += 4
            if num_points > _MAX_CLIP_POINTS:
                return None
            q += 12 * num_points
        q += 5
        return parse_parent(raw, q, end, variant)
    except Exception:
        return None


def calibrate_parent_variant(samples, object_ids, kind):
    best, best_score = "bus7", -1.0
    for variant in PARENT_VARIANTS:
        good = total = 0
        for raw, p, end in samples:
            if kind == "music":
                # Music nodes start with a flags byte before their NodeBaseParams.
                p = p + 1
            parent = parse_parent(raw, p, end, variant)
            if parent is None:
                continue
            total += 1
            if parent == 0 or parent in object_ids:
                good += 1
        score = good / total if total else 0.0
        if score > best_score:
            best, best_score = variant, score
    return best, best_score


def parse_dialogue_children(raw, p, end):
    if p + 10 > end:
        return []
    depth = struct.unpack_from("<I", raw, p + 1)[0]
    if depth > _MAX_TREE_DEPTH_DIALOGUE:
        return []
    q = p + 5 + depth * 5
    if q + 5 > end:
        return []
    tree_size = struct.unpack_from("<I", raw, q)[0]
    q += 5
    if tree_size > end - q + 4 or tree_size % TREE_NODE_SIZE:
        return []
    return [struct.unpack_from("<I", raw, q + i * TREE_NODE_SIZE + 4)[0]
            for i in range(tree_size // TREE_NODE_SIZE)]


# Children always sit forward in the node array, so a union that cannot point there is an audio node.
# There is no cap on the child count: a big switch's default branch has hundreds of children.
def decode_decision_tree(tree, depth):
    count = len(tree) // TREE_NODE_SIZE
    if not count:
        return None
    nodes = [struct.unpack_from("<IIHH", tree, i * TREE_NODE_SIZE) for i in range(count)]
    leaves = []

    def visit(idx, level, path):
        key, union = nodes[idx][0], nodes[idx][1]
        child_idx, child_count = union & 0xFFFF, union >> 16
        if level >= depth or child_count == 0 or child_idx <= idx or child_idx + child_count > count:
            leaves.append((tuple(path + [key]), union))
            return
        for k in range(child_count):
            visit(child_idx + k, level + 1, path + [key])

    root_union = nodes[0][1]
    root_child_idx, root_child_count = root_union & 0xFFFF, root_union >> 16
    if not root_child_count or root_child_idx + root_child_count > count:
        return None
    for k in range(root_child_count):
        visit(root_child_idx + k, 1, [])
    return leaves


# A DialogueEvent starts with probability(1), depth(4), one group entry(5) per level, tree size(4), mode(1).
def parse_dialogue_tree(raw, p, end):
    if p + 10 > end:
        return None
    depth = struct.unpack_from("<I", raw, p + 1)[0]
    if not depth or depth > _MAX_TREE_DEPTH_DIALOGUE:
        return None
    q = p + 5
    if q + depth * 5 + 5 > end:
        return None
    groups = struct.unpack_from(f"<{depth}I", raw, q)
    group_types = tuple(raw[q + 4 * depth: q + 5 * depth])
    q += depth * 5
    tree_size = struct.unpack_from("<I", raw, q)[0]
    q += 5
    if tree_size % TREE_NODE_SIZE or q + tree_size > end:
        return None
    leaves = decode_decision_tree(raw[q:q + tree_size], depth)
    if not leaves:
        return None
    return DecisionTree(groups, group_types, leaves)


# The tree ends a MusicSwitchCntr whose front shifts across Wwise versions, so it is probed from the end.
# A candidate must match its depth and point at known objects.
def parse_music_switch_tree(raw, p, end, object_ids):
    for tree_size in range(TREE_NODE_SIZE, end - p - 9, TREE_NODE_SIZE):
        size_offset = end - tree_size - 5
        if size_offset - 9 < p:
            break
        if struct.unpack_from("<I", raw, size_offset)[0] != tree_size:
            continue
        for depth in range(1, _MAX_TREE_DEPTH_MUSIC + 1):
            depth_offset = size_offset - 5 * depth - 4
            if depth_offset < p:
                break
            if struct.unpack_from("<I", raw, depth_offset)[0] != depth:
                continue
            groups = struct.unpack_from(f"<{depth}I", raw, depth_offset + 4)
            group_types = tuple(raw[depth_offset + 4 + 4 * depth: depth_offset + 4 + 5 * depth])
            leaves = decode_decision_tree(raw[size_offset + 5: size_offset + 5 + tree_size], depth)
            if not leaves:
                continue
            valid = sum(1 for _keys, target in leaves if target == 0 or target in object_ids)
            if valid < len(leaves) * _MUSIC_TREE_MIN_VALID_LEAVES:
                continue
            return DecisionTree(groups, group_types, leaves)
    return None


# The NodeBaseParams size varies, so every offset is probed for a tail that closes on the object end.
def parse_switch_container(raw, p, end, object_ids):
    for q in range(p, end - 22):
        group_type = raw[q]
        if group_type > 1 or raw[q + 9] > 1:
            continue
        group_id = struct.unpack_from("<I", raw, q + 1)[0]
        if not group_id:
            continue
        r = q + 10
        num_children = struct.unpack_from("<I", raw, r)[0]
        r += 4
        if num_children > _MAX_SWITCH_ENTRIES or r + num_children * 4 + 4 > end:
            continue
        children = struct.unpack_from(f"<{num_children}I", raw, r)
        r += num_children * 4
        if any(child not in object_ids for child in children):
            continue
        assignments = _read_switch_assignments(raw, r, end)
        if assignments is None:
            continue
        assignments, r = assignments
        num_params = struct.unpack_from("<I", raw, r)[0]
        r += 4
        if not any(r + num_params * size == end for size in SWITCH_PARAM_SIZES):
            continue
        return SwitchContainer(group_type, group_id, assignments)
    return None


# Some banks assign nodes the child list leaves out.
def _read_switch_assignments(raw, r, end):
    num_switches = struct.unpack_from("<I", raw, r)[0]
    r += 4
    if num_switches > _MAX_SWITCH_ENTRIES:
        return None
    assignments = {}
    for _ in range(num_switches):
        if r + 8 > end:
            return None
        switch_id, num_nodes = struct.unpack_from("<II", raw, r)
        r += 8
        if num_nodes > _MAX_SWITCH_ENTRIES or r + num_nodes * 4 > end:
            return None
        assignments.setdefault(switch_id, set()).update(struct.unpack_from(f"<{num_nodes}I", raw, r))
        r += num_nodes * 4
    if r + 4 > end:
        return None
    return assignments, r
