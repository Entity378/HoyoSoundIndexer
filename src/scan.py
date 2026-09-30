# Scans every pck and loose bnk of a folder, then reads the HIRC of every bank in two passes.
# The first pass collects the object ids to calibrate the layout, the second extracts the objects.

from pathlib import Path

from src.games import dedupe_pck_files, persistent_twins
from src.index import ScanIndex
from src.model import WemLocation
from src.wwise.bnk import BankInfo, read_bank_chunks
from src.wwise.hirc import (
    CONTAINER_TYPES, HIRC_ACTION, HIRC_DIALOGUE_EVENT, HIRC_EVENT, HIRC_MUSIC_SWITCH,
    HIRC_MUSIC_TRACK, HIRC_SOUND, HIRC_SWITCH, MUSIC_NODE_TYPES, NAMED_OBJECT_TYPES,
    calibrate_parent_variant, iter_hirc_objects, parse_action, parse_action_syncs,
    parse_dialogue_children, parse_dialogue_tree, parse_event_actions, parse_music_switch_tree,
    parse_music_track, parse_parent, parse_sound, parse_switch_container,
)
from src.wwise.pck import parse_pck

_CALIBRATION_SAMPLES = 4000
_PROGRESS_EVERY_BANKS = 20
_MUSIC_TRACK_FALLBACK_VARIANTS = ("bus7", "bus6")


# When cancel() turns true the scan stops and returns what it has so far.
def scan_folder(root, progress=None, cancel=None):
    root = Path(root)
    index = ScanIndex()
    roots = [root] + persistent_twins(root)
    if len(roots) > 1:
        index.stats["persistent_root"] = str(roots[1])
    pck_files, dropped = dedupe_pck_files({p for r in roots for p in r.rglob("*.pck")})
    if dropped:
        index.stats["shadowed_pck"] = dropped
    bnk_files = sorted({p for r in roots for p in r.rglob("*.bnk")})

    banks = _index_files(index, pck_files, bnk_files, progress, cancel)
    if banks is None:
        return index
    if not _read_hierarchy(index, banks, progress, cancel):
        return index
    _register_selector_syncs(index)

    index.stats["pck"] = len(pck_files)
    index.stats["bnk_inline"] = len(banks)
    index.stats["music_switch_trees"] = len(index.music_trees)
    index.stats["dialogue_trees"] = len(index.dialogue_trees)
    index.stats["switch_containers"] = len(index.switch_containers)
    index.stats["wem_ids"] = len(index.wem_locations)
    index.stats["externals"] = len(index.external_locations)
    index.stats["objects"] = len(index.object_ids)
    index.stats["game_syncs"] = len(index.sync_ids)
    index.stats["named_objects"] = len(index.named_objects)
    if progress:
        progress(1, 1, "Building reverse index...")
    index.build_wems_below()
    index.build_sync_links()
    return index


def _index_files(index, pck_files, bnk_files, progress, cancel):
    total = len(pck_files) + len(bnk_files)
    if progress:
        progress(0, total, "Indexing files...")
    banks = []
    done = 0
    for pck_path in pck_files:
        if cancel and cancel():
            return None
        done += 1
        if _index_pck(index, pck_path, banks) and progress:
            progress(done, total, pck_path.name)
    for bnk_path in bnk_files:
        if cancel and cancel():
            return None
        _index_loose_bank(index, bnk_path, banks)
        done += 1
        if progress:
            progress(done, total, bnk_path.name)
    return banks


def _index_pck(index, pck_path, banks):
    try:
        tables = parse_pck(pck_path)
    except Exception:
        return False
    path = str(pck_path)
    for entry in tables.sounds:
        index.wem_locations[entry.file_id].append(WemLocation(
            path, 0, tables.language(entry.lang_id), entry.offset, entry.size, "pck"))
    for entry in tables.externals:
        index.external_locations[entry.file_id].append(WemLocation(
            path, 0, tables.language(entry.lang_id), entry.offset, entry.size, "pck"))
    with open(pck_path, "rb") as f:
        for entry in tables.banks:
            index.bank_ids.add(entry.file_id)
            bank = BankInfo(file_path=path, bank_id=entry.file_id, lang=tables.language(entry.lang_id))
            try:
                read_bank_chunks(f, entry.offset, entry.size, bank)
            except Exception:
                continue
            banks.append(bank)
            for wem_id, offset, size in bank.embedded_wems:
                index.wem_locations[wem_id].append(WemLocation(
                    path, entry.file_id, bank.lang, offset, size, "bnk"))
    return True


