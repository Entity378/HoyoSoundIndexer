import io
import json
import re
import tarfile
from concurrent.futures import ThreadPoolExecutor

from src.harvest import IDENTIFIER_RE
from src.hashing import fnv1_32
from src.jsondoc import table_rows, walk_scalars, walk_strings
from src.model import Kind
from src.roster import avatar_roster
from src.voice.paths import is_voice_file_path
from src.online.http import gitea_list_dir, gitea_raw, gitlab_archive, source_json, source_raw

_AUDIO_NAME_RE = re.compile(r"^[A-Za-z][\w]{2,}$")
# Integers above this in the GI audio tables are object ids, below it counts and flags.
_AUDIO_ID_MIN = 0xFFFF
_WWISE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,}$")
_TABLE_DOWNLOADS = 6


# GI lists every path in its voice archive, HSR in the VoicePath field of its voice tables.
def fetch_voice_paths(game, source, progress=None):
    if source.get("voice_subtree"):
        if progress:
            progress(0, 1, f"Downloading {game} voice archive...")
        return _voice_paths_from_archive(gitlab_archive(source, source["voice_subtree"]), progress)
    paths = []
    for relative in source.get("voice_files", []):
        if progress:
            progress(0, 1, f"Downloading {relative}...")
        paths.extend(_voice_paths_from_table(source_raw(source, relative)))
    return paths


def _json_members(archive):
    return [m for m in archive.getmembers() if m.isfile() and m.name.endswith(".json")]


def _voice_paths_from_archive(blob, progress=None):
    found = set()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = _json_members(archive)
        for i, member in enumerate(members):
            try:
                doc = json.loads(archive.extractfile(member).read().decode("utf-8", "ignore"))
            except Exception:
                continue
            found.update(text for text in walk_strings(doc) if is_voice_file_path(text))
            if progress and i % 500 == 0:
                progress(i, len(members), f"Reading voice data ({len(found)} paths)")
    return sorted(found)


def _voice_paths_from_table(data):
    doc = json.loads(data.decode("utf-8", "ignore"))
    items = doc if isinstance(doc, list) else list(doc.values())
    out = []
    for item in items:
        if isinstance(item, dict):
            voice_path = item.get("VoicePath")
            if isinstance(voice_path, str) and voice_path:
                out.append(voice_path)
    return out


# Id -> name pairs are the only handle on objects whose id is not a name hash, like GI's music segments.
def fetch_audio_labels(source, progress=None):
    if progress:
        progress(0, 1, "Downloading audio metadata...")
    subtree = source["audio_subtree"]
    blob = gitlab_archive(source, subtree)
    leaf = subtree.rstrip("/").split("/")[-1]
    names = set()
    id_names = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = _json_members(archive)
        for i, member in enumerate(members):
            parts = member.name.split("/")
            category = (parts[parts.index(leaf) + 1]
                        if leaf in parts and parts.index(leaf) + 1 < len(parts) else leaf)
            try:
                doc = json.loads(archive.extractfile(member).read().decode("utf-8", "ignore"))
            except Exception:
                continue
            _collect_audio_labels(doc, names, id_names, category)
            if progress and i % 100 == 0:
                progress(i, len(members),
                         f"Reading audio metadata ({len(names)} names, {len(id_names)} labels)")
    return sorted(names), id_names


# An object holding exactly one name and one id labels that id, in the category of its folder.
def _collect_audio_labels(node, names, id_names, category):
    if isinstance(node, dict):
        strings = [v for v in node.values() if isinstance(v, str) and _AUDIO_NAME_RE.match(v)]
        ids = [v for v in node.values() if isinstance(v, int) and v > _AUDIO_ID_MIN]
        names.update(strings)
        if len(strings) == 1 and len(ids) == 1:
            id_names[str(ids[0])] = [strings[0], category]
        for value in node.values():
            _collect_audio_labels(value, names, id_names, category)
    elif isinstance(node, list):
        for value in node:
            _collect_audio_labels(value, names, id_names, category)


def fetch_event_names(source, progress=None):
    pattern = _event_name_pattern(source.get("event_prefixes", "Ev,Play,Stop"))
    names = set()
    for relative in source.get("event_files", []):
        if progress:
            progress(0, 1, f"Downloading {relative}...")
        names.update(hit.group().decode("ascii", "ignore")
                      for hit in pattern.finditer(source_raw(source, relative)))
    for subtree in source.get("event_subtrees", []):
        if progress:
            progress(0, 1, f"Downloading {subtree}...")
        names.update(_names_in_archive(gitlab_archive(source, subtree), pattern))
    return names


