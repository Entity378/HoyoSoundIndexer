# Matches names (txt/json/harvest/online) against the Wwise content of a folder: FNV-32 events resolved down to wems, FNV-64 externals.
# GUI: python hoyo_sound_indexer.py | CLI: --scan <folder> --names <txt>

import argparse
import ctypes
import io
import json
import multiprocessing
import os
import re
import ssl
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from shutil import which

import certifi
from blkdec import iter_blk_blocks, oodle_available
from PyQt6.QtCore import QPoint, QRectF, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QRadioButton, QSlider, QStyle, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

APP_NAME = "Hoyo Sound Indexer"


def _config_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HoyoSoundIndexer"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "HoyoSoundIndexer"


CONFIG_FILE = _config_dir() / "config.json"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


HIRC_SOUND = 0x02
HIRC_ACTION = 0x03
HIRC_EVENT = 0x04
HIRC_RANSEQ = 0x05
HIRC_SWITCH = 0x06
HIRC_ACTORMIXER = 0x07
HIRC_LAYER = 0x09
HIRC_MUSIC_SEGMENT = 0x0A
HIRC_MUSIC_TRACK = 0x0B
HIRC_MUSIC_SWITCH = 0x0C
HIRC_MUSIC_RANSEQ = 0x0D
HIRC_DIALOGUE_EVENT = 0x0F

HIRC_STATE = 0x01
HIRC_AUDIO_BUS = 0x08
HIRC_ATTENUATION = 0x0E
HIRC_FX_SHARESET = 0x10
HIRC_FX_CUSTOM = 0x11
HIRC_AUX_BUS = 0x12
HIRC_AUDIO_DEVICE = 0x15

# Objects whose ID is the hash of the name (containers/Sounds have arbitrary ids instead).
NAMED_OBJECT_TYPES = {
    HIRC_STATE: "State",
    HIRC_AUDIO_BUS: "Bus",
    HIRC_AUX_BUS: "Aux bus",
    HIRC_FX_SHARESET: "FX share set",
    HIRC_FX_CUSTOM: "FX",
    HIRC_AUDIO_DEVICE: "Audio device",
    HIRC_ATTENUATION: "Attenuation",
}

ACTION_SET_STATE = 18
ACTION_SET_GAME_PARAM = 19
ACTION_RESET_GAME_PARAM = 20
ACTION_SET_SWITCH = 25
ACTION_TRIGGER = 29

CONTAINER_TYPES = {HIRC_RANSEQ, HIRC_SWITCH, HIRC_ACTORMIXER, HIRC_LAYER}
MUSIC_NODE_TYPES = {HIRC_MUSIC_SEGMENT, HIRC_MUSIC_SWITCH, HIRC_MUSIC_RANSEQ}

# pluginID(4) + streamType(1) + sourceID(4) + mediaSize(4) + sourceBits(1)
SOURCE_DATA_SIZE = 14
# trackID(4) + sourceID(4) + eventID(4) + 4 doubles
TRACK_SRC_INFO_SIZE = 44

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


def fnv1_32(name):
    h = 0x811C9DC5
    for b in name.lower().encode("utf-8"):
        h = (h * 0x01000193) & 0xFFFFFFFF
        h ^= b
    return h


def fnv1_64(name):
    h = 0xCBF29CE484222325
    for b in name.lower().encode("utf-8"):
        h = (h * 0x00000100000001B3) & 0xFFFFFFFFFFFFFFFF
        h ^= b
    return h


# ---------------------------------------------------------------- PCK / BNK parsing

class WemLocation:
    __slots__ = ("pck_path", "bnk_id", "lang", "offset", "size", "kind")

    def __init__(self, pck_path, bnk_id, lang, offset, size, kind):
        self.pck_path = pck_path
        self.bnk_id = bnk_id
        self.lang = lang
        self.offset = offset
        self.size = size
        self.kind = kind

    def label(self):
        src = Path(self.pck_path).name
        if "persistent" in self.pck_path.lower():
            src = "Persistent/" + src
        if self.kind == "bnk":
            src += f" > bnk {self.bnk_id}"
        return src


def parse_pck(pck_path):
    lang_map = {0: "sfx", 1: "english", 2: "chinese", 3: "japanese", 4: "korean"}
    banks, sounds, externals = [], [], []

    with open(pck_path, "rb") as f:
        if f.read(4) != b"AKPK":
            raise ValueError("not AKPK")
        header_size = struct.unpack("<I", f.read(4))[0]
        # version/flags
        f.read(4)
        sec1 = struct.unpack("<I", f.read(4))[0]
        sec2 = struct.unpack("<I", f.read(4))[0]
        sec3 = struct.unpack("<I", f.read(4))[0]
        if sec1 + sec2 + sec3 + 0x10 < header_size:
            sec4 = struct.unpack("<I", f.read(4))[0]
        else:
            sec4 = 0

        strings_offset = f.tell()
        if sec1 > 0:
            lang_count = struct.unpack("<I", f.read(4))[0]
            defs = []
            for _ in range(lang_count):
                off, lid = struct.unpack("<II", f.read(8))
                defs.append((lid, strings_offset + off))
            for lid, off in defs:
                pos = f.tell()
                f.seek(off)
                name = ""
                while True:
                    ch = f.read(2)
                    if len(ch) < 2 or ch == b"\x00\x00":
                        break
                    try:
                        name += ch.decode("utf-16-le")
                    except Exception:
                        break
                if name:
                    lang_map[lid] = name.lower()
                f.seek(pos)
        f.seek(strings_offset + sec1)

        def read_table(section_size, wide_id):
            out = []
            if section_size == 0:
                return out
            count = struct.unpack("<I", f.read(4))[0]
            for _ in range(count):
                if wide_id:
                    fid = struct.unpack("<Q", f.read(8))[0]
                else:
                    fid = struct.unpack("<I", f.read(4))[0]
                blocksize, size, offset_block, lang_id = struct.unpack("<IIII", f.read(16))
                offset = offset_block * blocksize if blocksize else offset_block
                out.append({"id": fid, "offset": offset, "size": size, "lang_id": lang_id})
            return out

        banks = read_table(sec2, False)
        sounds = read_table(sec3, False)
        externals = read_table(sec4, True) if sec4 > 0 else []

    return lang_map, banks, sounds, externals


class BnkMeta:
    __slots__ = ("pck_path", "bank_id", "lang", "version", "bkhd_id",
                 "hirc_offset", "hirc_size", "didx_wems")

    def __init__(self):
        self.pck_path = None
        self.bank_id = 0
        self.lang = "?"
        self.version = 0
        self.bkhd_id = 0
        # absolute offset in the file
        self.hirc_offset = 0
        self.hirc_size = 0
        # (wem_id, abs_offset, size)
        self.didx_wems = []