# A standalone bank only has the id in its own header.
def _index_loose_bank(index, bnk_path, banks):
    try:
        size = bnk_path.stat().st_size
        bank = BankInfo(file_path=str(bnk_path), lang="sfx")
        with open(bnk_path, "rb") as f:
            read_bank_chunks(f, 0, size, bank)
        bank.bank_id = bank.header_id
        index.bank_ids.add(bank.header_id)
        banks.append(bank)
        for wem_id, offset, wem_size in bank.embedded_wems:
            index.wem_locations[wem_id].append(WemLocation(
                bank.file_path, bank.bank_id, bank.lang, offset, wem_size, "bnk"))
    except Exception:
        pass


# The chunks read by the first pass are kept for the second, so every bank is read once.
def _read_hierarchy(index, banks, progress, cancel):
    banks = [bank for bank in banks if bank.hirc_size]
    loaded = []
    container_samples, music_samples = [], []
    if progress:
        progress(0, len(banks) or 1, "Parsing HIRC (1/2)...")
    for i, bank in enumerate(banks):
        if cancel and cancel():
            return False
        try:
            with open(bank.file_path, "rb") as f:
                f.seek(bank.hirc_offset)
                raw = f.read(bank.hirc_size)
        except Exception:
            continue
        loaded.append((bank, raw))
        _collect_object_ids(index, bank, raw, container_samples, music_samples)
        if progress and i % _PROGRESS_EVERY_BANKS == 0:
            progress(i + 1, len(banks), "Parsing HIRC (1/2)...")

    container_variant, container_score = calibrate_parent_variant(
        container_samples, index.object_ids, "container")
    music_variant, music_score = calibrate_parent_variant(music_samples, index.object_ids, "music")
    index.stats["cont_variant"] = f"{container_variant} ({container_score:.0%})"
    index.stats["music_variant"] = f"{music_variant} ({music_score:.0%})"

    reader = _HircReader(index, container_variant, music_variant)
    if progress:
        progress(0, len(loaded) or 1, "Parsing HIRC (2/2)...")
    for i, (bank, raw) in enumerate(loaded):
        if cancel and cancel():
            return False
        reader.read_bank(bank, raw)
        if progress and i % _PROGRESS_EVERY_BANKS == 0:
            progress(i + 1, len(loaded), "Parsing HIRC (2/2)...")
    return True


def _collect_object_ids(index, bank, raw, container_samples, music_samples):
    has_objects = False
    for otype, oid, p, end in iter_hirc_objects(raw):
        has_objects = True
        index.object_ids.add(oid)
        if otype in CONTAINER_TYPES and len(container_samples) < _CALIBRATION_SAMPLES:
            container_samples.append((raw, p, end))
        elif otype in MUSIC_NODE_TYPES and len(music_samples) < _CALIBRATION_SAMPLES:
            music_samples.append((raw, p, end))
    if has_objects:
        index.bank_ids.add(bank.header_id)


