# The bank chunks the scan reads: BKHD for the id, DIDX and DATA for embedded wems, HIRC for objects.

import struct
from dataclasses import dataclass, field

# A chunk claiming to run further past the bank than this is garbage and ends the walk.
_CHUNK_OVERRUN_SLACK = 16
_BKHD_HEAD_SIZE = 12
_DIDX_ENTRY_SIZE = 12


# bank_id is the id in the pck table, header_id the one in the bank's own BKHD chunk.
# embedded_wems holds (wem id, absolute offset, size).
@dataclass(slots=True)
class BankInfo:
    file_path: str = None
    bank_id: int = 0
    lang: str = "?"
    version: int = 0
    header_id: int = 0
    hirc_offset: int = 0
    hirc_size: int = 0
    embedded_wems: list = field(default_factory=list)


def read_bank_chunks(f, base_offset, size, bank):
    end = base_offset + size
    pos = base_offset
    index_entries = []
    data_start = None
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            break
        tag = head[:4]
        chunk_size = struct.unpack("<I", head[4:])[0]
        payload = pos + 8
        if payload + chunk_size > end + _CHUNK_OVERRUN_SLACK:
            break
        if tag == b"BKHD":
            header = f.read(min(_BKHD_HEAD_SIZE, chunk_size))
            if len(header) >= _BKHD_HEAD_SIZE:
                bank.version, bank.header_id, _lang = struct.unpack("<III", header)
        elif tag == b"DIDX":
            raw = f.read(chunk_size)
            for i in range(len(raw) // _DIDX_ENTRY_SIZE):
                index_entries.append(struct.unpack_from("<III", raw, i * _DIDX_ENTRY_SIZE))
        elif tag == b"DATA":
            data_start = payload
        elif tag == b"HIRC":
            bank.hirc_offset = payload
            bank.hirc_size = chunk_size
        pos = payload + chunk_size
    # DIDX offsets are relative to the DATA payload, which may come after it.
    if data_start is not None:
        for wem_id, offset, wem_size in index_entries:
            bank.embedded_wems.append((wem_id, data_start + offset, wem_size))