def scan_bnk_chunks(f, base_offset, size, meta):
    end = base_offset + size
    pos = base_offset
    didx = []
    data_payload = None
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            break
        tag = head[:4]
        clen = struct.unpack("<I", head[4:])[0]
        payload = pos + 8
        if payload + clen > end + 16:
            break
        if tag == b"BKHD":
            hdr = f.read(min(12, clen))
            if len(hdr) >= 12:
                meta.version, meta.bkhd_id, _lang = struct.unpack("<III", hdr)
        elif tag == b"DIDX":
            raw = f.read(clen)
            for i in range(len(raw) // 12):
                wid, off, wsize = struct.unpack_from("<III", raw, i * 12)
                didx.append((wid, off, wsize))
        elif tag == b"DATA":
            data_payload = payload
        elif tag == b"HIRC":
            meta.hirc_offset = payload
            meta.hirc_size = clen
        pos = payload + clen
    if data_payload is not None:
        for wid, off, wsize in didx:
            meta.didx_wems.append((wid, data_payload + off, wsize))


# ---------------------------------------------------------------- HIRC extraction

def iter_hirc_objects(raw):
    if len(raw) < 4:
        return
    count = struct.unpack_from("<I", raw, 0)[0]
    pos = 4
    n = 0
    total = len(raw)
    while n < count and pos + 9 <= total:
        otype = raw[pos]
        osize = struct.unpack_from("<I", raw, pos + 1)[0]
        start = pos + 5
        end = start + osize
        if end > total or osize < 4:
            break
        oid = struct.unpack_from("<I", raw, start)[0]
        yield otype, oid, start + 4, end
        pos = end
        n += 1


# After the id: count (varint or u32, decided from the data) + action ids.
def parse_event_actions(raw, p, end):
    if p >= end:
        return []
    # 7-bit varint
    q = p
    cnt = 0
    shift = 0
    ok = False
    while q < end and shift < 32:
        b = raw[q]
        cnt |= (b & 0x7F) << shift
        q += 1
        if not b & 0x80:
            ok = True
            break
        shift += 7
    if ok and q + cnt * 4 == end and cnt < 1000:
        return list(struct.unpack_from(f"<{cnt}I", raw, q)) if cnt else []
    # u32 count
    if p + 4 <= end:
        cnt = struct.unpack_from("<I", raw, p)[0]
        if p + 4 + cnt * 4 == end and cnt < 1000:
            return list(struct.unpack_from(f"<{cnt}I", raw, p + 4))
    return []


# scope(1) type(1) target(4).
def parse_action(raw, p, end):
    if p + 6 > end:
        return None
    atype = raw[p + 1]
    target = struct.unpack_from("<I", raw, p + 2)[0]
    return atype, target


# After the id: scope(1) type(1) target(4) isBus(1) props modifiers params.
# SetState/SetSwitch end with two u32 (group, value).
def parse_action_syncs(raw, p, end, atype, target):
    if atype == ACTION_TRIGGER:
        return [(target, "Trigger")] if target else []
    if atype in (ACTION_SET_GAME_PARAM, ACTION_RESET_GAME_PARAM):
        return [(target, "Game parameter")] if target else []
    if atype not in (ACTION_SET_STATE, ACTION_SET_SWITCH):
        return []
    q = p + 7
    if q >= end:
        return []
    num_props = raw[q]
    # id(1) + value(4) per property
    q += 1 + num_props * 5
    if q >= end:
        return []
    num_mods = raw[q]
    # id(1) + min/max(8) per modifier
    q += 1 + num_mods * 9
    if q + 8 > end:
        return []
    group, value = struct.unpack_from("<II", raw, q)
    if atype == ACTION_SET_STATE:
        return [(group, "State group"), (value, "State")]
    return [(group, "Switch group"), (value, "Switch")]


# BankSourceData at the start of the payload: returns (src_id, offset_base_params).
def parse_sound(raw, p, end):
    if p + SOURCE_DATA_SIZE > end:
        return None
    plugin = struct.unpack_from("<I", raw, p)[0]
    src_id = struct.unpack_from("<I", raw, p + 5)[0]
    q = p + SOURCE_DATA_SIZE
    # source plugin: uSize + params
    if plugin & 0xF == 2:
        if q + 4 > end:
            return src_id, None
        psize = struct.unpack_from("<I", raw, q)[0]
        q += 4
        if psize < 0x10000:
            q += psize
    return src_id, q


# DirectParentID offset from the start of NodeBaseParams with numFx==0: bus7 +7 (Eleiyas), bus6 +6, flat2 +2 (XXAR music).
# Each variant is (offset, fx entry size); with numFx>0 add 1 + numFx*fx_size.
PARENT_VARIANTS = {"bus7": (7, 7), "bus6": (6, 7), "flat2": (2, 6)}


def parse_parent(raw, p, end, variant):
    base_off, fx_size = PARENT_VARIANTS[variant]
    if p + 2 > end:
        return None
    num_fx = raw[p + 1]
    off = p + base_off
    if num_fx:
        if num_fx > 32:
            return None
        off += 1 + num_fx * fx_size
    if off + 4 > end:
        return None
    return struct.unpack_from("<I", raw, off)[0]


# Returns (set(src_ids), parent|None); layout proven on ZZZ.
def parse_music_track(raw, p, end, variant):
    if p + 5 > end:
        return None
    num_src = struct.unpack_from("<I", raw, p + 1)[0]
    if num_src > 100:
        return None
    q = p + 5
    if q + num_src * SOURCE_DATA_SIZE > end:
        return None
    srcs = set()
    for _ in range(num_src):
        srcs.add(struct.unpack_from("<I", raw, q + 5)[0])
        q += SOURCE_DATA_SIZE
    if q + 4 > end:
        return srcs, None
    num_pl = struct.unpack_from("<I", raw, q)[0]
    q += 4
    if num_pl > 500 or q + num_pl * TRACK_SRC_INFO_SIZE > end:
        return srcs, None
    for _ in range(num_pl):
        srcs.add(struct.unpack_from("<I", raw, q + 4)[0])
        q += TRACK_SRC_INFO_SIZE
    srcs.discard(0)
    parent = None
    try:
        # numSubTrack
        q += 4
        num_clip = struct.unpack_from("<I", raw, q)[0]
        q += 4
        if num_clip > 200:
            raise ValueError
        for _ in range(num_clip):
            q += 8
            npt = struct.unpack_from("<I", raw, q)[0]
            q += 4
            if npt > 10000:
                raise ValueError
            q += 12 * npt
        # eTrackType(4) + bIsTransitionEnabled(1)
        q += 5
        parent = parse_parent(raw, q, end, variant)
    except Exception:
        parent = None
    return srcs, parent


# probability(1) treeDepth(4) args(5*depth) treeSize(4) mode(1) nodes(12B: key, id/children, weight, prob).
def parse_dialogue_children(raw, p, end):
    if p + 10 > end:
        return []
    depth = struct.unpack_from("<I", raw, p + 1)[0]
    if depth > 16:
        return []
    q = p + 5 + depth * 5
    if q + 5 > end:
        return []
    tree_size = struct.unpack_from("<I", raw, q)[0]
    q += 5
    if tree_size > end - q + 4 or tree_size % 12:
        return []
    out = []
    for i in range(tree_size // 12):
        out.append(struct.unpack_from("<I", raw, q + i * 12 + 4)[0])
    return out


# ---------------------------------------------------------------- scanner

class ScanIndex:
    def __init__(self):
        self.wem_locations = defaultdict(list)
        self.external_locations = defaultdict(list)
        self.bank_ids = set()
        self.event_actions = {}
        self.event_banks = defaultdict(set)
        self.actions = {}
        self.dialogue_children = defaultdict(set)
        self.node_srcs = defaultdict(set)
        self.parents = defaultdict(set)
        self.object_ids = set()
        self.sync_ids = {}
        self.named_objects = {}
        self.stats = defaultdict(int)
        self.inv = None

    def build_inverted(self):
        inv = defaultdict(set)
        for node, srcs in self.node_srcs.items():
            chain = {node}
            cur = node
            for _ in range(64):
                ps = self.parents.get(cur)
                if not ps:
                    break
                cur = next(iter(ps))
                if cur in chain or cur == 0:
                    break
                chain.add(cur)
            extra = set()
            for c in list(chain):
                for pp in self.parents.get(c, ()):
                    if pp and pp not in chain:
                        extra.add(pp)
            chain |= extra
            for c in chain:
                inv[c] |= srcs
        self.inv = inv

    def wems_for_event(self, event_id):
        out = set()
        targets = set()
        for aid in self.event_actions.get(event_id, ()):
            act = self.actions.get(aid)
            if act and act[1]:
                targets.add(act[1])
        targets |= self.dialogue_children.get(event_id, set())
        for t in targets:
            out |= self.inv.get(t, set())
            out |= self.node_srcs.get(t, set())
        return out


# samples: [(raw, p, end)]; the variant with the best parse rate wins.
def calibrate_variant(samples, object_ids, kind):
    best, best_score = "bus7", -1.0
    for variant in PARENT_VARIANTS:
        good = total = 0
        for raw, p, end in samples:
            if kind == "music":
                # MusicParameter flags byte
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


def find_persistent_roots(root):
    root = Path(root)
    parts = list(root.parts)
    lowered = [p.lower() for p in parts]
    if "streamingassets" in lowered:
        i = lowered.index("streamingassets")
        candidate = Path(*parts[:i], "Persistent", *parts[i + 1:])
        if candidate.is_dir():
            return [candidate]
    return []


PERSISTENT_PRIORITY_PCKS = {"patch.pck", "hotfix.pck"}


# Same sub-path in both roots: Patch/Hotfix.pck win in Persistent.
# Everything else wins in StreamingAssets, the other copy is dropped.
def _dedupe_pck_files(paths):
    groups = {}
    for p in paths:
        parts = [x.lower() for x in p.parts]
        marker, key = None, str(p).lower()
        for m in ("streamingassets", "persistent"):
            if m in parts:
                marker, key = m, "/".join(parts[parts.index(m) + 1:])
                break
        groups.setdefault(key, []).append((marker, p))
    kept, shadowed = [], 0
    for key, candidates in groups.items():
        if len(candidates) > 1:
            name = candidates[0][1].name.lower()
            preferred = "persistent" if name in PERSISTENT_PRIORITY_PCKS else "streamingassets"
            chosen = [p for m, p in candidates if m == preferred] or [p for _, p in candidates]
            kept.append(chosen[0])
            shadowed += len(candidates) - 1
        else:
            kept.append(candidates[0][1])
    return sorted(kept), shadowed


def scan_folder(root, progress=None, cancel=None):
    root = Path(root)
    index = ScanIndex()
    roots = [root] + find_persistent_roots(root)
    if len(roots) > 1:
        index.stats["persistent_root"] = str(roots[1])
    pck_files, shadowed = _dedupe_pck_files({p for r in roots for p in r.rglob("*.pck")})
    if shadowed:
        index.stats["shadowed_pck"] = shadowed
    bnk_files = sorted({p for r in roots for p in r.rglob("*.bnk")})
    total_files = len(pck_files) + len(bnk_files)
    if progress:
        progress(0, total_files, "Indexing files...")

    bnk_metas = []
    done = 0

    for pck_path in pck_files:
        if cancel and cancel():
            return index
        try:
            lang_map, banks, sounds, externals = parse_pck(pck_path)
        except Exception:
            done += 1
            continue
        spath = str(pck_path)
        for s in sounds:
            index.wem_locations[s["id"]].append(WemLocation(
                spath, 0, lang_map.get(s["lang_id"], str(s["lang_id"])),
                s["offset"], s["size"], "pck"))
        for e in externals:
            index.external_locations[e["id"]].append(WemLocation(
                spath, 0, lang_map.get(e["lang_id"], str(e["lang_id"])),
                e["offset"], e["size"], "pck"))
        with open(pck_path, "rb") as f:
            for b in banks:
                index.bank_ids.add(b["id"])
                meta = BnkMeta()
                meta.pck_path = spath
                meta.bank_id = b["id"]
                meta.lang = lang_map.get(b["lang_id"], str(b["lang_id"]))
                try:
                    scan_bnk_chunks(f, b["offset"], b["size"], meta)
                except Exception:
                    continue
                bnk_metas.append(meta)
                for wid, off, wsize in meta.didx_wems:
                    index.wem_locations[wid].append(WemLocation(
                        spath, b["id"], meta.lang, off, wsize, "bnk"))
        done += 1
        if progress:
            progress(done, total_files, pck_path.name)

    for bnk_path in bnk_files:
        if cancel and cancel():
            return index
        try:
            size = bnk_path.stat().st_size
            meta = BnkMeta()
            meta.pck_path = str(bnk_path)
            meta.lang = "sfx"
            with open(bnk_path, "rb") as f:
                scan_bnk_chunks(f, 0, size, meta)
            meta.bank_id = meta.bkhd_id
            index.bank_ids.add(meta.bkhd_id)
            bnk_metas.append(meta)
            for wid, off, wsize in meta.didx_wems:
                index.wem_locations[wid].append(WemLocation(
                    meta.pck_path, meta.bank_id, meta.lang, off, wsize, "bnk"))
        except Exception:
            pass
        done += 1
        if progress:
            progress(done, total_files, bnk_path.name)

    # Pass 1: universe of object ids, for calibration.
    hirc_metas = [m for m in bnk_metas if m.hirc_size]
    if progress:
        progress(0, len(hirc_metas) or 1, "Parsing HIRC (1/2)...")
    container_samples, music_samples = [], []
    raw_cache = {}
    for i, meta in enumerate(hirc_metas):
        if cancel and cancel():
            return index
        try:
            with open(meta.pck_path, "rb") as f:
                f.seek(meta.hirc_offset)
                raw = f.read(meta.hirc_size)
        except Exception:
            continue
        raw_cache[id(meta)] = raw
        for otype, oid, p, end in iter_hirc_objects(raw):
            index.object_ids.add(oid)
            index.bank_ids.add(meta.bkhd_id)
            if otype in CONTAINER_TYPES and len(container_samples) < 4000:
                container_samples.append((raw, p, end))
            elif otype in MUSIC_NODE_TYPES and len(music_samples) < 4000:
                music_samples.append((raw, p, end))
        if progress and i % 20 == 0:
            progress(i + 1, len(hirc_metas), "Parsing HIRC (1/2)...")

    cont_variant, cont_score = calibrate_variant(container_samples, index.object_ids, "container")
    music_variant, music_score = calibrate_variant(music_samples, index.object_ids, "music")
    index.stats["cont_variant"] = f"{cont_variant} ({cont_score:.0%})"
    index.stats["music_variant"] = f"{music_variant} ({music_score:.0%})"

    # Pass 2: extraction.
    if progress:
        progress(0, len(hirc_metas) or 1, "Parsing HIRC (2/2)...")
    for i, meta in enumerate(hirc_metas):
        if cancel and cancel():
            return index
        raw = raw_cache.get(id(meta))
        if raw is None:
            continue
        for otype, oid, p, end in iter_hirc_objects(raw):
            named = NAMED_OBJECT_TYPES.get(otype)
            if named:
                index.named_objects.setdefault(oid, named)
            try:
                if otype == HIRC_EVENT:
                    acts = parse_event_actions(raw, p, end)
                    if acts:
                        index.event_actions.setdefault(oid, acts)
                    if meta.bkhd_id:
                        index.event_banks[oid].add(meta.bkhd_id)
                    index.stats["events"] += 1
                elif otype == HIRC_ACTION:
                    act = parse_action(raw, p, end)
                    if act:
                        index.actions.setdefault(oid, act)
                        for sync_id, kind in parse_action_syncs(raw, p, end, act[0], act[1]):
                            if sync_id:
                                index.sync_ids.setdefault(sync_id, kind)
                elif otype == HIRC_SOUND:
                    r = parse_sound(raw, p, end)
                    if r:
                        src_id, bp = r
                        if src_id:
                            index.node_srcs[oid].add(src_id)
                        if bp is not None:
                            parent = parse_parent(raw, bp, end, cont_variant)
                            if parent and parent in index.object_ids:
                                index.parents[oid].add(parent)
                elif otype == HIRC_MUSIC_TRACK:
                    r = parse_music_track(raw, p, end, "flat2")
                    if r and not (r[1] and r[1] in index.object_ids):
                        for variant in ("bus7", "bus6"):
                            r2 = parse_music_track(raw, p, end, variant)
                            if r2 and r2[1] and r2[1] in index.object_ids:
                                r = r2
                                break
                    if r:
                        srcs, parent = r
                        if srcs:
                            index.node_srcs[oid] |= srcs
                        if parent and parent in index.object_ids:
                            index.parents[oid].add(parent)
                elif otype in CONTAINER_TYPES:
                    parent = parse_parent(raw, p, end, cont_variant)
                    if parent and parent in index.object_ids:
                        index.parents[oid].add(parent)
                elif otype in MUSIC_NODE_TYPES:
                    parent = parse_parent(raw, p + 1, end, music_variant)
                    if parent and parent in index.object_ids:
                        index.parents[oid].add(parent)
                elif otype == HIRC_DIALOGUE_EVENT:
                    kids = [k for k in parse_dialogue_children(raw, p, end)
                            if k and k in index.object_ids]
                    if kids:
                        index.dialogue_children[oid] |= set(kids)
                    if meta.bkhd_id:
                        index.event_banks[oid].add(meta.bkhd_id)
                    index.stats["dialogue_events"] += 1
            except Exception:
                index.stats["parse_errors"] += 1
        if progress and i % 20 == 0:
            progress(i + 1, len(hirc_metas), "Parsing HIRC (2/2)...")

    index.stats["pck"] = len(pck_files)
    index.stats["bnk_inline"] = len(bnk_metas)
    index.stats["wem_ids"] = len(index.wem_locations)
    index.stats["externals"] = len(index.external_locations)
    index.stats["objects"] = len(index.object_ids)
    index.stats["game_syncs"] = len(index.sync_ids)
    index.stats["named_objects"] = len(index.named_objects)
    if progress:
        progress(1, 1, "Building reverse index...")
    index.build_inverted()
    return index


# ---------------------------------------------------------------- matching

class NameMatch:
    __slots__ = ("name", "kind", "wem_ids", "hash_id")

    def __init__(self, name, kind, wem_ids, hash_id=0):
        self.name = name
        self.kind = kind
        self.wem_ids = wem_ids
        self.hash_id = hash_id


def match_names(names, index, progress=None):
    matches = []
    unmatched = []
    event_ids = set(index.event_actions) | set(index.dialogue_children)
    total = len(names)
    for i, name in enumerate(names):
        h32 = fnv1_32(name)
        h64 = fnv1_64(name)
        found = False
        if h32 in event_ids:
            wems = index.wems_for_event(h32)
            kind = "Event" if h32 in index.event_actions else "DialogueEvent"
            matches.append(NameMatch(name, kind, sorted(wems), h32))
            found = True
        if h32 in index.bank_ids:
            matches.append(NameMatch(name, "Bank", [], h32))
            found = True
        if h32 in index.wem_locations:
            matches.append(NameMatch(name, "Direct WEM", [h32], h32))
            found = True
        if h64 in index.external_locations:
            matches.append(NameMatch(name, "External", [h64], h64))
            found = True
        else:
            # Exported voice names are full paths whose id is the hash of path + extension.
            # That way the exported json resolves again on reload.
            as_path = name if name.lower().endswith(".wem") else name + ".wem"
            h64_path = fnv1_64(as_path)
            if h64_path in index.external_locations:
                matches.append(NameMatch(name, "External", [h64_path], h64_path))
                found = True
        if not found:
            kind = index.sync_ids.get(h32) or index.named_objects.get(h32)
            if kind:
                matches.append(NameMatch(name, kind, [], h32))
                found = True
        if not found:
            unmatched.append(name)
        if progress and i % 2000 == 0:
            progress(i, total, "Matching names...")
    return matches, unmatched


# One match per (kind, id): the names file and the online sources overlap.
# The first one wins, so a name loaded from file beats the one rebuilt online.
def dedupe_matches(matches):
    seen = set()
    unique = []
    for m in matches:
        key = (m.kind, m.hash_id)
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


# Accepts txt (one name per line), a json list of strings, or the tool's own export.
def load_names(path):
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() == ".json":
        try:
            doc = json.loads(text)
        except Exception:
            doc = None
        if isinstance(doc, dict):
            sections = ("events", "banks", "direct_wems", "externals", "game_syncs")
            if any(isinstance(doc.get(s), list) for s in sections):
                # Voices in an export are rebuilt by restore_exported_matches from the saved id.
                # Re-hashing 326k paths at 32 bit here yields ~26 false collisions.
                return [x for x in doc.get("unmatched_names", []) if isinstance(x, str)]
        if isinstance(doc, list):
            return [x for x in doc if isinstance(x, str) and x]
    names = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def _wems_for_object(index, oid):
    if oid in index.event_actions:
        return sorted(index.wems_for_event(oid))
    if oid in index.object_ids:
        inv = index.inv or {}
        return sorted(set(inv.get(oid, set())) | set(index.node_srcs.get(oid, set())))
    return []


# Rebuilds the matches of an export from the saved id, not from the name hash.
# Labels whose id is not a hash (MusicSegment and friends) would not come back otherwise.
def restore_exported_matches(path, index):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    restored = []
    for section, fixed_kind in (("events", None), ("banks", "Bank"),
                                ("direct_wems", "Direct WEM"), ("externals", "External"),
                                ("game_syncs", None)):
        for entry in doc.get(section, []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            oid = entry.get("id")
            kind = fixed_kind or entry.get("type") or entry.get("kind")
            if not name or not kind or not isinstance(oid, int):
                continue
            if kind == "Bank":
                if oid not in index.bank_ids:
                    continue
                wems = []
            elif kind == "Direct WEM":
                if oid not in index.wem_locations:
                    continue
                wems = [oid]
            elif kind == "External":
                if oid not in index.external_locations:
                    continue
                wems = [oid]
            else:
                wems = _wems_for_object(index, oid)
                if (oid not in index.object_ids and oid not in index.event_actions
                        and oid not in index.sync_ids and oid not in index.named_objects):
                    continue
            restored.append(NameMatch(name, kind, wems, oid))
    return restored


def apply_exported_matches(names_path, index, matches):
    restored = restore_exported_matches(names_path, index) if names_path else []
    return matches + restored if restored else matches


def prune_unmatched(matches, unmatched):
    resolved_names = {m.name for m in matches}
    return [n for n in unmatched if n not in resolved_names]


# ---------------------------------------------------------------- export

def _rel_path(path, root):
    try:
        return str(Path(path).relative_to(root))
    except Exception:
        return str(path)


def _wem_entry(wid, index, root):
    locs = index.wem_locations.get(wid, []) + index.external_locations.get(wid, [])
    return {
        "languages": sorted({l.lang for l in locs}),
        "sources": [{
            "file": _rel_path(l.pck_path, root),
            "kind": l.kind,
            "bnk": l.bnk_id or None,
            "offset": l.offset,
            "size": l.size,
        } for l in locs],
    }


def export_txt(matches, out_path):
    seen = set()
    lines = []
    for m in matches:
        if m.kind in ("Event", "DialogueEvent") and m.name not in seen:
            seen.add(m.name)
            lines.append(m.name)
    Path(out_path).write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    return len(lines)


def export_json(matches, unmatched, index, scan_root, names_file, out_path):
    events, banks, direct_wems, externals, game_syncs = [], [], [], [], []
    used_wems = set()
    for m in sorted(matches, key=lambda x: x.name.lower()):
        if m.kind in ("Event", "DialogueEvent"):
            action_types = sorted({
                ACTION_TYPE_NAMES.get(a[0], f"type_{a[0]}")
                for aid in index.event_actions.get(m.hash_id, ())
                if (a := index.actions.get(aid))})
            events.append({
                "name": m.name,
                "id": m.hash_id,
                "type": m.kind,
                "banks": sorted(index.event_banks.get(m.hash_id, ())),
                "action_types": action_types,
                "wems": m.wem_ids,
            })
            used_wems.update(m.wem_ids)
        elif m.kind == "Bank":
            banks.append({"name": m.name, "id": m.hash_id})
        elif m.kind == "Direct WEM":
            direct_wems.append({"name": m.name, "id": m.hash_id})
            used_wems.add(m.hash_id)
        elif m.kind == "External":
            externals.append({"name": m.name, "id": m.hash_id})
            used_wems.add(m.hash_id)
        else:
            game_syncs.append({"name": m.name, "id": m.hash_id, "kind": m.kind})
    doc = {
        "tool": APP_NAME,
        "scan_folder": str(scan_root),
        "names_file": str(names_file),
        "stats": {k: v for k, v in sorted(index.stats.items())},
        "counts": {
            "events": len(events), "banks": len(banks),
            "direct_wems": len(direct_wems), "externals": len(externals),
            "game_syncs": len(game_syncs),
            "wems": len(used_wems), "unmatched_names": len(unmatched),
        },
        "events": events,
        "banks": banks,
        "direct_wems": direct_wems,
        "externals": externals,
        "game_syncs": game_syncs,
        "wems": {str(w): _wem_entry(w, index, scan_root) for w in sorted(used_wems)},
        "unmatched_names": unmatched,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    return len(events)


# ---------------------------------------------------------------- name harvesting

DEFAULT_HARVEST_PREFIXES = "play,vo,stop,state,sfx,pause,resume,mute,trigger"
# HSR names events Ev_... instead of Play_...: without 'ev' the harvest misses them.
HARVEST_PREFIXES_BY_GAME = {"SR": "ev,play,vo,stop,state,sfx,music,amb,mute,trigger"}


def default_harvest_prefixes(game):
    return HARVEST_PREFIXES_BY_GAME.get(game, DEFAULT_HARVEST_PREFIXES)


# 16 MB per read: constant RAM per worker
_HARVEST_CHUNK = 1 << 24
# covers names straddling two chunks
_HARVEST_OVERLAP = 256


HARVEST_GAMES = ("(raw / auto)", "ZZZ", "GI", "SR")
_GAME_KEY = {"ZZZ": "ZZZ", "GI": "GI", "SR": "SR"}
# UTF-16-ascii collapse: drop isolated null bytes so "P\0l\0a\0y" reads as "Play".
_ISO_NULL = re.compile(rb"(?<=[\x01-\xff])\x00(?=[\x01-\xff])")


def _harvest_pattern(prefixes):
    alt = b"|".join(re.escape(p.strip().encode()) for p in prefixes if p.strip())
    return re.compile(rb"(?<![0-9A-Za-z_])(?:" + alt + rb")_[0-9A-Za-z_]{2,120}",
                      re.IGNORECASE)


def blk_decode_available():
    return oodle_available()


# Every string is preceded by an int32 length and the regex must use it.
# Otherwise it swallows the next string's length byte and produces names like _Wrong3.
def _names_from_data(data, pat):
    found = set()
    for m in pat.finditer(data):
        start, stop = m.start(), m.end()
        if start >= 4:
            declared = int.from_bytes(data[start - 4:start], "little")
            if 4 <= declared < stop - start:
                stop = start + declared
        found.add(data[start:stop].decode("ascii", "ignore"))
    return found


def _names_from_payload(payload, pat):
    return _names_from_data(_ISO_NULL.sub(b"", payload), pat)


# Regexes run on the lowercased text but the result is sliced from the original via match offsets.
# That keeps the game's original casing in the names.
def _vo_from_data(data):
    low = data.lower()
    prefixes = {data[m.start():m.end()].decode("ascii", "ignore") for m in _VO_PREFIX_RE.finditer(low)}
    sources = {data[m.start():m.end()].decode("ascii", "ignore") for m in _VO_PATH_RE.finditer(low)}
    sources |= {data[m.start():m.end()].decode("ascii", "ignore") for m in _VO_LEAF_RE.finditer(low)}
    return prefixes, sources


# Block by block, without concatenating the payload.
# On Genshin a single file exceeds 160 MB and parallel workers would blow up the RAM.
def _harvest_blk(path, pat, game):
    names, prefixes, sources = set(), set(), set()
    seen_any = False
    for block in iter_blk_blocks(path, game):
        if not block:
            continue
        seen_any = True
        data = _ISO_NULL.sub(b"", block)
        names |= _names_from_data(data, pat)
        pre, src = _vo_from_data(data)
        prefixes |= pre
        sources |= src
    if not seen_any:
        # unknown container: the caller falls back to the raw scan
        return None
    return names, prefixes, sources


def _harvest_file(path, prefixes, game=None):
    pat = _harvest_pattern(prefixes)
    if game and (path.lower().endswith(".blk") or path.lower().endswith(".block")):
        try:
            res = _harvest_blk(path, pat, game)
            if res is not None:
                return res
        except Exception:
            # fall back to raw scan below
            pass
    found = set()
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(_HARVEST_CHUNK)
                if not chunk:
                    break
                data = tail + chunk
                for m in pat.finditer(data):
                    found.add(m.group().decode("ascii", "ignore"))
                tail = data[-_HARVEST_OVERLAP:]
    except Exception:
        pass
    return found, set(), set()


def harvest_names(folder, prefixes, progress=None, cancel=None, game=None):
    return harvest_all(folder, prefixes, progress, cancel, game)["names"]


# With game set, .blk files go through blkdec, the rest through the raw scan.
def harvest_all(folder, prefixes, progress=None, cancel=None, game=None):
    game = _GAME_KEY.get(game)
    # Downloaded/hotfix content lives in the Persistent twin of the folder (e.g.
    # Persistent\Blocks): without it a chunk of the newest event names is missed.
    roots = [Path(folder)] + find_persistent_roots(folder)
    files = sorted({p for r in roots for p in r.rglob("*") if p.is_file()})
    found, vo_prefixes, vo_sources = set(), set(), set()
    workers = max(2, min(8, (os.cpu_count() or 4) - 1))
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_harvest_file, str(p), prefixes, game): p for p in files}
        for fut in as_completed(futures):
            if cancel and cancel():
                pool.shutdown(wait=False, cancel_futures=True)
                break
            names, prefs, srcs = fut.result()
            found |= names
            vo_prefixes |= prefs
            vo_sources |= srcs
            done += 1
            if progress and (done % 5 == 0 or done == len(files)):
                progress(done, len(files), f"Harvesting ({len(found)} names, {len(vo_sources)} voice)")
    if progress:
        progress(len(files), len(files), "Harvest done")
    return {"names": sorted(found), "vo_prefixes": sorted(vo_prefixes), "vo_sources": sorted(vo_sources)}


# ---------------------------------------------------------------- voice (external) names

# Known voice-file categories: first token of the name, or first folder of the path.
VO_CATEGORIES = (
    "Accompany", "Activity", "Breath", "Bubble", "Chat", "ChatPlus", "Chessboard", "Cinema",
    "Clue", "Comic", "Gal", "Galgame", "Level", "Maincity", "Maincity_NPC", "Maincity_ShopNpc",
    "Message", "Ongoing", "OngoingAntique", "OngoingCinema", "OngoingLevel", "OngoingMainCity",
    "Tips", "TL", "VoiceOnly", "NPC", "MainCity", "Timeline", "GalGame",
)

# Language names and path shapes vary per game: never fixed, always calibrated on the data.
# ZZZ uses '<lang>/Ex/', Genshin '<lang>' + backslash, Star Rail '<lang>/'.
VO_LANGUAGE_CANDIDATES = (
    "English(EN)", "English(US)", "English", "En",
    "Japanese(JP)", "Japanese", "Jp",
    "Chinese(PRC)", "Chinese", "Cn",
    "Korean(KR)", "Korean", "Kr",
)


# Star Rail inserts 'voice/' between language and path.
# Protagonist lines carry _m/_f suffixes: both seen in the reference repos.
VO_SUFFIXES = ("", "_m", "_f")


def vo_head_candidates(languages=VO_LANGUAGE_CANDIDATES):
    # some games omit the language
    heads = [""]
    for lang in languages:
        heads.extend([f"{lang}/Ex/", f"{lang}\\", f"{lang}/", f"{lang}/voice/"])
    return heads


def vo_language_of_head(head):
    return head.rstrip("/\\").replace("/Ex", "") or "(no language)"


# Regexes run on already-lowercased text: without IGNORECASE they are 3x faster.
# Voice paths are compared lowercased in the cracking anyway.
_VO_PREFIX_RE = re.compile((r"(?<![0-9a-z_/\\])vo_[0-9a-z_]+(?:[/\\][0-9a-z_]+)*[/\\]").encode())
_VO_PATH_RE = re.compile((r"(?<![0-9a-z_/\\])vo_[0-9a-z_]+(?:[/\\][0-9a-z_]+)+").encode())
_VO_LEAF_RE = re.compile(
    (r"(?<![0-9a-z_/])(?:%s)_[0-9a-z_]{2,150}"
     % "|".join(sorted({c.lower() for c in VO_CATEGORIES}, key=len, reverse=True))).encode())

_FNV64_PRIME = 0x100000001B3
_FNV64_INIT = 0xCBF29CE484222325
_MASK64 = 0xFFFFFFFFFFFFFFFF


# Incremental FNV-1 64: the prefix state gets reused.
def _fnv64_feed(data, state=_FNV64_INIT):
    for b in data:
        state = (state * _FNV64_PRIME) & _MASK64
        state ^= b
    return state


def _vo_category_of(leaf):
    low = leaf.rsplit("/", 1)[-1].lower()
    for start, cat in (("galgame_", "vo_galgame"), ("comic_", "vo_comic"), ("chatplus_", "vo_chatplus"),
                       ("timeline_", "vo_tl"), ("vo_cs", "vo_tl"), ("bubble", "vo_bubble"),
                       ("vo_npc_", "vo_bubble"), ("cinema", "vo_cinema"), ("ongoingcinema_", "vo_cinema"),
                       ("ongoinglevel_", "vo_level"), ("level_", "vo_level"), ("chessboard_", "vo_chessboard"),
                       ("accompany_", "vo_accompany"), ("tips_", "vo_tips"), ("ongoing", "vo_ongoing")):
        if low.startswith(start):
            return cat
    return None


# 'VO_Galgame/Ver2_2/Vo_Belle/' -> (category, speaker) lowercased.
def parse_vo_prefix(prefix):
    parts = [x for x in prefix.strip("/").split("/") if x]
    if not parts:
        return None
    category = parts[0].lower()
    speaker = ""
    for part in reversed(parts[1:]):
        if part.lower().startswith("vo_"):
            speaker = part[3:].lower()
            break
    return category, speaker


def _vo_candidate_paths(source, by_category, by_speaker, max_candidates):
    leaf_low = source.lower()
    # already a path
    if "/" in leaf_low or "\\" in leaf_low:
        return [leaf_low]
    paths = []
    category = _vo_category_of(leaf_low)
    pool = by_category.get(category) if category else None
    if pool:
        named = [pre for pre, spk in pool if spk and spk in leaf_low]
        paths = named or ([pre for pre, _ in pool] if len(pool) <= max_candidates else [])
    if not paths:
        for speaker, entries in by_speaker.items():
            if speaker and speaker in leaf_low:
                paths.extend(pre for pre, _ in entries)
                if len(paths) >= max_candidates:
                    break
    out = []
    for pre in paths[:max_candidates]:
        sep = "\\" if "\\" in pre else "/"
        out.append(pre.rstrip("/\\") + sep + leaf_low)
    return out


def _vo_indexes(prefixes):
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


def _vo_tail(path, suffix):
    if path.lower().endswith(".wem"):
        base = path[:-4]
    else:
        base = path
    return (base + suffix + ".wem").encode()


# Discovers which (language, path shape) pairs are in use by trying them on real data.
# No fixed per-game rules: only prefixes that hit real ids survive.
def calibrate_vo_heads(prefixes, sources, external_ids, sample=4000, max_candidates=16):
    by_category, by_speaker = _vo_indexes(prefixes)
    heads = vo_head_candidates()
    head_states = {h: _fnv64_feed(h.lower().encode()) for h in heads}
    hits = Counter()
    for source in sources[:sample]:
        for path in _vo_candidate_paths(source, by_category, by_speaker, max_candidates):
            for suffix in VO_SUFFIXES:
                tail = _vo_tail(path, suffix)
                for head, state in head_states.items():
                    if _fnv64_feed(tail, state) in external_ids:
                        hits[(head, suffix)] += 1
    return [combo for combo, _ in hits.most_common()], hits


def _walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_strings(value)


# For games with no plaintext paths in the client: every .wem string from any field, obfuscated or not.
# The cracking then verifies by hash, as always.
def import_vo_sources(path, progress=None):
    root = Path(path)
    files = sorted(root.rglob("*.json")) if root.is_dir() else [root]
    found = set()
    for i, f in enumerate(files):
        try:
            doc = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for text in _walk_strings(doc):
            if text.lower().endswith(".wem") and 4 < len(text) < 300:
                found.add(text)
        if progress and i % 200 == 0:
            progress(i, len(files), f"Importing voice paths ({len(found)} found)")
    if progress:
        progress(len(files), len(files), f"Imported {len(found)} voice paths")
    return sorted(found)


# The id is FNV-1 64 of '<head><path>.wem' lowercased; the head is game-specific and calibrated on the data.
# Only candidates that hit a present id are kept, so zero false positives.
def crack_vo_names(prefixes, sources, external_ids, heads=None, progress=None,
                   max_candidates=48):
    external_ids = set(external_ids)
    sources = list(sources)
    combos = heads
    if combos is None:
        if progress:
            progress(0, len(sources), "Calibrating voice path rule...")
        combos, _ = calibrate_vo_heads(prefixes, sources, external_ids)
        if progress:
            found = ", ".join(f"{vo_language_of_head(h)}{s or ''}" for h, s in combos[:4]) or "none"
            progress(0, len(sources), f"Voice rule: {found}")
    if not combos:
        return {}

    by_category, by_speaker = _vo_indexes(prefixes)
    head_states = {head: _fnv64_feed(head.lower().encode()) for head, _ in combos}
    resolved = {}
    total = len(sources)
    for i, source in enumerate(sources):
        for path in _vo_candidate_paths(source, by_category, by_speaker, max_candidates):
            for head, suffix in combos:
                state = head_states.get(head)
                if state is None:
                    continue
                h = _fnv64_feed(_vo_tail(path, suffix), state)
                if h in external_ids and h not in resolved:
                    resolved[h] = (path + suffix, vo_language_of_head(head))
        if progress and i % 2000 == 0:
            progress(i, total, f"Recovering voice names ({len(resolved)} found)")
    if progress:
        progress(total, total, f"Voice names: {len(resolved)} recovered")
    return resolved


# ---------------------------------------------------------------- playback helpers

# The tool ships without vgmstream, so it is fetched once into the config dir on first use.
# XXAR's copy and anything on PATH still win, so an existing install is never downloaded twice.
VGMSTREAM_RELEASE_API = "https://api.github.com/repos/vgmstream/vgmstream/releases/latest"
VGMSTREAM_ASSETS = {"win32": "vgmstream-win64.zip", "linux": "vgmstream-linux.zip", "darwin": "vgmstream-mac.zip"}


def vgmstream_exe_name():
    return "vgmstream-cli.exe" if sys.platform == "win32" else "vgmstream-cli"


def vgmstream_dir():
    return _config_dir() / "tools" / "vgmstream"


def find_vgmstream():
    exe = vgmstream_exe_name()
    env = os.environ.get("HSI_VGMSTREAM")
    if env and Path(env).is_file():
        return env
    candidates = [vgmstream_dir() / exe]
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).parent
        candidates += [here / exe, here / "vgmstream" / exe]
    if sys.platform == "win32":
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "XXAR" / "tools" / "audio" / "vgmstream" / exe)
    else:
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        candidates.append(data_home / "XXAR" / "tools" / "audio" / "vgmstream" / exe)
    for c in candidates:
        if c.is_file():
            return str(c)
    return which("vgmstream-cli")