# A handler that trips on a malformed object costs only that object, counted in parse_errors.
class _HircReader:
    def __init__(self, index, container_variant, music_variant):
        self.index = index
        self.container_variant = container_variant
        self.music_variant = music_variant
        self.handlers = {
            HIRC_EVENT: self._event,
            HIRC_ACTION: self._action,
            HIRC_SOUND: self._sound,
            HIRC_MUSIC_TRACK: self._music_track,
            HIRC_DIALOGUE_EVENT: self._dialogue_event,
        }
        self.handlers.update({otype: self._container for otype in CONTAINER_TYPES})
        self.handlers.update({otype: self._music_node for otype in MUSIC_NODE_TYPES})

    def read_bank(self, bank, raw):
        index = self.index
        for otype, oid, p, end in iter_hirc_objects(raw):
            named = NAMED_OBJECT_TYPES.get(otype)
            if named:
                index.named_objects.setdefault(oid, named)
            handler = self.handlers.get(otype)
            if handler is None:
                continue
            try:
                handler(bank, otype, oid, raw, p, end)
            except Exception:
                index.stats["parse_errors"] += 1

    def _add_parent(self, oid, parent):
        if parent and parent in self.index.object_ids:
            self.index.parents[oid].add(parent)

    def _event(self, bank, _otype, oid, raw, p, end):
        index = self.index
        actions = parse_event_actions(raw, p, end)
        if actions:
            index.event_actions.setdefault(oid, actions)
        if bank.header_id:
            index.event_banks[oid].add(bank.header_id)
        index.stats["events"] += 1

    def _action(self, _bank, _otype, oid, raw, p, end):
        index = self.index
        action = parse_action(raw, p, end)
        if not action:
            return
        index.actions.setdefault(oid, action)
        touched = []
        for sync_id, kind in parse_action_syncs(raw, p, end, action):
            if sync_id:
                index.sync_ids.setdefault(sync_id, kind)
                touched.append(sync_id)
        if touched:
            index.action_syncs.setdefault(oid, touched)

    def _sound(self, _bank, _otype, oid, raw, p, end):
        parsed = parse_sound(raw, p, end)
        if not parsed:
            return
        source_id, params_offset = parsed
        if source_id:
            self.index.node_sources[oid].add(source_id)
        if params_offset is not None:
            self._add_parent(oid, parse_parent(raw, params_offset, end, self.container_variant))

    # Tracks mostly use the flat layout, and a parent that lands nowhere is retried with the others.
    def _music_track(self, _bank, _otype, oid, raw, p, end):
        object_ids = self.index.object_ids
        parsed = parse_music_track(raw, p, end, "flat2")
        if parsed and not (parsed[1] and parsed[1] in object_ids):
            for variant in _MUSIC_TRACK_FALLBACK_VARIANTS:
                retry = parse_music_track(raw, p, end, variant)
                if retry and retry[1] and retry[1] in object_ids:
                    parsed = retry
                    break
        if not parsed:
            return
        sources, parent = parsed
        if sources:
            self.index.node_sources[oid] |= sources
        self._add_parent(oid, parent)

    def _container(self, bank, otype, oid, raw, p, end):
        index = self.index
        if bank.header_id:
            index.object_banks[oid].add(bank.header_id)
        self._add_parent(oid, parse_parent(raw, p, end, self.container_variant))
        if otype == HIRC_SWITCH:
            container = parse_switch_container(raw, p, end, index.object_ids)
            # The same container recurs across banks, and the copy with most assignments wins.
            known = index.switch_containers.get(oid)
            if container and (known is None or len(container.assignments) > len(known.assignments)):
                index.switch_containers[oid] = container

    def _music_node(self, _bank, otype, oid, raw, p, end):
        index = self.index
        # Music nodes start with a flags byte before their NodeBaseParams.
        self._add_parent(oid, parse_parent(raw, p + 1, end, self.music_variant))
        if otype == HIRC_MUSIC_SWITCH:
            tree = parse_music_switch_tree(raw, p, end, index.object_ids)
            # The same switch recurs across banks, and the fullest copy (Patch.pck) wins.
            known = index.music_trees.get(oid)
            if tree and (known is None or len(tree.leaves) > len(known.leaves)):
                index.music_trees[oid] = tree

    def _dialogue_event(self, bank, _otype, oid, raw, p, end):
        index = self.index
        children = [child for child in parse_dialogue_children(raw, p, end)
                    if child and child in index.object_ids]
        if children:
            index.dialogue_children[oid] |= set(children)
        tree = parse_dialogue_tree(raw, p, end)
        known = index.dialogue_trees.get(oid)
        if tree and (known is None or len(tree.leaves) > len(known.leaves)):
            index.dialogue_trees[oid] = tree
        if bank.header_id:
            index.event_banks[oid].add(bank.header_id)
        index.stats["dialogue_events"] += 1


def _group_kind(group_type):
    return "Switch group" if group_type == 0 else "State group"


def _value_kind(group_type):
    return "Switch" if group_type == 0 else "State"


# Every selector's group and value ids become matchable syncs, so state names attach to them.
def _register_selector_syncs(index):
    sync_ids = index.sync_ids
    for trees in (index.music_trees, index.dialogue_trees):
        for tree in trees.values():
            for group_id, group_type in zip(tree.groups, tree.group_types):
                if group_id:
                    sync_ids.setdefault(group_id, _group_kind(group_type))
            for keys, _target in tree.leaves:
                for group_type, key in zip(tree.group_types, keys):
                    if key:
                        sync_ids.setdefault(key, _value_kind(group_type))
    for container in index.switch_containers.values():
        sync_ids.setdefault(container.group_id, _group_kind(container.group_type))
        for value_id in container.assignments:
            if value_id:
                sync_ids.setdefault(value_id, _value_kind(container.group_type))