# Case sensitive, unlike the client harvest, since the tables spell their events exactly.
def _event_name_pattern(prefixes):
    alternatives = b"|".join(re.escape(p.strip().encode()) for p in prefixes.split(",") if p.strip())
    return re.compile(rb"(?<![0-9A-Za-z_])(?:" + alternatives + rb")_[0-9A-Za-z_]{2,120}")


def _names_in_archive(blob, pattern):
    names = set()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            f = archive.extractfile(member)
            if f is None:
                continue
            names.update(hit.group().decode("ascii", "ignore") for hit in pattern.finditer(f.read()))
    return names


# MusicPlayerConfig ties each track to its Play_ event and to its title's text map key.
# The avatar rows ride along, since both need the 45 MB text map.
def fetch_zzz_music(source, progress=None):
    names, id_names = set(), {}
    if progress:
        progress(0, 1, "Downloading ZZZ music config...")
    tracks = []
    for row in table_rows(json.loads(gitea_raw(source, source["music_cfg"]).decode("utf-8", "ignore"))):
        event = text_key = None
        for value in row.values():
            if isinstance(value, str):
                if value.startswith("Play_"):
                    event = value
                elif value.startswith("TextMap_"):
                    text_key = value
        if event and text_key:
            tracks.append((event, text_key))
            names.add(event)
    rows = avatar_rows(source, progress)
    titles = {}
    if tracks or rows:
        if progress:
            progress(0, 1, "Downloading ZZZ text map (~45 MB)...")
        wanted = {text_key for _event, text_key in tracks}
        wanted.update(str(value) for row in rows for value in row)
        # The Overwrite table comes later and wins, since patches fix the titles there.
        titles = textmap_texts(source, (source["textmap"], source.get("textmap_overwrite")), wanted)
    for event, text_key in tracks:
        title = titles.get(text_key)
        if title:
            id_names[str(fnv1_32(event))] = [title, Kind.MUSIC]
    for relative in source.get("event_cfg_files", []):
        if progress:
            progress(0, 1, f"Downloading {relative}...")
        try:
            doc = json.loads(gitea_raw(source, relative).decode("utf-8", "ignore"))
        except Exception:
            continue
        names.update(value for value in walk_strings(doc) if "{" not in value and _WWISE_NAME_RE.match(value))
    return sorted(names), id_names, avatar_roster(rows, titles)


def fetch_avatar_names(source, progress=None):
    rows = avatar_rows(source, progress)
    if not rows:
        return {}
    if progress:
        progress(0, 1, "Downloading the text map of the avatar names...")
    wanted = {str(value) for row in rows for value in row}
    return avatar_roster(rows, textmap_texts(source, source.get("avatar_textmap") or [], wanted))


# Nothing is read by field name, and a table that is gone costs only its own rows.
def avatar_rows(source, progress=None):
    tables = source.get("avatar_cfg") or []
    rows = []
    for relative in [tables] if isinstance(tables, str) else tables:
        if progress:
            progress(0, 1, f"Downloading {relative}...")
        try:
            rows.extend(list(walk_scalars(row)) for row in table_rows(source_json(source, relative)))
        except Exception:
            continue
    return rows


# Only the wanted keys are kept from maps of 20 to 60 MB, and a later file wins.
def textmap_texts(source, files, wanted):
    texts = {}
    for relative in files:
        if not relative:
            continue
        textmap = source_json(source, relative)
        texts.update({key: value for key, value in textmap.items() if key in wanted and isinstance(value, str)})
    return texts


# Matching keeps only real hits, so the obfuscated field names that come along do no harm.
def fetch_state_candidates(source, progress=None):
    dirpath = source.get("cfg_scan_dir")
    if not dirpath:
        return []
    try:
        files = gitea_list_dir(source, dirpath)
    except Exception:
        return []
    seen = set()

    def download(name):
        try:
            return gitea_raw(source, f"{dirpath}/{name}")
        except Exception:
            return b""

    with ThreadPoolExecutor(max_workers=_TABLE_DOWNLOADS) as pool:
        for done, blob in enumerate(pool.map(download, files), 1):
            if blob:
                seen.update(hit.group() for hit in IDENTIFIER_RE.finditer(blob))
            if progress and done % 25 == 0:
                progress(done, len(files),
                         f"Downloading config tables ({done}/{len(files)}, {len(seen)} candidates)")
    out = []
    for raw in seen:
        try:
            out.append(raw.decode("ascii"))
        except UnicodeDecodeError:
            pass
    return sorted(out)