# The zip has its own top folder, so the tree is flattened onto the first folder holding the exe.
def download_vgmstream(progress=None):
    exe = vgmstream_exe_name()
    asset_name = VGMSTREAM_ASSETS.get(sys.platform)
    if not asset_name:
        raise RuntimeError(f"no vgmstream build for {sys.platform}")
    if progress:
        progress(0, 1, "Looking up the latest vgmstream release...")
    release = json.loads(_http_get(VGMSTREAM_RELEASE_API).decode("utf-8", "ignore"))
    asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
    if not asset:
        raise RuntimeError(f"{asset_name} missing from vgmstream {release.get('tag_name', '?')}")
    if progress:
        progress(0, 1, f"Downloading vgmstream {release.get('tag_name', '')} ({asset['size'] // 1000000} MB)...")
    blob = _http_get(asset["browser_download_url"], timeout=180)

    target = vgmstream_dir()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        members = [m for m in zf.namelist() if not m.endswith("/")]
        root = next((m[:-len(exe)] for m in members if m.rsplit("/", 1)[-1] == exe), "")
        for member in members:
            if root and not member.startswith(root):
                continue
            name = member[len(root):]
            if "/" in name:
                continue
            (target / name).write_bytes(zf.read(member))
    result = target / exe
    if not result.is_file():
        raise RuntimeError(f"{exe} not found inside {asset_name}")
    if sys.platform != "win32":
        result.chmod(0o755)
    if progress:
        progress(1, 1, f"vgmstream {release.get('tag_name', '')} ready")
    return str(result)


def extract_wem_bytes(loc):
    with open(loc.pck_path, "rb") as f:
        f.seek(loc.offset)
        return f.read(loc.size)


def wem_to_wav(wem_bytes, vgmstream, out_dir, stem="wnf_current"):
    wem_path = Path(out_dir) / f"{stem}.wem"
    wav_path = Path(out_dir) / f"{stem}.wav"
    wem_path.write_bytes(wem_bytes)
    kwargs = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si
    r = subprocess.run([vgmstream, "-o", str(wav_path), str(wem_path)],
                       capture_output=True, timeout=60, **kwargs)
    if r.returncode != 0 or not wav_path.exists():
        raise RuntimeError(r.stderr.decode(errors="replace")[:300] or "vgmstream failed")
    return str(wav_path)


# ---------------------------------------------------------------- online name sources (Dimbreath)

# GI/HSR clients keep voice paths only as hashes: the names live in Dimbreath's repos.
# Those repos vanish (DMCA), so they can be overridden in config.json under voice_sources.
VOICE_SOURCES = {
    "GI": {
        "label": "Genshin - Dimbreath/animegamedata2",
        "project": "Dimbreath/animegamedata2",
        "ref": "main",
        "voice_subtree": "BinOutput/Voice",
        "audio_subtree": "BinOutput/Audio",
    },
    "ZZZ": {
        "label": "Zenless - dimbreath/ZenlessData (music titles)",
        "host": "https://git.mero.moe",
        "project": "dimbreath/ZenlessData",
        "ref": "master",
        "music_cfg": "FileCfg/MusicPlayerConfigTemplateTb.json",
        "textmap": "TextMap/TextMap_ENTemplateTb.json",
        "textmap_overwrite": "TextMap/TextMap_ENOverwriteTemplateTb.json",
        "event_cfg_files": ["FileCfg/AudioEventTemplateTb.json"],
    },
    "SR": {
        "label": "Star Rail - Dimbreath/turnbasedgamedata",
        "project": "Dimbreath/turnbasedgamedata",
        "ref": "main",
        "voice_files": ["ExcelOutput/VoiceConfig.json"],
        # Event names (Ev_...) are scattered across tables: only the small high-yield ones are taken.
        # The huge folders (Level, LevelOutput: over 1 GB) are left to the game.
        "event_files": ["ExcelOutput/VoiceAtlas.json", "ExcelOutput/ChimeraTalk.json",
                        "Config/AudioConfig.json"],
        "event_subtrees": ["Config/ConfigAnimEvents", "Config/ConfigFreeStyle", "Config/Props"],
        "event_prefixes": "Ev,Play,Stop,Set",
    },
}

