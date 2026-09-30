# Names files in, exports out; an export reloads one to one, so a resolve is never cracked twice.

import json
from pathlib import Path

from src.config import APP_NAME
from src.model import Kind, NameMatch
from src.wwise.hirc import ACTION_TYPE_NAMES

# The export sections, with the kind of their rows (None when each row names its own).
EXPORT_SECTIONS = (("events", None), ("banks", Kind.BANK), ("direct_wems", Kind.DIRECT_WEM),
                   ("externals", Kind.EXTERNAL), ("game_syncs", None))


def load_names(path):
    return read_names_file(path)[0]


# For an export only the unmatched names come back: its rows are restored by id, not re-hashed.
# Re-hashing 326k voice paths at 32 bits would only add false collisions.
def read_names_file(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    doc = None
    if path.suffix.lower() == ".json":
        try:
            doc = json.loads(text)
        except Exception:
            doc = None
    export = ExportedNames(doc) if isinstance(doc, dict) else None
    if isinstance(doc, dict) and _is_export(doc):
        return [name for name in doc.get("unmatched_names", []) if isinstance(name, str)], export
    if isinstance(doc, list):
        return [name for name in doc if isinstance(name, str) and name], None
    names = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names, export


def _is_export(doc):
    return any(isinstance(doc.get(section), list) for section, _kind in EXPORT_SECTIONS)


class ExportedNames:
    def __init__(self, doc):
        self.doc = doc

    @classmethod
    def read(cls, path):
        if not path or not str(path).lower().endswith(".json"):
            return None
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None
        return cls(doc) if isinstance(doc, dict) else None

    # (name, kind, id) of every saved row, in file order.
    def entries(self):
        for section, fixed_kind in EXPORT_SECTIONS:
            for entry in self.doc.get(section, []):
                if isinstance(entry, dict):
                    kind = fixed_kind or entry.get("type") or entry.get("kind")
                    yield entry.get("name"), kind, entry.get("id")

    # Rows are rebuilt from their saved ids, since labels like GI's music segments are not name hashes.
    # Music branches are rebuilt from the restored state names instead, with every wem of the branch.
    def matches(self, index):
        restored = []
        for name, kind, oid in self.entries():
            if not name or not kind or not isinstance(oid, int) or kind == Kind.MUSIC_BRANCH:
                continue
            wems = _restored_wems(index, kind, oid)
            if wems is not None:
                restored.append(NameMatch(name, kind, wems, oid))
        return restored

    def avatar_names(self):
        roster = self.doc.get("avatar_names")
        if not isinstance(roster, dict):
            return {}
        return {key: value for key, value in roster.items() if isinstance(value, str)}


# None when the index does not know the id, so an older export restores what still exists.
def _restored_wems(index, kind, oid):
    if kind == Kind.BANK:
        return [] if oid in index.bank_ids else None
    if kind == Kind.DIRECT_WEM:
        return [oid] if oid in index.wem_locations else None
    if kind == Kind.EXTERNAL:
        return [oid] if oid in index.external_locations else None
    wems = index.wems_for_object(oid)
    if (oid not in index.object_ids and oid not in index.event_actions
            and oid not in index.sync_ids and oid not in index.named_objects):
        return None
    return wems


def export_txt(matches, out_path):
    seen = set()
    lines = []
    for m in matches:
        if m.kind in Kind.EVENTS and m.name not in seen:
            seen.add(m.name)
            lines.append(m.name)
    Path(out_path).write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    return len(lines)


def export_json(matches, unmatched, index, scan_root, names_file, out_path):
    events, banks, direct_wems, externals, game_syncs = [], [], [], [], []
    used_wems = set()
    for m in sorted(matches, key=lambda match: match.name.lower()):
        if m.kind in Kind.EVENTS:
            events.append({
                "name": m.name,
                "id": m.hash_id,
                "type": m.kind,
                "banks": sorted(index.event_banks.get(m.hash_id, ())),
                "action_types": _action_type_names(index, m.hash_id),
                "wems": m.wem_ids,
            })
            used_wems.update(m.wem_ids)
        elif m.kind == Kind.BANK:
            banks.append({"name": m.name, "id": m.hash_id})
        elif m.kind == Kind.DIRECT_WEM:
            direct_wems.append({"name": m.name, "id": m.hash_id})
            used_wems.add(m.hash_id)
        elif m.kind == Kind.EXTERNAL:
            externals.append({"name": m.name, "id": m.hash_id})
            used_wems.add(m.hash_id)
        else:
            game_syncs.append({"name": m.name, "id": m.hash_id, "kind": m.kind})
    doc = {
        "tool": APP_NAME,
        "scan_folder": str(scan_root),
        "names_file": str(names_file),
        "stats": dict(sorted(index.stats.items())),
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
        "avatar_names": dict(sorted(index.avatar_names.items())),
        "wems": {str(wem_id): _wem_entry(index, wem_id, scan_root) for wem_id in sorted(used_wems)},
        "unmatched_names": unmatched,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    return len(events)


def _action_type_names(index, event_id):
    names = set()
    for action_id in index.event_actions.get(event_id, ()):
        action = index.actions.get(action_id)
        if action:
            names.add(ACTION_TYPE_NAMES.get(action.type, f"type_{action.type}"))
    return sorted(names)


def _relative_path(path, root):
    try:
        return str(Path(path).relative_to(root))
    except Exception:
        return str(path)


def _wem_entry(index, wem_id, scan_root):
    locations = index.locations_of(wem_id)
    return {
        "languages": sorted({location.lang for location in locations}),
        "sources": [{
            "file": _relative_path(location.file_path, scan_root),
            "kind": location.kind,
            "bnk": location.bnk_id or None,
            "offset": location.offset,
            "size": location.size,
        } for location in locations],
    }
