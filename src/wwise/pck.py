# AKPK packages: a language table, then the tables of banks, streamed sounds and externals.
# Externals have 64-bit ids, the hashes of the voice file paths.

import struct
from typing import NamedTuple

# A package's own language table renames or adds to these ids.
DEFAULT_LANGUAGES = {0: "sfx", 1: "english", 2: "chinese", 3: "japanese", 4: "korean"}
_HEADER_SECTIONS = 0x10


# The offset is absolute, already multiplied by the block size.
class PckEntry(NamedTuple):
    file_id: int
    offset: int
    size: int
    lang_id: int


class PckTables(NamedTuple):
    languages: dict
    banks: list
    sounds: list
    externals: list

    def language(self, lang_id):
        return self.languages.get(lang_id, str(lang_id))


def _u32(f):
    return struct.unpack("<I", f.read(4))[0]


def _read_utf16z(f, offset):
    back = f.tell()
    f.seek(offset)
    text = ""
    while True:
        pair = f.read(2)
        if len(pair) < 2 or pair == b"\x00\x00":
            break
        try:
            text += pair.decode("utf-16-le")
        except Exception:
            break
    f.seek(back)
    return text


def _read_table(f, section_size, wide_ids):
    if section_size == 0:
        return []
    entries = []
    for _ in range(_u32(f)):
        file_id = struct.unpack("<Q", f.read(8))[0] if wide_ids else _u32(f)
        block_size, size, start_block, lang_id = struct.unpack("<IIII", f.read(16))
        offset = start_block * block_size if block_size else start_block
        entries.append(PckEntry(file_id, offset, size, lang_id))
    return entries


def parse_pck(path):
    languages = dict(DEFAULT_LANGUAGES)
    with open(path, "rb") as f:
        if f.read(4) != b"AKPK":
            raise ValueError("not AKPK")
        header_size = _u32(f)
        # The version field is not needed.
        f.read(4)
        lang_size = _u32(f)
        banks_size = _u32(f)
        sounds_size = _u32(f)
        # Older packages end the header after three tables, with no externals.
        has_externals = lang_size + banks_size + sounds_size + _HEADER_SECTIONS < header_size
        externals_size = _u32(f) if has_externals else 0

        # Language names are addressed relative to the start of the language table.
        strings_offset = f.tell()
        if lang_size > 0:
            definitions = []
            for _ in range(_u32(f)):
                offset, lang_id = struct.unpack("<II", f.read(8))
                definitions.append((lang_id, strings_offset + offset))
            for lang_id, offset in definitions:
                name = _read_utf16z(f, offset)
                if name:
                    languages[lang_id] = name.lower()
        f.seek(strings_offset + lang_size)

        banks = _read_table(f, banks_size, False)
        sounds = _read_table(f, sounds_size, False)
        externals = _read_table(f, externals_size, True) if externals_size > 0 else []
    return PckTables(languages, banks, sounds, externals)