# Language folder names for the hash rule, per game.
# Calibration keeps only those that actually hit, so extra values do no harm.
VO_ONLINE_LANGS = {
    "GI": ["English(US)", "Japanese", "Chinese", "Korean"],
    "SR": ["English", "Chinese", "Japanese", "Korean"],
    "ZZZ": ["English(EN)", "Japanese(JP)", "Chinese", "Korean"],
}


# Everything game-specific in one place.
# The UI has a single game selector and derives decryptor, blocks folder and online source from it.
GAME_UI = {
    "ZZZ": {"label": "Zenless", "decrypt": "mhy0/mhy1", "has_online": True,
            "blocks": ("ZenlessZoneZero_Data", "StreamingAssets", "Blocks"),
            "match": ("zenlesszonezero", "zenless", "zzz")},
    "GI": {"label": "Genshin", "decrypt": "Blb3", "has_online": True,
           "blocks": ("GenshinImpact_Data", "StreamingAssets", "AssetBundles", "blocks"),
           "match": ("genshin", "yuanshen")},
    "SR": {"label": "Star Rail", "decrypt": "mr0k", "has_online": True,
           "blocks": ("StarRail_Data", "StreamingAssets", "Asb", "Windows"),
           "match": ("starrail", "star rail", "hkrpg", "honkai")},
}
GAME_ORDER = ("ZZZ", "GI", "SR")
DEFAULT_UI_GAME = "ZZZ"

# Result buckets for the side filter: the order is the one shown in the rail.
TYPE_BUCKETS = (("all", "All"), ("voice", "Voice"), ("event", "Event"),
                ("music", "Music"), ("bank", "Bank"), ("sync", "State / other"))


def bucket_of_kind(kind):
    if kind == "External":
        return "voice"
    if kind in ("Event", "DialogueEvent"):
        return "event"
    if kind in ("MusicSegment", "Music"):
        return "music"
    if kind == "Bank":
        return "bank"
    return "sync"


def online_source_short(game):
    project = (VOICE_SOURCES.get(game) or {}).get("project", "")
    return project.split("/")[-1] if project else "—"


def detect_game_from_path(text):
    low = (text or "").lower()
    for game in GAME_ORDER:
        if any(k in low for k in GAME_UI[game]["match"]):
            return game
    return None


def _voice_cache_file(game):
    return _config_dir() / "cache" / f"voice_{game}.json"


def _subproc_kwargs():
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": si}


def _http_get(url, timeout=300):
    contexts = []
    try:
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:
        pass
    try:
        contexts.append(ssl.create_default_context())
    except Exception:
        pass
    last = None
    for ctx in contexts:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HoyoSoundIndexer"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except Exception as e:
            last = e
    try:
        out = subprocess.run(["curl", "-sfL", url], capture_output=True,
                             timeout=timeout, **_subproc_kwargs())
        if out.returncode == 0 and out.stdout:
            return out.stdout
        last = RuntimeError(out.stderr.decode("utf-8", "replace")[:200] or "curl failed")
    except Exception as e:
        last = e
    raise RuntimeError(f"download failed: {url}: {last}")


# Archive of just the subtree: no need to download the whole repo.
def _gitlab_archive(source, subtree):
    project = source["project"].replace("/", "%2F")
    return _http_get(f"https://gitlab.com/api/v4/projects/{project}/repository/archive.tar.gz"
                     f"?path={subtree}&sha={source['ref']}")


def _gitlab_raw(source, relative_path):
    project = source["project"]
    return _http_get(f"https://gitlab.com/{project}/-/raw/{source['ref']}/{relative_path}")


def _extract_wem_from_targz(blob, progress=None):
    found = set()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        members = [m for m in tf.getmembers() if m.isfile() and m.name.endswith(".json")]
        for i, m in enumerate(members):
            try:
                doc = json.loads(tf.extractfile(m).read().decode("utf-8", "ignore"))
            except Exception:
                continue
            for s in _walk_strings(doc):
                if s.lower().endswith(".wem") and 4 < len(s) < 300:
                    found.add(s)
            if progress and i % 500 == 0:
                progress(i, len(members), f"Reading voice data ({len(found)} paths)")
    return sorted(found)


def _extract_voicepaths(data):
    doc = json.loads(data.decode("utf-8", "ignore"))
    items = doc if isinstance(doc, list) else list(doc.values())
    out = []
    for it in items:
        if isinstance(it, dict):
            vp = it.get("VoicePath")
            if isinstance(vp, str) and vp:
                out.append(vp)
    return out


def _fetch_voice_paths(game, src, progress=None):
    if src.get("voice_subtree"):
        if progress:
            progress(0, 1, f"Downloading {game} voice archive...")
        return _extract_wem_from_targz(_gitlab_archive(src, src["voice_subtree"]), progress)
    paths = []
    for relative in src.get("voice_files", []):
        if progress:
            progress(0, 1, f"Downloading {relative}...")
        paths.extend(_extract_voicepaths(_gitlab_raw(src, relative)))
    return paths


_AUDIO_NAME_RE = re.compile(r"^[A-Za-z][\w]{2,}$")


# id->name pairs are the only handle for objects whose id is not the hash of the name (MusicSegment and friends).
def _collect_audio_meta(node, names, id_names, category):
    if isinstance(node, dict):
        strs = [v for v in node.values() if isinstance(v, str) and _AUDIO_NAME_RE.match(v)]
        ints = [v for v in node.values() if isinstance(v, int) and v > 0xFFFF]
        for v in node.values():
            if isinstance(v, str) and _AUDIO_NAME_RE.match(v):
                names.add(v)
        if len(strs) == 1 and len(ints) == 1:
            id_names[str(ints[0])] = [strs[0], category]
        for v in node.values():
            _collect_audio_meta(v, names, id_names, category)
    elif isinstance(node, list):
        for v in node:
            _collect_audio_meta(v, names, id_names, category)


def _extract_audio_meta_from_targz(blob, subtree, progress=None):
    leaf = subtree.rstrip("/").split("/")[-1]
    names = set()
    id_names = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        members = [m for m in tf.getmembers() if m.isfile() and m.name.endswith(".json")]
        for i, m in enumerate(members):
            parts = m.name.split("/")
            category = parts[parts.index(leaf) + 1] if leaf in parts and parts.index(leaf) + 1 < len(parts) else leaf
            try:
                doc = json.loads(tf.extractfile(m).read().decode("utf-8", "ignore"))
            except Exception:
                continue
            _collect_audio_meta(doc, names, id_names, category)
            if progress and i % 100 == 0:
                progress(i, len(members), f"Reading audio metadata ({len(names)} names, {len(id_names)} labels)")
    return sorted(names), id_names


def _event_name_pattern(prefixes):
    alt = b"|".join(re.escape(x.strip().encode()) for x in prefixes.split(",") if x.strip())
    return re.compile(rb"(?<![0-9A-Za-z_])(?:" + alt + rb")_[0-9A-Za-z_]{2,120}")


def _scan_targz_names(blob, pattern, progress=None):
    names = set()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf:
            if not m.isfile():
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            for hit in pattern.finditer(f.read()):
                names.add(hit.group().decode("ascii", "ignore"))
    return names


def _gitea_raw(source, relative_path):
    host = source.get("host", "https://git.mero.moe")
    return _http_get(f"{host}/{source['project']}/raw/branch/{source['ref']}/{relative_path}")


_WWISE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,}$")


# MusicPlayerConfig ties each track to its Play_* event and to the title's TextMap key.
# Field names are obfuscated and rotate every patch: they are recognized by value shape.
def _fetch_zzz_music_meta(src, progress=None):
    names, id_names = set(), {}
    if progress:
        progress(0, 1, "Downloading ZZZ music config...")
    cfg = json.loads(_gitea_raw(src, src["music_cfg"]).decode("utf-8", "ignore"))
    rows = next(iter(cfg.values())) if isinstance(cfg, dict) else cfg
    tracks = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        event = tmkey = None
        for value in row.values():
            if isinstance(value, str):
                if value.startswith("Play_"):
                    event = value
                elif value.startswith("TextMap_"):
                    tmkey = value
        if event and tmkey:
            tracks.append((event, tmkey))
            names.add(event)
    titles = {}
    if tracks:
        if progress:
            progress(0, 1, "Downloading ZZZ text map (~45 MB)...")
        wanted = {tmkey for _, tmkey in tracks}
        for relative in (src["textmap"], src.get("textmap_overwrite")):
            if not relative:
                continue
            textmap = json.loads(_gitea_raw(src, relative).decode("utf-8", "ignore"))
            # The Overwrite comes later and wins: it is where patches fix the titles.
            titles.update({k: v for k, v in textmap.items()
                           if k in wanted and isinstance(v, str)})
    for event, tmkey in tracks:
        title = titles.get(tmkey)
        if title:
            id_names[str(fnv1_32(event))] = [title, "Music"]
    for relative in src.get("event_cfg_files", []):
        if progress:
            progress(0, 1, f"Downloading {relative}...")
        try:
            doc = json.loads(_gitea_raw(src, relative).decode("utf-8", "ignore"))
        except Exception:
            continue
        for value in _walk_strings(doc):
            if "{" not in value and _WWISE_NAME_RE.match(value):
                names.add(value)
    return sorted(names), id_names


def _fetch_audio_meta(src, progress=None):
    names, id_names = set(), {}
    # ZZZ: track titles from the music player config (gitea), no gitlab archive.
    if src.get("music_cfg"):
        n, ids = _fetch_zzz_music_meta(src, progress)
        names.update(n)
        id_names.update(ids)
    # GI: json subtree -> hash-attachable names + id->name tables (MusicSegment).
    if src.get("audio_subtree"):
        if progress:
            progress(0, 1, "Downloading audio metadata...")
        n, ids = _extract_audio_meta_from_targz(
            _gitlab_archive(src, src["audio_subtree"]), src["audio_subtree"], progress)
        names.update(n)
        id_names.update(ids)
    # SR: event names (Ev_...) from targeted tables and subtrees, raw scan.
    if src.get("event_files") or src.get("event_subtrees"):
        pattern = _event_name_pattern(src.get("event_prefixes", "Ev,Play,Stop"))
        for rel in src.get("event_files", []):
            if progress:
                progress(0, 1, f"Downloading {rel}...")
            names.update(h.group().decode("ascii", "ignore")
                         for h in pattern.finditer(_gitlab_raw(src, rel)))
        for subtree in src.get("event_subtrees", []):
            if progress:
                progress(0, 1, f"Downloading {subtree}...")
            names.update(_scan_targz_names(_gitlab_archive(src, subtree), pattern, progress))
    return sorted(names), id_names


# Returns voice_paths, names (hash-attachable) and id_names {id: [name, category]}, with a local cache.
def download_online_data(game, progress=None, config=None, force=False):
    src = None
    if config:
        src = (config.get("voice_sources") or {}).get(game)
    src = src or VOICE_SOURCES.get(game)
    if not src:
        raise RuntimeError(f"no online data source configured for {game}")
    cache = _voice_cache_file(game)
    if cache.exists() and not force:
        try:
            meta = json.loads(cache.read_text(encoding="utf-8"))
            if "names" in meta:
                return meta
        except Exception:
            pass
    voice_paths = sorted(set(_fetch_voice_paths(game, src, progress)))
    names, id_names = _fetch_audio_meta(src, progress)
    meta = {"game": game, "label": src.get("label", ""),
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "voice_count": len(voice_paths), "names_count": len(names),
            "labels_count": len(id_names),
            "voice_paths": voice_paths, "names": names, "id_names": id_names}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(meta), encoding="utf-8")
    return meta


def download_online_voice_paths(game, progress=None, config=None, force=False):
    meta = download_online_data(game, progress=progress, config=config, force=force)
    return meta.get("voice_paths", []), meta


def resolve_online_labels(id_names, index):
    out = []
    for sid, entry in id_names.items():
        try:
            oid = int(sid)
        except (TypeError, ValueError):
            continue
        if oid not in index.event_actions and oid not in index.object_ids:
            continue
        name, category = (entry if isinstance(entry, list) else (entry, "Audio"))
        out.append((name, category, oid, _wems_for_object(index, oid)))
    return out


# Heads: ZZZ '<lang>/Ex/', GI '<lang>' + backslash, SR '<lang>/voice/' with _m/_f suffixes.
def _online_heads(game, langs):
    heads = []
    for lang in langs:
        if game == "GI":
            heads.append((lang + "\\", lang, False))
        elif game == "SR":
            heads.append((lang + "/voice/", lang, True))
        elif game == "ZZZ":
            heads.append((lang + "/Ex/", lang, False))
        else:
            heads.append((lang + "\\", lang, False))
            heads.append((lang + "/voice/", lang, True))
    return heads


_TAKE_SUFFIX_RE = re.compile(r"_(\d{2,3})(\.wem)?$", re.I)
_MIN_TAKES_PROBED = 15
_TAKE_PROBE_MARGIN = 4
_MAX_EXTRAPOLATED_CANDIDATES = 40_000_000


def _split_voice_path(path):
    cut = max(path.rfind("\\"), path.rfind("/"))
    if cut < 0:
        return "", path
    return path[:cut], path[cut + 1:]


# VO_heroine -> heroine.
def _speaker_of_folder(folder):
    tail = _split_voice_path(folder)[1].lower()
    return tail[3:] if tail.startswith("vo_") else ""


# Numbered bases -> {shape: known numbers}, with shape = (digits, extension).
# GI's 2-digit takes and SR's 3-digit ids have different caps: never the same range.
def _take_families(voice_paths):
    families = defaultdict(lambda: defaultdict(set))
    for path in voice_paths:
        take = _TAKE_SUFFIX_RE.search(path)
        if take:
            shape = (len(take.group(1)), take.group(2) or "")
            families[path[:take.start()]][shape].add(int(take.group(1)))
    return families


# Two generators: every leaf pattern on every speaker folder, and takes renumbered up to the family maximum.
# A candidate only enters if it hits an id actually present, so zero false positives.
def extrapolate_voice_names(live, voice_paths, external_ids, resolved, progress=None):
    missing = set(external_ids) - set(resolved)
    if not missing or not live:
        return 0

    speaker_prefixes = {}
    patterns = set()
    for path in voice_paths:
        folder, leaf = _split_voice_path(path)
        if not folder:
            continue
        speaker = _speaker_of_folder(folder)
        if speaker and leaf.lower().startswith("vo_" + speaker + "_"):
            cut = len(speaker) + 4
            patterns.add(leaf[cut:])
            speaker_prefixes[folder] = path[:len(folder) + 1] + leaf[:cut]

    families = _take_families(voice_paths)
    if not patterns and not families:
        return 0
    take_cap = defaultdict(int)
    for base, shapes in families.items():
        folder = _split_voice_path(base)[0]
        for shape, numbers in shapes.items():
            key = (folder, shape)
            take_cap[key] = max(take_cap[key], max(numbers))
    for key in take_cap:
        digits = key[1][0]
        take_cap[key] = min(max(_MIN_TAKES_PROBED, take_cap[key] + _TAKE_PROBE_MARGIN),
                            10 ** digits - 1)

    found = 0
    budget = _MAX_EXTRAPOLATED_CANDIDATES
    steps = len(live) * (len(speaker_prefixes) + len(families))
    done = 0
    for head, lang, uses, state in live:
        suffixes = ("", "_m", "_f") if uses else ("",)
        pattern_tails = [(_vo_tail(pattern, suffix).decode(), _vo_tail(pattern.lower(), suffix))
                         for pattern in patterns for suffix in suffixes]
        take_tails = {}
        for (_folder, shape) in take_cap:
            if shape in take_tails:
                continue
            digits, extension = shape
            take_tails[shape] = [[(_vo_tail("_%0*d%s" % (digits, number, extension), suffix).decode(),
                                   _vo_tail("_%0*d%s" % (digits, number, extension), suffix))
                                  for suffix in suffixes]
                                 for number in range(0, 10 ** digits)]

        for prefix in speaker_prefixes.values():
            if budget <= 0 or not missing:
                break
            prefix_state = _fnv64_feed(prefix.lower().encode(), state)
            for name, tail in pattern_tails:
                budget -= 1
                hashed = _fnv64_feed(tail, prefix_state)
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
            folder = _split_voice_path(base)[0]
            base_state = _fnv64_feed(base.lower().encode(), state)
            for shape, numbers in shapes.items():
                for number in range(1, take_cap[(folder, shape)] + 1):
                    if number in numbers:
                        continue
                    for name, tail in take_tails[shape][number]:
                        budget -= 1
                        hashed = _fnv64_feed(tail, base_state)
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


# FNV-1 64 of '<language><sep><path>.wem' lowercased, rule calibrated on the data; returns {id: (name, language)}.
def resolve_online_voices(game, voice_paths, external_ids, progress=None):
    external_ids = set(external_ids)
    paths = [p for p in voice_paths if p]
    heads = _online_heads(game, VO_ONLINE_LANGS.get(game, VO_ONLINE_LANGS["GI"]))
    head_states = {h: _fnv64_feed(h.lower().encode()) for h, _, _ in heads}
    suf_for = lambda uses: ("", "_m", "_f") if uses else ("",)

    step = max(1, len(paths) // 4000)
    sample = paths[::step][:4000]
    live = []
    for head, lang, uses in heads:
        st = head_states[head]
        for p in sample:
            pl = p.lower()
            if any(_fnv64_feed(_vo_tail(pl, suf), st) in external_ids for suf in suf_for(uses)):
                live.append((head, lang, uses, st))
                break
    if not live:
        live = [(h, l, u, head_states[h]) for h, l, u in heads]

    resolved = {}
    total = len(paths)
    for i, path in enumerate(paths):
        pl = path.lower()
        for head, lang, uses, st in live:
            for suf in suf_for(uses):
                h = _fnv64_feed(_vo_tail(pl, suf), st)
                if h in external_ids and h not in resolved:
                    resolved[h] = (head + _vo_tail(path, suf).decode(), lang)
        if progress and i % 4000 == 0:
            progress(i, total, f"Resolving voice names ({len(resolved)} found)")
    extrapolated = extrapolate_voice_names(live, paths, external_ids, resolved, progress)
    if progress:
        progress(total, total, f"Voice names: {len(resolved)} resolved "
                               f"({extrapolated} extrapolated)")
    return resolved


# ---------------------------------------------------------------- CLI mode

def run_cli(args):
    interactive = sys.stdout.isatty()
    state = {"t0": time.time(), "last": 0.0, "phase": ""}

    def prog(cur, tot, msg):
        now = time.time()
        if msg != state["phase"]:
            state["phase"] = msg
            state["t0"] = now
        pct = (cur / tot * 100) if tot else 0
        elapsed = now - state["t0"]
        eta = (elapsed / cur * (tot - cur)) if cur else 0
        line = f"{msg} {cur}/{tot} ({pct:.1f}%) {elapsed:.0f}s elapsed, ETA {eta:.0f}s"
        if interactive:
            sys.stdout.write("\r" + line.ljust(90))
            sys.stdout.flush()
        elif now - state["last"] >= 5 or cur >= tot:
            # redirected output: one line every 5s so progress is visible in logs/files
            state["last"] = now
            print(line, flush=True)

    names = []
    vo_data = None
    if args.names:
        names.extend(load_names(args.names))
    if args.vo_import:
        imported = import_vo_sources(args.vo_import, progress=prog)
        vo_data = {"vo_prefixes": [], "vo_sources": imported}
        print(f"  voice paths imported: {len(imported)}")
    if args.vo_in:
        vo_data = json.loads(Path(args.vo_in).read_text(encoding="utf-8"))
        print(f"  voice data loaded: {len(vo_data['vo_prefixes'])} prefixes, "
              f"{len(vo_data['vo_sources'])} voice names")
    if args.vo_online:
        meta = download_online_data(args.vo_online, progress=prog,
                                    config=load_config(), force=args.vo_online_refresh)
        vo_data = {"online": True, "game": args.vo_online,
                   "voice_paths": meta.get("voice_paths", []),
                   "id_names": meta.get("id_names", {})}
        names.extend(meta.get("names", []))
        print(f"  online data [{args.vo_online}]: {len(meta.get('voice_paths', []))} voice, "
              f"{len(meta.get('names', []))} names, {len(meta.get('id_names', {}))} id-labels "
              f"(updated {meta.get('updated')})")

    if args.harvest:
        t0 = time.time()
        prefixes = args.harvest_prefixes.split(",")
        harvest = harvest_all(args.harvest, prefixes, progress=prog, game=args.harvest_game)
        harvested = harvest["names"]
        vo_data = {"vo_prefixes": harvest["vo_prefixes"], "vo_sources": harvest["vo_sources"]}
        print()
        print(f"  harvested: {len(harvested)} names, {len(vo_data['vo_prefixes'])} voice prefixes, "
              f"{len(vo_data['vo_sources'])} voice names in {time.time() - t0:.0f}s")
        if args.harvest_out:
            Path(args.harvest_out).write_text(chr(10).join(harvested) + chr(10), encoding="utf-8")
            print(f"  candidates written -> {args.harvest_out}")
        if args.vo_out:
            Path(args.vo_out).write_text(json.dumps(vo_data, indent=1), encoding="utf-8")
            print(f"  voice data written -> {args.vo_out}")
        names.extend(harvested)

    if not args.scan:
        return

    names = list(dict.fromkeys(names))
    index = scan_folder(args.scan, progress=prog)
    print()
    for k, v in sorted(index.stats.items()):
        print(f"  {k}: {v}")
    linked = sum(1 for e in index.event_actions if index.wems_for_event(e))
    print(f"  events with >=1 resolved wem: {linked}/{len(index.event_actions)}")

    if names or vo_data:
        matches, unmatched = match_names(names, index) if names else ([], [])
        matches = apply_exported_matches(args.names, index, matches)
        if vo_data and index.external_locations:
            if vo_data.get("online"):
                resolved = resolve_online_voices(vo_data["game"], vo_data["voice_paths"],
                                                 index.external_locations.keys(), progress=prog)
                label = lambda lang, path: path
            else:
                resolved = crack_vo_names(vo_data["vo_prefixes"], vo_data["vo_sources"],
                                          index.external_locations.keys(), progress=prog)
                label = lambda lang, path: f"{lang}/Ex/{path}"
            print()
            for ext_hash, (path, lang) in resolved.items():
                matches.append(NameMatch(label(lang, path), "External", [ext_hash], ext_hash))
            pct = len(resolved) / max(1, len(index.external_locations)) * 100
            print(f"  voice names recovered: {len(resolved)} / {len(index.external_locations)} externals ({pct:.1f}%)")
        if vo_data and vo_data.get("id_names"):
            labels = resolve_online_labels(vo_data["id_names"], index)
            for lname, category, oid, wems in labels:
                matches.append(NameMatch(lname, category, wems, oid))
            print(f"  audio labels applied: {len(labels)} (MusicSegment & co.)")
        matches = dedupe_matches(matches)
        unmatched = prune_unmatched(matches, unmatched)
        by_kind = defaultdict(int)
        with_wems = 0
        located = 0
        for m in matches:
            by_kind[m.kind] += 1
            if m.wem_ids:
                with_wems += 1
                if any(index.wem_locations.get(w) or index.external_locations.get(w) for w in m.wem_ids):
                    located += 1
        print(f"\n  names: {len(names)}  unmatched: {len(unmatched)}")
        for k, v in sorted(by_kind.items()):
            print(f"  {k} matches: {v}")
        print(f"  matches with wems: {with_wems} (located: {located})")
        if args.export_txt:
            n = export_txt(matches, args.export_txt)
            print(f"  exported {n} event names -> {args.export_txt}")
        if args.export_json:
            src = args.names or f"harvest:{args.harvest}"
            n = export_json(matches, unmatched, index, args.scan, src, args.export_json)
            print(f"  exported {n} events -> {args.export_json}")
        for m in matches[: args.sample]:
            locs = []
            for w in m.wem_ids[:4]:
                ll = index.wem_locations.get(w) or index.external_locations.get(w)
                locs.append(f"{w}@{ll[0].label()}" if ll else f"{w}@?")
            print(f"    [{m.kind}] {m.name} -> {len(m.wem_ids)} wem  {locs}")


# ---------------------------------------------------------------- GUI

def run_gui():

    # ---------- small widgets

    class ClickSlider(QSlider):
        # Click on the track = jump straight to that point (drag then continues as usual).
        clickedValue = pyqtSignal(int)

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), int(event.position().x()), self.width())
                self.setValue(value)
                self.clickedValue.emit(value)
            super().mousePressEvent(event)

    def fmt_ms(ms):
        s = max(0, int(ms)) // 1000
        return f"{s // 60}:{s % 60:02d}"

    # Prototype colors, the system theme is not enough.
    # Without these the cards, rail and tree lose the contrasts the UI was designed on.
    C_WIN, C_BASE, C_ALT = "#353535", "#2a2a2a", "#303030"
    C_BTN, C_BTN_HI = "#3c3c3c", "#484848"
    C_LINE, C_LINE_SOFT = "#5a5a5a", "#444444"
    C_TXT, C_DIM, C_OFF = "#f0f0f0", "#9a9a9a", "#7f7f7f"
    C_ACC, C_SEL, C_HDR = "#d13438", "#4a4a4a", "#3a3a3a"

    TYPE_COLORS = {"voice": "#4ec9b0", "event": "#569cd6", "music": "#c586c0",
                   "bank": "#d7ba7d", "sync": "#9a9a9a", "all": "#d0d0d0"}

    # ---------- theme

    STYLESHEET = f"""
    QWidget {{
        background: {C_WIN};
        color: {C_TXT};
        font-family: "Segoe UI";
        font-size: 12px;
    }}

    QLineEdit, QPlainTextEdit, QTreeWidget {{
        background: {C_BASE};
        border: 1px solid {C_LINE};
        color: {C_TXT};
        selection-background-color: {C_SEL};
    }}
    QLineEdit {{ padding: 3px 6px; }}
    QLineEdit#searchEdit {{ border: 1px solid {C_ACC}; }}

    QPushButton {{
        background: {C_BTN};
        border: 1px solid {C_LINE};
        color: {C_TXT};
        padding: 4px 12px;
    }}
    QPushButton:hover {{ background: {C_BTN_HI}; }}
    QPushButton:default {{ border-color: {C_ACC}; }}
    QPushButton:disabled {{
        background: #333333;
        color: {C_OFF};
        border-color: {C_LINE_SOFT};
    }}

    QFrame#sourceBox {{ background: #323232; border: 1px solid {C_LINE_SOFT}; }}
    QFrame#sourceBox[on="true"] {{ background: {C_HDR}; border: 1px solid {C_LINE}; }}
    QFrame#sourceBox QWidget {{ background: transparent; }}
    QFrame#filterRail {{ background: {C_WIN}; border: 1px solid {C_LINE_SOFT}; }}
    QFrame#filterRail QWidget {{ background: transparent; }}

    QLabel#railHeader {{ color: {C_OFF}; font-size: 10px; }}
    QLabel#sourceTag {{
        color: {C_OFF};
        border: 1px solid {C_LINE_SOFT};
        padding: 0px 5px;
    }}
    QLabel#sourceValue, QLabel#facetCount {{ color: {C_DIM}; }}

    QTreeWidget {{ alternate-background-color: {C_ALT}; outline: 0; }}
    QTreeWidget::item {{ padding: 2px 0px; border: 0px; }}
    QTreeWidget::item:selected, QTreeWidget::item:selected:active {{ background: {C_SEL}; color: {C_TXT}; }}

    QFrame#facetRow {{ background: transparent; border: 1px solid transparent; }}
    QFrame#facetRow:hover {{ background: {C_HDR}; }}
    QFrame#facetRow[on="true"] {{ background: {C_SEL}; border: 1px solid {C_LINE_SOFT}; }}
    QFrame#facetRow[off="true"] QLabel {{ color: {C_OFF}; }}

    QHeaderView::section {{
        background: {C_HDR};
        color: #d0d0d0;
        border: 0px;
        border-right: 1px solid {C_LINE_SOFT};
        border-bottom: 1px solid {C_LINE_SOFT};
        padding: 3px 6px;
    }}

    QTabBar::tab {{
        background: {C_ALT};
        border: 1px solid {C_LINE_SOFT};
        border-bottom: none;
        color: {C_DIM};
        padding: 4px 14px;
    }}
    QTabBar::tab:selected {{ background: {C_WIN}; color: {C_TXT}; }}
    QTabWidget::pane {{ border: 1px solid {C_LINE_SOFT}; top: -1px; }}

    QProgressBar {{ background: {C_BASE}; border: 1px solid {C_LINE}; }}
    QProgressBar::chunk {{ background: {C_ACC}; }}
    QStatusBar {{ background: #2f2f2f; }}
    QStatusBar QLabel {{ background: transparent; }}
    QStatusBar::item {{ border: 0px; }}

    QSlider::groove:horizontal {{ height: 4px; background: {C_SEL}; }}
    QSlider::sub-page:horizontal {{ background: {C_ACC}; }}
    QSlider::handle:horizontal {{
        width: 9px;
        height: 12px;
        margin: -5px 0px;
        background: #c8c8c8;
    }}

    QCheckBox::indicator {{
        width: 13px;
        height: 13px;
        background: {C_BASE};
        border: 1px solid #8a8a8a;
    }}
    QCheckBox::indicator:hover {{ border: 1px solid #b4b4b4; }}
    QCheckBox::indicator:checked {{ background: {C_ACC}; border: 1px solid #dcdcdc; }}
    QCheckBox::indicator:disabled {{ background: #333333; border: 1px solid #565656; }}

    QRadioButton::indicator {{
        width: 13px;
        height: 13px;
        border-radius: 7px;
        background: {C_BASE};
        border: 1px solid #8a8a8a;
    }}
    QRadioButton::indicator:hover {{ border: 1px solid #b4b4b4; }}
    QRadioButton::indicator:checked {{
        border: 1px solid #dcdcdc;
        background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 {C_ACC}, stop:0.55 {C_ACC}, stop:0.6 {C_BASE}, stop:1 {C_BASE});
    }}

    QScrollBar:vertical {{ background: {C_BASE}; width: 12px; }}
    QScrollBar:horizontal {{ background: {C_BASE}; height: 12px; }}
    QScrollBar::handle {{
        background: #4f4f4f;
        min-height: 20px;
        min-width: 20px;
    }}
    QScrollBar::handle:hover {{ background: #5c5c5c; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}

    QToolTip {{
        background: {C_HDR};
        color: {C_TXT};
        border: 1px solid {C_LINE};
    }}
    """

    class FacetRow(QFrame):

        clicked = pyqtSignal(str)

        def __init__(self, key, text, color):
            super().__init__()
            self.key = key
            self.setObjectName("facetRow")
            self.setProperty("on", False)
            self.setProperty("off", False)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            row = QHBoxLayout(self)
            row.setContentsMargins(6, 3, 6, 3)
            row.setSpacing(7)
            dot = QLabel()
            dot.setPixmap(type_swatch(color))
            dot.setFixedWidth(9)
            self.count_lbl = QLabel("")
            self.count_lbl.setObjectName("facetCount")
            row.addWidget(dot)
            row.addWidget(QLabel(text))
            row.addStretch(1)
            row.addWidget(self.count_lbl)

        def mouseReleaseEvent(self, event):
            if self.isEnabled():
                self.clicked.emit(self.key)

        def set_active(self, active):
            self.setProperty("on", bool(active))
            self.style().unpolish(self)
            self.style().polish(self)

        def set_count(self, total, usable):
            self.count_lbl.setText(f"{total:,}" if total else "")
            self.setEnabled(usable)
            self.setProperty("off", not usable)
            self.style().unpolish(self)
            self.style().polish(self)

    def type_swatch(color):
        pixmap = QPixmap(9, 9)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.fillRect(1, 1, 7, 7, QColor(color))
        painter.end()
        return pixmap

    def type_dot(color):
        return QIcon(type_swatch(color))

    def media_icon(kind, color):
        # Icons like XXAR's player buttons: solid triangle, double bar, square (radius 1).
        size, dpr = 15, 2
        pm = QPixmap(size * dpr, size * dpr)
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        if kind == "play":
            path = QPainterPath()
            path.moveTo(2.5, 1)
            path.lineTo(13.5, size / 2)
            path.lineTo(2.5, size - 1)
            path.closeSubpath()
            p.drawPath(path)
        elif kind == "pause":
            p.drawRoundedRect(QRectF(3, 1, 4, 13), 1, 1)
            p.drawRoundedRect(QRectF(9, 1, 4, 13), 1, 1)
        elif kind == "stop":
            p.drawRoundedRect(QRectF(1, 1, 13, 13), 1, 1)
        p.end()
        return QIcon(pm)

    USER_ROLE = Qt.ItemDataRole.UserRole

    def load_config():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_config(cfg):
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---------- workers

    class ScanWorker(QThread):
        progressed = pyqtSignal(int, int, str)
        finished_ok = pyqtSignal(object, object, object)
        failed = pyqtSignal(str)

        def __init__(self, folder, names, vo_data=None, names_path=""):
            super().__init__()
            self.folder = folder
            self.names = names
            self.names_path = names_path
            self.vo_data = vo_data
            self._cancel = False

        def cancel(self):
            self._cancel = True

        def run(self):
            try:
                index = scan_folder(self.folder, progress=self.progressed.emit,
                                    cancel=lambda: self._cancel)
                if self._cancel:
                    self.failed.emit("Scan cancelled")
                    return
                matches, unmatched = match_names(self.names, index, progress=self.progressed.emit)
                matches = apply_exported_matches(self.names_path, index, matches)
                if self.vo_data and index.external_locations:
                    if self.vo_data.get("online"):
                        resolved = resolve_online_voices(self.vo_data["game"],
                                                         self.vo_data["voice_paths"],
                                                         index.external_locations.keys(),
                                                         progress=self.progressed.emit)
                        vo_label = lambda lang, path: path
                    else:
                        resolved = crack_vo_names(self.vo_data.get("vo_prefixes", []),
                                                  self.vo_data.get("vo_sources", []),
                                                  index.external_locations.keys(),
                                                  progress=self.progressed.emit)
                        vo_label = lambda lang, path: f"{lang}/Ex/{path}"
                    for ext_hash, (path, lang) in resolved.items():
                        matches.append(NameMatch(vo_label(lang, path), "External", [ext_hash], ext_hash))
                if self.vo_data and self.vo_data.get("id_names"):
                    for lname, category, oid, wems in resolve_online_labels(self.vo_data["id_names"], index):
                        matches.append(NameMatch(lname, category, wems, oid))
                matches = dedupe_matches(matches)
                self.finished_ok.emit(index, matches, prune_unmatched(matches, unmatched))
            except Exception as e:
                self.failed.emit(str(e))

    class OnlineVoiceWorker(QThread):
        progressed = pyqtSignal(int, int, str)
        finished_ok = pyqtSignal(str, object)
        failed = pyqtSignal(str)

        def __init__(self, game, force=False):
            super().__init__()
            self.game = game
            self.force = force

        def run(self):
            try:
                meta = download_online_data(
                    self.game, progress=self.progressed.emit,
                    config=load_config(), force=self.force)
                self.finished_ok.emit(self.game, meta)
            except Exception as e:
                self.failed.emit(str(e))

    class HarvestWorker(QThread):
        progressed = pyqtSignal(int, int, str)
        finished_ok = pyqtSignal(object)
        failed = pyqtSignal(str)

        def __init__(self, folder, prefixes, game=None):
            super().__init__()
            self.folder = folder
            self.prefixes = prefixes
            self.game = game
            self._cancel = False

        def cancel(self):
            self._cancel = True

        def run(self):
            try:
                result = harvest_all(self.folder, self.prefixes,
                                     progress=self.progressed.emit,
                                     cancel=lambda: self._cancel, game=self.game)
                self.finished_ok.emit(result)
            except Exception as e:
                self.failed.emit(str(e))

    class ConvertWorker(QThread):
        done = pyqtSignal(str)
        failed = pyqtSignal(str)

        def __init__(self, loc, vgmstream, out_dir, stem):
            super().__init__()
            self.loc = loc
            self.vgmstream = vgmstream
            self.out_dir = out_dir
            self.stem = stem

        def run(self):
            try:
                wav = wem_to_wav(extract_wem_bytes(self.loc), self.vgmstream,
                                 self.out_dir, self.stem)
                self.done.emit(wav)
            except Exception as e:
                self.failed.emit(str(e))

    class VgmstreamWorker(QThread):
        progressed = pyqtSignal(int, int, str)
        done = pyqtSignal(str)
        failed = pyqtSignal(str)

        def run(self):
            try:
                self.done.emit(download_vgmstream(progress=self.progressed.emit))
            except Exception as e:
                self.failed.emit(str(e))

    # ---------- main window

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.resize(1180, 760)
            self.cfg = load_config()
            self.index = None
            self.matches = []
            self._names_lower = []
            self.unmatched = []
            self.scan_root = ""
            self.names_path = ""
            self.harvested = []
            self.harvest_vo = None
            self.online_payload = None
            self._online_names = []
            self._blocks_auto = True
            self._suppress_detect = False
            self._match_buckets = []
            self._match_langs = []
            self._type_icons = {}
            self.active_bucket = "all"
            self.worker = None
            self.harvest_worker = None
            self.convert_worker = None
            self.vgm_worker = None
            self.vgmstream = find_vgmstream()
            self.temp_dir = tempfile.mkdtemp(prefix="wemnamefinder_")
            self.player = QMediaPlayer(self)
            self.audio_out = QAudioOutput(self)
            self.audio_out.setVolume(int(self.cfg.get("volume", 80)) / 100)
            self.player.setAudioOutput(self.audio_out)
            self.player.positionChanged.connect(self.on_position_changed)
            self.player.durationChanged.connect(self.on_duration_changed)
            self.player.playbackStateChanged.connect(self.on_playback_state)
            self._loaded_loc = None
            self._pending_loc = None
            self._lookup_item = None

            self.tabs = QTabWidget()
            self.setCentralWidget(self.tabs)
            central = QWidget()
            self.tabs.addTab(central, "Search")
            layout = QVBoxLayout(central)

            # Row 1: game (single choice) + folder + scan.
            row1 = QHBoxLayout()
            self.game_buttons = {}
            self.game_group = QButtonGroup(self)
            for game in GAME_ORDER:
                radio = QRadioButton(GAME_UI[game]["label"])
                radio.setToolTip(f"{game}: decrittazione .blk {GAME_UI[game]['decrypt']}")
                radio.clicked.connect(lambda _=False, g=game: self.on_game_changed(g))
                self.game_group.addButton(radio)
                self.game_buttons[game] = radio
                row1.addWidget(radio)
            row1.addSpacing(10)
            self.folder_edit = QLineEdit(self.cfg.get("folder", ""))
            self.folder_edit.setPlaceholderText(
                "game root folder works too — pck files are found recursively (Persistent included)")
            self.folder_edit.textChanged.connect(self.on_folder_changed)
            row1.addWidget(self.folder_edit, 1)
            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(self.pick_folder)
            row1.addWidget(browse_btn)
            self.scan_btn = QPushButton("Scan")
            self.scan_btn.setDefault(True)
            self.scan_btn.clicked.connect(self.start_scan)
            row1.addWidget(self.scan_btn)
            layout.addLayout(row1)

            # Row 2: the three name sources, side by side.
            row2 = QHBoxLayout()
            row2.setSpacing(7)
            (harvest_box, self.harvest_chk, self.harvest_tag,
             self.harvest_val, _) = self._make_source_box("Client harvest", "", None)
            self.harvest_chk.setChecked(True)
            self.harvest_chk.setToolTip("Usa i nomi raccolti nel tab \"Generate names\"")
            row2.addWidget(harvest_box, 1)

            (online_box, self.online_chk, self.online_tag,
             self.online_val, self.online_btn) = self._make_source_box("Online names", "", "Update")
            self.online_chk.setChecked(True)
            self.online_btn.clicked.connect(self.start_online_fetch)
            row2.addWidget(online_box, 1)

            (file_box, self.file_chk, _file_tag,
             self.file_val, file_btn) = self._make_source_box("Names file", "txt / json", "Browse...")
            file_btn.clicked.connect(self.pick_names)
            row2.addWidget(file_box, 1)
            layout.addLayout(row2)

            # Body: filter rail + search and tree.
            body = QHBoxLayout()
            body.setSpacing(7)
            body.addWidget(self._build_filter_rail())

            right = QVBoxLayout()
            right.setSpacing(5)
            search_row = QHBoxLayout()
            search_row.addWidget(QLabel("Search:"))
            self.filter_edit = QLineEdit()
            self.filter_edit.setObjectName("searchEdit")
            self.filter_edit.setPlaceholderText(
                "search by event name (substring) or by ID — event/bank/wem id (exact match when numeric)")
            self.filter_edit.textChanged.connect(self.schedule_filter)
            search_row.addWidget(self.filter_edit, 1)
            right.addLayout(search_row)

            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["Name / Source", "Type", "ID", "Language", "Size"])
            header = self.tree.header()
            # Interactive: columns draggable by hand (double click on the separator = fit to content).
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            header.setStretchLastSection(False)
            # The name column absorbs the leftover space: without it an empty column shows up on the right.
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setCascadingSectionResizes(False)
            # Widths redone for the rail view: the old ones overflowed and scrollbars appeared.
            saved = self.cfg.get("column_widths_v2") or []
            # external ids are 64-bit: wide column
            defaults = [560, 116, 116, 70, 62]
            for i, width in enumerate(defaults):
                self.tree.setColumnWidth(i, saved[i] if i < len(saved) and saved[i] > 20 else width)
            self.tree.setFont(QFont("Segoe UI", 9))
            self.tree.setUniformRowHeights(True)
            self.tree.setAlternatingRowColors(True)
            self.tree.setRootIsDecorated(True)
            self.tree.itemExpanded.connect(self.fill_children)
            self.tree.itemSelectionChanged.connect(self.update_buttons)
            self.tree.itemDoubleClicked.connect(lambda *_: self.play_selected())
            right.addWidget(self.tree, 1)
            body.addLayout(right, 1)
            layout.addLayout(body, 1)

            rowp = QHBoxLayout()
            self.seek_slider = ClickSlider(Qt.Orientation.Horizontal)
            self.seek_slider.setRange(0, 0)
            self.seek_slider.sliderMoved.connect(self.player.setPosition)
            self.seek_slider.clickedValue.connect(self.player.setPosition)
            rowp.addWidget(self.seek_slider, 1)
            self.time_lbl = QLabel("0:00 / 0:00")
            rowp.addWidget(self.time_lbl)
            rowp.addSpacing(16)
            rowp.addWidget(QLabel("Vol"))
            self.vol_slider = ClickSlider(Qt.Orientation.Horizontal)
            self.vol_slider.setFixedWidth(110)
            self.vol_slider.setRange(0, 100)
            self.vol_slider.setValue(int(self.cfg.get("volume", 80)))
            self.vol_slider.valueChanged.connect(self.on_volume_changed)
            rowp.addWidget(self.vol_slider)
            self.vol_lbl = QLabel(f"{int(self.cfg.get('volume', 80))}%")
            self.vol_lbl.setFixedWidth(34)
            rowp.addWidget(self.vol_lbl)
            layout.addLayout(rowp)

            row5 = QHBoxLayout()
            btn_color = self.palette().buttonText().color()
            self.icon_play = media_icon("play", btn_color)
            self.icon_pause = media_icon("pause", btn_color)
            self.icon_stop = media_icon("stop", btn_color)
            self.play_btn = QPushButton("Play")
            self.play_btn.setIcon(self.icon_play)
            self.play_btn.clicked.connect(self.play_selected)
            self.play_btn.setEnabled(False)
            row5.addWidget(self.play_btn)
            self.stop_btn = QPushButton("Stop")
            self.stop_btn.setIcon(self.icon_stop)
            self.stop_btn.clicked.connect(self.player.stop)
            self.stop_btn.setEnabled(False)
            row5.addWidget(self.stop_btn)
            self.export_btn = QPushButton("Export WEM...")
            self.export_btn.clicked.connect(self.export_selected)
            self.export_btn.setEnabled(False)
            row5.addWidget(self.export_btn)
            self.export_names_btn = QPushButton("Export names...")
            self.export_names_btn.clicked.connect(self.export_names)
            self.export_names_btn.setEnabled(False)
            row5.addWidget(self.export_names_btn)
            row5.addStretch(1)
            self.play_lbl = QLabel("")
            row5.addWidget(self.play_lbl)
            layout.addLayout(row5)

            # Status bar: stats and progress, out of the way.
            bar = self.statusBar()
            self.status_lbl = QLabel("Ready")
            bar.addWidget(self.status_lbl)
            self.progress = QProgressBar()
            self.progress.setTextVisible(False)
            self.progress.setMaximumHeight(12)
            self.progress.setFixedWidth(170)
            # only visible while something is running
            self.progress.setVisible(False)
            bar.addPermanentWidget(self.progress)
            self.detail_lbl = QLabel("")
            bar.addPermanentWidget(self.detail_lbl)
            # The harvest reuses the same indicators instead of having its own.
            self.harvest_progress = self.progress
            self.harvest_status = self.status_lbl

            if not self.vgmstream:
                self.play_lbl.setText("vgmstream missing: it downloads on first play")

            self.filter_timer = QTimer(self)
            self.filter_timer.setSingleShot(True)
            self.filter_timer.setInterval(250)
            self.filter_timer.timeout.connect(self.apply_filter)

            self.tabs.addTab(self._build_generate_tab(), "Generate names")

            saved_game = self.cfg.get("game")
            if saved_game not in GAME_UI:
                saved_game = detect_game_from_path(self.folder_edit.text()) or DEFAULT_UI_GAME
            self.game_buttons[saved_game].setChecked(True)
            self.harvest_val.setText("nothing harvested yet")
            self.file_val.setText(Path(self.cfg.get("names", "")).name or "no file loaded")
            self.file_chk.setChecked(bool(self.cfg.get("names")))
            self.names_path = self.cfg.get("names", "")
            self._sync_game_ui()
            self._update_blocks_folder()

        # ---------- UI pieces

        def _make_source_box(self, title, tag_text, button_text):
            box = QFrame()
            box.setObjectName("sourceBox")
            box.setProperty("on", False)
            outer = QVBoxLayout(box)
            outer.setContentsMargins(8, 5, 8, 6)
            outer.setSpacing(4)

            top = QHBoxLayout()
            top.setSpacing(6)
            check = QCheckBox(title)
            tag = QLabel(tag_text)
            tag.setObjectName("sourceTag")
            top.addWidget(check)
            top.addStretch(1)
            top.addWidget(tag)
            outer.addLayout(top)

            bottom = QHBoxLayout()
            bottom.setSpacing(6)
            value = QLabel("—")
            value.setObjectName("sourceValue")
            bottom.addWidget(value, 1)
            button = None
            if button_text:
                button = QPushButton(button_text)
                button.setMaximumHeight(22)
                bottom.addWidget(button)
            outer.addLayout(bottom)
            check.toggled.connect(lambda on, frame=box: self._paint_source(frame, on))
            return box, check, tag, value, button

        @staticmethod
        def _paint_source(frame, on):
            frame.setProperty("on", bool(on))
            frame.style().unpolish(frame)
            frame.style().polish(frame)

        def _rail_header(self, text):
            label = QLabel(text)
            label.setObjectName("railHeader")
            return label

        # With hundreds of thousands of results text search alone is not enough.
        def _build_filter_rail(self):
            rail = QFrame()
            rail.setObjectName("filterRail")
            rail.setFixedWidth(198)
            column = QVBoxLayout(rail)
            column.setContentsMargins(7, 7, 7, 7)
            column.setSpacing(3)

            column.addWidget(self._rail_header("TYPE"))
            self.type_rows = {}
            for key, label in TYPE_BUCKETS:
                row = FacetRow(key, label, TYPE_COLORS[key])
                row.clicked.connect(self.on_facet_clicked)
                column.addWidget(row)
                self.type_rows[key] = row
            self.active_bucket = "all"
            self._paint_facets()

            column.addSpacing(8)
            self.lang_header = self._rail_header("LANGUAGE")
            column.addWidget(self.lang_header)
            self.lang_box = QVBoxLayout()
            self.lang_box.setSpacing(3)
            column.addLayout(self.lang_box)
            self.lang_checks = {}

            column.addSpacing(8)
            column.addWidget(self._rail_header("SHOW"))
            self.audio_only_chk = QCheckBox("Only with audio")
            self.audio_only_chk.stateChanged.connect(self.apply_filter)
            column.addWidget(self.audio_only_chk)
            column.addStretch(1)
            return rail

        # The game: one choice that drives everything else.

        def current_game(self):
            for game, radio in self.game_buttons.items():
                if radio.isChecked():
                    return game
            return DEFAULT_UI_GAME

        # ---------- game switching

        def on_game_changed(self, game):
            previous = self.cfg.get("game")
            folders = dict(self.cfg.get("folders") or {})
            current = self.folder_edit.text().strip()
            if previous in GAME_UI and previous != game and current:
                # remember where the other game was
                folders[previous] = current
            self.cfg["game"] = game
            self.cfg["folders"] = folders

            target = folders.get(game) or self.locate_game_folder(game)
            if target != current:
                # No install found: better an empty field than another game's path.
                # That path would get scanned as if it belonged to this game.
                self._suppress_detect = True
                self.folder_edit.setText(target)
                self.folder_edit.setPlaceholderText(
                    f"select the {GAME_UI[game]['label']} folder" if not target else
                    "game root folder works too — pck files are found recursively (Persistent included)")
                if not target:
                    self.harvest_edit.clear()
                self._suppress_detect = False

            # Names and voices belong to the previous game: keeping them would mix two games.
            self.harvested = []
            self.harvest_vo = None
            self.online_payload = None
            self._online_names = []
            self.harvest_val.setText("nothing harvested yet")
            self.harvest_preview.clear()
            self.harvest_vo_preview.clear()
            self.harvest_names_hdr.setText("Harvested event names")
            self.harvest_voice_hdr.setText("Harvested voice data")
            self.harvest_save_btn.setEnabled(False)
            self.harvest_save_vo_btn.setEnabled(False)

            known = {DEFAULT_HARVEST_PREFIXES} | set(HARVEST_PREFIXES_BY_GAME.values())
            if self.prefixes_edit.text().strip() in known:
                self.prefixes_edit.setText(default_harvest_prefixes(game))
            self._blocks_auto = True
            self._sync_game_ui()
            self._update_blocks_folder()

        def locate_game_folder(self, game):
            keys = GAME_UI[game]["match"]

            def pick(directory):
                try:
                    for child in directory.iterdir():
                        if child.is_dir() and any(k in child.name.lower() for k in keys):
                            return child
                except Exception:
                    pass
                return None

            current = Path(self.folder_edit.text().strip() or ".")
            for parent in list(current.parents)[:5]:
                found = pick(parent)
                if found is not None:
                    return str(found)

            roots = []
            for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                base = os.environ.get(variable)
                if base:
                    roots.append(Path(base) / "HoYoPlay" / "games")
                    roots.append(Path(base) / "miHoYo Launcher" / "games")
            for root in roots:
                if root.is_dir():
                    found = pick(root)
                    if found is not None:
                        return str(found)
            return ""

        # The folder dictates the game.
        def on_folder_changed(self, text):
            if self._suppress_detect:
                return
            detected = detect_game_from_path(text)
            if detected and not self.game_buttons[detected].isChecked():
                self.game_buttons[detected].setChecked(True)
                self.on_game_changed(detected)

        def _sync_game_ui(self):
            game = self.current_game()
            meta = GAME_UI[game]
            self.harvest_tag.setText(".blk " + meta["decrypt"])
            has_online = meta["has_online"]
            self.online_chk.setEnabled(has_online)
            self.online_btn.setEnabled(has_online)
            if has_online:
                self.online_tag.setText(online_source_short(game))
                if self.online_payload and self.online_payload.get("game") == game:
                    self.online_val.setText(
                        f"{len(self.online_payload['voice_paths']):,} voice · "
                        f"{len(self.online_payload['id_names']):,} labels")
                elif _voice_cache_file(game).exists():
                    self.online_val.setText("cached — loaded on Scan, Update to refresh")
                else:
                    self.online_val.setText("not fetched yet — press Update")
            else:
                self.online_tag.setText("—")
                self.online_chk.setChecked(False)
                self.online_val.setText("not needed — names are in the client")
            detail = ".blk " + meta["decrypt"]
            if has_online:
                detail += " · " + online_source_short(game)
            self.detail_lbl.setText(detail)

        def _update_blocks_folder(self):
            if not self._blocks_auto:
                return
            root = Path(self.folder_edit.text().strip() or ".")
            candidate = root.joinpath(*GAME_UI[self.current_game()]["blocks"])
            if candidate.is_dir():
                self.harvest_edit.setText(str(candidate))

        def _build_generate_tab(self):
            page = QWidget()
            lay = QVBoxLayout(page)

            row1 = QHBoxLayout()
            blocks_lbl = QLabel("Blocks folder:")
            blocks_lbl.setFixedWidth(80)
            blocks_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row1.addWidget(blocks_lbl)
            self.harvest_edit = QLineEdit(self.cfg.get("harvest_folder", ""))
            self.harvest_edit.setPlaceholderText("derived from the game folder — override only if needed")
            row1.addWidget(self.harvest_edit, 1)
            override_btn = QPushButton("Override…")
            override_btn.setFixedWidth(96)
            override_btn.clicked.connect(self.pick_harvest_folder)
            row1.addWidget(override_btn)
            lay.addLayout(row1)

            row2 = QHBoxLayout()
            prefix_lbl = QLabel("Prefixes:")
            prefix_lbl.setFixedWidth(80)
            prefix_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row2.addWidget(prefix_lbl)
            self.prefixes_edit = QLineEdit(self.cfg.get("prefixes")
                                           or default_harvest_prefixes(self.cfg.get("game")))
            row2.addWidget(self.prefixes_edit, 1)
            self.harvest_btn = QPushButton("Harvest")
            self.harvest_btn.setFixedWidth(96)
            self.harvest_btn.clicked.connect(self.start_harvest)
            row2.addWidget(self.harvest_btn)
            lay.addLayout(row2)

            # Two columns: event names and voice data, which the harvest collects together.
            columns = QHBoxLayout()
            columns.setSpacing(7)

            names_col = QVBoxLayout()
            names_col.setSpacing(3)
            self.harvest_names_hdr = QLabel("Harvested event names")
            names_col.addWidget(self.harvest_names_hdr)
            self.harvest_preview = QPlainTextEdit()
            self.harvest_preview.setReadOnly(True)
            self.harvest_preview.setPlaceholderText("harvested candidate names will appear here")
            names_col.addWidget(self.harvest_preview, 1)
            columns.addLayout(names_col, 1)

            voice_col = QVBoxLayout()
            voice_col.setSpacing(3)
            self.harvest_voice_hdr = QLabel("Harvested voice data")
            voice_col.addWidget(self.harvest_voice_hdr)
            self.harvest_vo_preview = QPlainTextEdit()
            self.harvest_vo_preview.setReadOnly(True)
            self.harvest_vo_preview.setPlaceholderText("voice prefixes and file names will appear here")
            voice_col.addWidget(self.harvest_vo_preview, 1)
            columns.addLayout(voice_col, 1)
            lay.addLayout(columns, 1)

            row3 = QHBoxLayout()
            self.harvest_save_btn = QPushButton("Save names TXT...")
            self.harvest_save_btn.clicked.connect(self.save_harvest)
            self.harvest_save_btn.setEnabled(False)
            row3.addWidget(self.harvest_save_btn)
            self.harvest_save_vo_btn = QPushButton("Save voice data JSON...")
            self.harvest_save_vo_btn.clicked.connect(self.save_harvest_voice)
            self.harvest_save_vo_btn.setEnabled(False)
            row3.addWidget(self.harvest_save_vo_btn)
            row3.addStretch(1)
            hint = QLabel("Harvested names feed Scan through the \"Client harvest\" box in the Search tab.")
            hint.setStyleSheet("color: gray")
            row3.addWidget(hint)
            lay.addLayout(row3)

            return page

        def save_harvest_voice(self):
            if not self.harvest_vo:
                return
            out, _ = QFileDialog.getSaveFileName(self, "Save voice data", "voice_data.json", "JSON (*.json)")
            if not out:
                return
            try:
                Path(out).write_text(json.dumps(self.harvest_vo, indent=1), encoding="utf-8")
                self.status_lbl.setText(f"Saved voice data to {Path(out).name}")
            except Exception as e:
                QMessageBox.warning(self, APP_NAME, f"Save failed: {e}")

        def pick_harvest_folder(self):
            p = QFileDialog.getExistingDirectory(self, "Blocks folder", self.harvest_edit.text() or "")
            if p:
                # explicit choice: never overwrite it again
                self._blocks_auto = False
                self.harvest_edit.setText(p)

        # ---------- harvest

        def start_harvest(self):
            if self.harvest_worker and self.harvest_worker.isRunning():
                self.harvest_worker.cancel()
                return
            folder = self.harvest_edit.text().strip()
            if not Path(folder).is_dir():
                QMessageBox.warning(self, APP_NAME, "Select a valid folder")
                return
            prefixes = [p for p in self.prefixes_edit.text().split(",") if p.strip()]
            if not prefixes:
                QMessageBox.warning(self, APP_NAME, "Enter at least one prefix")
                return
            game_key = self.current_game()
            self.cfg.update({"harvest_folder": folder, "prefixes": self.prefixes_edit.text(),
                             "game": game_key})
            save_config(self.cfg)
            self.harvest_btn.setText("Cancel")
            self.harvest_worker = HarvestWorker(folder, prefixes, game_key)
            self.harvest_worker.progressed.connect(self.on_harvest_progress)
            self.harvest_worker.finished_ok.connect(self.on_harvest_done)
            self.harvest_worker.failed.connect(self.on_harvest_failed)
            self.harvest_worker.start()

        def on_harvest_progress(self, cur, tot, msg):
            self.harvest_progress.setMaximum(max(tot, 1))
            self.harvest_progress.setValue(cur)
            self.harvest_status.setText(msg)

        def on_harvest_done(self, result):
            self.harvest_btn.setText("Harvest")
            self.progress.setVisible(False)
            names = result["names"] if isinstance(result, dict) else result
            if isinstance(result, dict):
                self.harvest_vo = {"vo_prefixes": result["vo_prefixes"],
                                   "vo_sources": result["vo_sources"]}
            self.harvested = names
            self.harvest_progress.setValue(self.harvest_progress.maximum())
            sources = self.harvest_vo.get("vo_sources", []) if self.harvest_vo else []
            prefixes = self.harvest_vo.get("vo_prefixes", []) if self.harvest_vo else []
            self.harvest_status.setText(
                f"{len(names)} candidate names harvested" + (f", {len(sources)} voice names" if sources else ""))
            self.harvest_val.setText(f"{len(names):,} names")
            self.harvest_chk.setChecked(bool(names))
            self.harvest_names_hdr.setText(f"Harvested event names — {len(names):,}")
            self.harvest_voice_hdr.setText(f"Harvested voice data — {len(sources):,}")
            self.harvest_save_btn.setEnabled(bool(names))
            self.harvest_save_vo_btn.setEnabled(bool(sources))
            preview = names[:5000]
            self.harvest_preview.setPlainText(
                "\n".join(preview) + (f"\n... {len(names) - 5000} more" if len(names) > 5000 else ""))
            vo_text = [f"vo_prefixes ({len(prefixes)}):"] + [f"  {x}" for x in prefixes[:40]]
            vo_text += ["", f"vo_sources ({len(sources)}):"] + [f"  {x}" for x in sources[:3000]]
            if not GAME_UI[self.current_game()]["has_online"]:
                vo_text.insert(0, "The client carries the full voice paths: no online source needed.\n")
            else:
                vo_text.insert(0, "This game keeps only partial voice data in the client — "
                                  "use \"Online names\" in the Search tab.\n")
            self.harvest_vo_preview.setPlainText("\n".join(vo_text))
            self._harvest_screenshot_hook()

        def on_harvest_failed(self, msg):
            self.harvest_btn.setText("Harvest")
            self.harvest_status.setText(msg)
            self._harvest_screenshot_hook()

        def _harvest_screenshot_hook(self):
            if os.environ.get("HSI_SCREENSHOT") and os.environ.get("HSI_AUTOHARVEST"):
                def _grab():
                    self.grab().save(os.environ["HSI_SCREENSHOT"])
                    QApplication.instance().quit()
                QTimer.singleShot(800, _grab)

        def save_harvest(self):
            if not self.harvested:
                return
            out, _ = QFileDialog.getSaveFileName(self, "Save harvested names", "harvested_names.txt", "Text (*.txt)")
            if not out:
                return
            try:
                Path(out).write_text("\n".join(self.harvested) + "\n", encoding="utf-8")
                self.harvest_status.setText(f"Saved {len(self.harvested)} names to {Path(out).name}")
            except Exception as e:
                QMessageBox.warning(self, APP_NAME, f"Save failed: {e}")

        # ---------- pickers

        def pick_names(self):
            p, _ = QFileDialog.getOpenFileName(self, "Names file", self.names_path or "",
                                               "Names (*.txt *.json);;All files (*)")
            if p:
                self.names_path = p
                self.file_val.setText(Path(p).name)
                self.file_chk.setChecked(True)

        def pick_folder(self):
            p = QFileDialog.getExistingDirectory(self, "Game folder", self.folder_edit.text() or "")
            if p:
                self.folder_edit.setText(p)

        # ---------- online names

        def start_online_fetch(self):
            if getattr(self, '_online_worker', None) and self._online_worker.isRunning():
                return
            game = self.current_game()
            if not GAME_UI[game]["has_online"]:
                return
            self.online_btn.setEnabled(False)
            self.online_val.setText("downloading...")
            self._online_worker = OnlineVoiceWorker(game, force=True)
            self._online_worker.progressed.connect(self.on_progress)
            self._online_worker.finished_ok.connect(self.on_online_ready)
            self._online_worker.failed.connect(self.on_online_failed)
            self._online_worker.start()

        def _online_payload_ready(self):
            return bool(self.online_payload) and self.online_payload.get("game") == self.current_game()

        def _load_online_cache(self):
            game = self.current_game()
            cache = _voice_cache_file(game)
            if not cache.exists():
                return False
            try:
                meta = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                return False
            self.online_payload = {"online": True, "game": game,
                                   "voice_paths": meta.get("voice_paths", []),
                                   "id_names": meta.get("id_names", {})}
            self._online_names = meta.get("names", [])
            self._sync_game_ui()
            return True

        def on_online_ready(self, game, meta):
            self.online_payload = {"online": True, "game": game,
                                   "voice_paths": meta.get("voice_paths", []),
                                   "id_names": meta.get("id_names", {})}
            self._online_names = meta.get("names", [])
            self.online_btn.setEnabled(True)
            self.online_chk.setChecked(True)
            self.online_val.setText(
                f"{len(self.online_payload['voice_paths']):,} voice · "
                f"{len(self.online_payload['id_names']):,} labels")
            if Path(self.folder_edit.text().strip()).is_dir():
                self.start_scan()

        def on_online_failed(self, msg):
            self.online_btn.setEnabled(True)
            self.online_val.setText("download failed")
            QMessageBox.warning(self, APP_NAME, f"Could not fetch names:\n{msg}")

        # ---------- scan

        def start_scan(self):
            if self.worker and self.worker.isRunning():
                self.worker.cancel()
                return
            folder = self.folder_edit.text().strip()
            if not Path(folder).is_dir():
                QMessageBox.warning(self, APP_NAME, "Select a valid folder")
                return

            names = []
            if self.file_chk.isChecked() and self.names_path:
                if not Path(self.names_path).is_file():
                    QMessageBox.warning(self, APP_NAME, "The selected names file no longer exists")
                    return
                try:
                    names.extend(load_names(self.names_path))
                except Exception as e:
                    QMessageBox.warning(self, APP_NAME, f"Cannot read names file: {e}")
                    return
            if self.harvest_chk.isChecked():
                names.extend(self.harvested)
            if self.online_chk.isChecked():
                names.extend(self._online_names)

            if self.online_chk.isChecked() and not self._online_payload_ready():
                self._load_online_cache()
                names.extend(self._online_names)
                names = list(dict.fromkeys(names))

            # Voice data: the online one wins, it is verified and complete.
            vo_data = None
            if self.online_chk.isChecked() and self.online_payload:
                vo_data = self.online_payload
            elif self.harvest_chk.isChecked() and self.harvest_vo:
                vo_data = self.harvest_vo

            if not names and not vo_data:
                QMessageBox.warning(
                    self, APP_NAME,
                    "No name source: pick a names file, run a harvest, or fetch the online names")
                return

            names = list(dict.fromkeys(names))
            folders = dict(self.cfg.get("folders") or {})
            folders[self.current_game()] = folder
            self.cfg.update({"names": self.names_path, "folder": folder,
                             "game": self.current_game(), "folders": folders})
            save_config(self.cfg)
            self.scan_root = folder
            self.tree.clear()
            self.export_names_btn.setEnabled(False)
            self.scan_btn.setText("Cancel")
            self.worker = ScanWorker(folder, names, vo_data,
                                     self.names_path if self.file_chk.isChecked() else "")
            self.worker.progressed.connect(self.on_progress)
            self.worker.finished_ok.connect(self.on_scan_done)
            self.worker.failed.connect(self.on_scan_failed)
            self.worker.start()

        def on_progress(self, cur, tot, msg):
            self.progress.setVisible(True)
            self.progress.setMaximum(max(tot, 1))
            self.progress.setValue(cur)
            self.status_lbl.setText(msg)

        def on_scan_failed(self, msg):
            self.progress.setVisible(False)
            self.scan_btn.setText("Scan")
            self.status_lbl.setText(msg)

        def on_scan_done(self, index, matches, unmatched):
            self.scan_btn.setText("Scan")
            self.progress.setVisible(False)
            self.index = index
            self.matches = matches
            self.unmatched = unmatched
            self.progress.setValue(self.progress.maximum())
            st = index.stats
            self.status_lbl.setText(
                f"{st['pck']:,} pck, {st['objects']:,} objects, {st['wem_ids']:,} wems — "
                f"matches: {len(matches):,}, unmatched: {len(unmatched):,}")
            self.export_names_btn.setEnabled(bool(matches))
            self.populate_tree()
            self._sync_game_ui()
            if os.environ.get("HSI_SCREENSHOT"):
                self._screenshot_after_scan()

        def _screenshot_after_scan(self):
            forced = os.environ.get("HSI_FILTER")
            if forced:
                self.filter_edit.setFocus()
                QTest.keyClicks(self.filter_edit, forced)
                QTest.qWait(700)
                if self._lookup_item is not None and self._lookup_item.childCount():
                    child = self._lookup_item.child(0)
                    self.tree.setCurrentItem(child)
                    if isinstance(child.data(0, USER_ROLE), WemLocation):
                        self.play_selected()

                def _grab2():
                    self.grab().save(os.environ["HSI_SCREENSHOT"])
                    QApplication.instance().quit()
                QTimer.singleShot(4000, _grab2)
                return
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                m = item.data(0, USER_ROLE)
                if m and m.wem_ids:
                    self.filter_edit.setText(str(m.wem_ids[0]))
                    self.apply_filter()
                    self.tree.expandItem(item)
                    if item.childCount():
                        child = item.child(0)
                        self.tree.setCurrentItem(child)
                        self.tree.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
                        if isinstance(child.data(0, USER_ROLE), WemLocation):
                            self.play_selected()
                    break

            def _grab():
                if os.environ.get("HSI_COLTEST"):
                    header = self.tree.header()
                    before = [self.tree.columnWidth(i) for i in range(5)]
                    # simulates the drag
                    header.resizeSection(1, before[1] + 120)
                    after = [self.tree.columnWidth(i) for i in range(5)]
                    print("larghezze prima:", before, "dopo:", after, flush=True)
                if os.environ.get("HSI_CLICKTEST"):
                    QTest.mouseClick(
                        self.seek_slider, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier,
                        QPoint(int(self.seek_slider.width() * 0.8), self.seek_slider.height() // 2))
                    QTest.qWait(400)
                self.grab().save(os.environ["HSI_SCREENSHOT"])
                QApplication.instance().quit()
            QTimer.singleShot(4000, _grab)

        # ---------- tree

        def populate_tree(self):
            # Pre-lowercased names: the filter does not recompute them on every keystroke.
            self._names_lower = [m.name.lower() for m in self.matches]
            self._match_buckets = [bucket_of_kind(m.kind) for m in self.matches]
            self._match_langs = [self._lang_of(m) for m in self.matches]
            self._refresh_facets()
            self.apply_filter()

        # First path segment: GI separates with backslash, SR and ZZZ with slash.
        @staticmethod
        def _lang_of(match):
            if match.kind != "External":
                return ""
            head = re.split(r"[\\/]", match.name, 1)[0]
            return head if head and not head.lower().endswith(".wem") else ""

        def _refresh_facets(self):
            counts = Counter(self._match_buckets)
            for key, _ in TYPE_BUCKETS:
                total = len(self.matches) if key == "all" else counts.get(key, 0)
                self.type_rows[key].set_count(total, key == "all" or total > 0)
            if not self.type_rows[self.active_bucket].isEnabled():
                self.active_bucket = "all"
            self._paint_facets()

            while self.lang_box.count():
                item = self.lang_box.takeAt(0)
                child = item.layout()
                if child is not None:
                    while child.count():
                        widget = child.takeAt(0).widget()
                        if widget is not None:
                            widget.deleteLater()
                    child.deleteLater()
            self.lang_checks = {}
            langs = Counter(lang for lang in self._match_langs if lang)
            self.lang_header.setVisible(bool(langs))
            for lang, total in sorted(langs.items()):
                row = QHBoxLayout()
                row.setSpacing(6)
                check = QCheckBox(lang)
                check.setChecked(True)
                check.stateChanged.connect(self.apply_filter)
                count = QLabel(f"{total:,}")
                count.setObjectName("facetCount")
                row.addWidget(check)
                row.addStretch(1)
                row.addWidget(count)
                self.lang_box.addLayout(row)
                self.lang_checks[lang] = check

        def on_facet_clicked(self, key):
            self.active_bucket = key
            self._paint_facets()
            self.apply_filter()

        def _paint_facets(self):
            for key, row in self.type_rows.items():
                row.set_active(key == self.active_bucket)

        def _active_bucket(self):
            return self.active_bucket

        # Rebuilding beats hiding rows one by one: 0.1s vs 1.4s on 111k rows.
        def rebuild_tree(self, matches, lookup_id=None):
            _t0 = time.time()
            self.tree.setUpdatesEnabled(False)
            self._lookup_item = None
            self.tree.clear()
            items = []
            if lookup_id is not None and self.index is not None:
                locs = (self.index.wem_locations.get(lookup_id, [])
                        + self.index.external_locations.get(lookup_id, []))
                if locs:
                    item = QTreeWidgetItem(["(direct wem id lookup)", "WEM", str(lookup_id), "", ""])
                    for loc in locs:
                        child = QTreeWidgetItem([loc.label(), "wem", str(lookup_id), loc.lang, f"{loc.size:,}"])
                        child.setData(0, USER_ROLE, loc)
                        item.addChild(child)
                    items.append(item)
                    self._lookup_item = item
            for m in matches:
                size_col = f"{len(m.wem_ids)} wems" if m.wem_ids else ""
                item = QTreeWidgetItem([m.name, m.kind, str(m.hash_id),
                                        self._lang_of(m), size_col])
                item.setIcon(0, self._type_icon(bucket_of_kind(m.kind)))
                item.setData(0, USER_ROLE, m)
                if m.wem_ids:
                    item.addChild(QTreeWidgetItem(["..."]))
                items.append(item)
            self.tree.addTopLevelItems(items)
            if self._lookup_item is not None:
                self._lookup_item.setExpanded(True)
            self.tree.setUpdatesEnabled(True)
            if os.environ.get("HSI_TIMING"):
                print(f"[timing] albero con {len(items)} righe: {time.time()-_t0:.2f}s", flush=True)

        def _type_icon(self, bucket):
            icon = self._type_icons.get(bucket)
            if icon is None:
                icon = type_dot(TYPE_COLORS.get(bucket, TYPE_COLORS["sync"]))
                self._type_icons[bucket] = icon
            return icon

        def fill_children(self, item):
            _t0 = time.time() if os.environ.get("HSI_TIMING") else None
            m = item.data(0, USER_ROLE)
            if m is None or item.childCount() != 1 or item.child(0).text(0) != "...":
                return
            item.takeChildren()
            for wid in m.wem_ids[:500]:
                locs = (self.index.wem_locations.get(wid, [])
                        + self.index.external_locations.get(wid, []))
                if not locs:
                    child = QTreeWidgetItem(["(not found in pcks)", "wem", str(wid), "", ""])
                    item.addChild(child)
                    continue
                for loc in locs:
                    child = QTreeWidgetItem([
                        loc.label(), "wem", str(wid), loc.lang, f"{loc.size:,}"])
                    child.setData(0, USER_ROLE, loc)
                    item.addChild(child)
            if len(m.wem_ids) > 500:
                item.addChild(QTreeWidgetItem([f"... {len(m.wem_ids) - 500} more wems", "", "", "", ""]))
            if _t0 is not None:
                print(f"[timing] espansione {len(m.wem_ids)} wem: {time.time()-_t0:.3f}s", flush=True)

        def schedule_filter(self):
            self.filter_timer.start()

        def apply_filter(self):
            text = self.filter_edit.text().strip().lower()
            as_id = int(text) if text.isdigit() else None
            bucket = self._active_bucket()
            wanted_langs = {name for name, check in self.lang_checks.items() if check.isChecked()}
            audio_only = self.audio_only_chk.isChecked()

            subset = []
            for i, match in enumerate(self.matches):
                if bucket != "all" and self._match_buckets[i] != bucket:
                    continue
                lang = self._match_langs[i]
                if lang and lang not in wanted_langs:
                    continue
                if audio_only and not match.wem_ids:
                    continue
                if text:
                    if text in self._names_lower[i]:
                        pass
                    elif as_id is not None and (as_id == match.hash_id or as_id in match.wem_ids):
                        pass
                    else:
                        continue
                subset.append(match)
            self.rebuild_tree(subset, lookup_id=as_id)
            if len(subset) != len(self.matches):
                self.status_lbl.setText(f"{len(subset):,} of {len(self.matches):,} results shown")

        def selected_location(self):
            items = self.tree.selectedItems()
            if not items:
                return None
            data = items[0].data(0, USER_ROLE)
            return data if isinstance(data, WemLocation) else None

        def update_buttons(self):
            loc = self.selected_location()
            playing = self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState
            self.play_btn.setEnabled(loc is not None or playing)
            self.export_btn.setEnabled(loc is not None)

        # ---------- playback / export

        def fetch_vgmstream(self):
            if self.vgm_worker and self.vgm_worker.isRunning():
                return
            self.play_lbl.setText("Downloading vgmstream...")
            self.vgm_worker = VgmstreamWorker()
            self.vgm_worker.progressed.connect(lambda c, t, m: self.play_lbl.setText(m))
            self.vgm_worker.done.connect(self.on_vgmstream_ready)
            self.vgm_worker.failed.connect(lambda e: self.play_lbl.setText(f"vgmstream download failed: {e}"))
            self.vgm_worker.start()

        def on_vgmstream_ready(self, path):
            self.vgmstream = path
            self.play_lbl.setText("")
            if self.selected_location() is not None:
                self.play_selected()

        def play_selected(self):
            _t0 = time.time() if os.environ.get("HSI_TIMING") else None
            loc = self.selected_location()
            if not self.vgmstream:
                self.fetch_vgmstream()
                return
            state = self.player.playbackState()
            if (loc is None or loc is self._loaded_loc) and state != QMediaPlayer.PlaybackState.StoppedState:
                if state == QMediaPlayer.PlaybackState.PlayingState:
                    self.player.pause()
                else:
                    self.player.play()
                return
            if loc is None:
                return
            if self.convert_worker and self.convert_worker.isRunning():
                return
            self.player.stop()
            self.player.setSource(QUrl())
            self.play_lbl.setText("Converting...")
            self._pending_loc = loc
            self._play_seq = getattr(self, "_play_seq", 0) + 1
            self.convert_worker = ConvertWorker(loc, self.vgmstream, self.temp_dir,
                                                f"wnf_{self._play_seq}")
            self.convert_worker.done.connect(self.on_wav_ready)
            self.convert_worker.failed.connect(lambda e: self.play_lbl.setText(f"Error: {e}"))
            self.convert_worker.start()
            if _t0 is not None:
                print(f"[timing] play_selected (thread GUI): {time.time()-_t0:.3f}s", flush=True)

        def on_wav_ready(self, wav_path):
            self.play_lbl.setText("")
            self._loaded_loc = self._pending_loc
            self.player.setSource(QUrl.fromLocalFile(wav_path))
            self.player.play()

        def on_position_changed(self, pos):
            if not self.seek_slider.isSliderDown():
                self.seek_slider.setValue(int(pos))
            self.time_lbl.setText(f"{fmt_ms(pos)} / {fmt_ms(self.player.duration())}")

        def on_duration_changed(self, dur):
            self.seek_slider.setRange(0, max(0, int(dur)))
            self.time_lbl.setText(f"{fmt_ms(self.player.position())} / {fmt_ms(dur)}")

        def on_playback_state(self, state):
            if state == QMediaPlayer.PlaybackState.PlayingState:
                self.play_btn.setText("Pause")
                self.play_btn.setIcon(self.icon_pause)
            else:
                self.play_btn.setText("Play")
                self.play_btn.setIcon(self.icon_play)
            self.stop_btn.setEnabled(state != QMediaPlayer.PlaybackState.StoppedState)
            if state == QMediaPlayer.PlaybackState.StoppedState:
                self.seek_slider.setValue(0)
                self.time_lbl.setText(f"0:00 / {fmt_ms(self.player.duration())}")
            self.update_buttons()

        def on_volume_changed(self, value):
            self.audio_out.setVolume(value / 100)
            self.vol_lbl.setText(f"{value}%")
            self.cfg["volume"] = value

        def export_selected(self):
            loc = self.selected_location()
            if not loc:
                return
            items = self.tree.selectedItems()
            wid = items[0].text(2)
            out, _ = QFileDialog.getSaveFileName(self, "Export WEM", f"{wid}.wem", "WEM (*.wem)")
            if not out:
                return
            try:
                Path(out).write_bytes(extract_wem_bytes(loc))
                self.play_lbl.setText(f"Exported {Path(out).name}")
            except Exception as e:
                QMessageBox.warning(self, APP_NAME, f"Export failed: {e}")

        def export_names(self):
            if not self.matches:
                return
            out, sel = QFileDialog.getSaveFileName(
                self, "Export names", "event_names.json",
                "JSON (*.json);;Text (*.txt)")
            if not out:
                return
            ext = Path(out).suffix.lower()
            if not ext:
                out += ".txt" if sel.startswith("Text") else ".json"
                ext = Path(out).suffix.lower()
            try:
                if ext == ".txt":
                    n = export_txt(self.matches, out)
                    self.play_lbl.setText(f"Exported {n} event names to {Path(out).name}")
                else:
                    n = export_json(self.matches, self.unmatched, self.index,
                                    self.scan_root, self.names_path, out)
                    self.play_lbl.setText(f"Exported {n} events to {Path(out).name}")
            except Exception as e:
                QMessageBox.warning(self, APP_NAME, f"Export failed: {e}")

        def closeEvent(self, event):
            self.cfg["column_widths_v2"] = [self.tree.columnWidth(i) for i in range(self.tree.columnCount())]
            self.cfg["game"] = self.current_game()
            folders = dict(self.cfg.get("folders") or {})
            if self.folder_edit.text().strip():
                folders[self.current_game()] = self.folder_edit.text().strip()
            self.cfg["folders"] = folders
            save_config(self.cfg)
            if self.worker and self.worker.isRunning():
                self.worker.cancel()
                self.worker.wait(3000)
            if self.harvest_worker and self.harvest_worker.isRunning():
                self.harvest_worker.cancel()
                self.harvest_worker.wait(3000)
            super().closeEvent(event)

    # With the light title bar the window clashes with the rest.
    def use_dark_titlebar(widget):
        if sys.platform != "win32":
            return
        try:
            handle = int(widget.winId())
            flag = ctypes.c_int(1)
            # DWMWA_USE_IMMERSIVE_DARK_MODE, old and new value
            for attribute in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    handle, attribute, ctypes.byref(flag), ctypes.sizeof(flag))
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    win = MainWindow()
    win.show()
    use_dark_titlebar(win)

    if os.environ.get("HSI_TAB"):
        win.tabs.setCurrentIndex(int(os.environ["HSI_TAB"]))
    if os.environ.get("HSI_AUTOSCAN"):
        QTimer.singleShot(300, win.start_scan)
    elif os.environ.get("HSI_AUTOHARVEST"):
        win.tabs.setCurrentIndex(1)
        QTimer.singleShot(300, win.start_harvest)
    elif os.environ.get("HSI_SCREENSHOT"):
        def _shot():
            win.grab().save(os.environ["HSI_SCREENSHOT"])
            app.quit()
        QTimer.singleShot(1500, _shot)

    sys.exit(app.exec())


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--scan", help="folder to scan (CLI mode)")
    parser.add_argument("--names", help="txt file with names")
    parser.add_argument("--sample", type=int, default=10, help="how many matches to print")
    parser.add_argument("--export-json", help="write matched events (with ids/wems/sources) to this json")
    parser.add_argument("--export-txt", help="write the clean matched event-name list to this txt")
    parser.add_argument("--harvest", help="folder to raw-scan for candidate event names (e.g. the game Blocks folder)")
    parser.add_argument("--harvest-prefixes", default=DEFAULT_HARVEST_PREFIXES, help=f"comma-separated name prefixes (default: {DEFAULT_HARVEST_PREFIXES})")
    parser.add_argument("--harvest-out", help="also write the raw candidate list to this txt")
    parser.add_argument("--vo-out", help="write harvested voice prefixes/names to this json")
    parser.add_argument("--vo-in", help="reuse voice prefixes/names from this json")
    parser.add_argument("--vo-import", help="folder/file of game-config json to pull voice paths (*.wem) from")
    parser.add_argument("--vo-online", choices=["GI", "SR", "ZZZ"], help="download & use online voice-name data (Dimbreath repos) for this game")
    parser.add_argument("--vo-online-refresh", action="store_true", help="force re-download of the online voice data (ignore the local cache)")
    parser.add_argument("--harvest-game", choices=["ZZZ", "GI", "SR"], help="decrypt .blk asset bundles for this game (Blocks folder) instead of raw scanning")
    args = parser.parse_args()
    if args.scan or args.harvest or args.vo_online:
        run_cli(args)
    else:
        run_gui()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
