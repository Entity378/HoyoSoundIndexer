# Star Rail's ENCR container (the .block files in StreamingAssets/Asb), from AnimeStudio (GPL-3.0).
# A chain of UnityFS without version strings: LZ4 blocksInfo, blocks with flag 5 = Lz4Mr0k and 7 = OodleMr0k.

import struct

from .mhy import _mr0k_decrypt, lz4_decompress, oodle_decompress
from ._keys import Mr0kExpansionKey, Mr0kInitVector, Mr0kBlockKey

_ENCR_SIG = b"ENCR"
_SR_MR0K = {"expansion_key": Mr0kExpansionKey, "init_vector": Mr0kInitVector,
            "block_key": Mr0kBlockKey}

# Compression flags (i & 0x3f): mihoyo values on top of the standard LZ4 ones.
_C_NONE, _C_LZ4, _C_LZ4HC, _C_LZ4MR0K, _C_OODLE_HSR, _C_OODLEMR0K, _C_OODLE = 0, 2, 3, 5, 6, 7, 9


def _decompress(block, out_size, kind):
    if kind in (_C_LZ4MR0K, _C_OODLEMR0K) and block[:4] == b"mr0k":
        block = _mr0k_decrypt(bytes(block), **_SR_MR0K)
    if kind in (_C_LZ4, _C_LZ4HC, _C_LZ4MR0K):
        return lz4_decompress(bytes(block), out_size)
    if kind in (_C_OODLE_HSR, _C_OODLEMR0K, _C_OODLE):
        return oodle_decompress(bytes(block), out_size)
    if kind == _C_NONE:
        return bytes(block[:out_size])
    raise ValueError("compressione ENCR non supportata: %d" % kind)


def _iter_one_encr(data, base):
    # after the ENCR magic; no version/revision
    pos = data.index(b"\x00", base) + 1
    size = struct.unpack_from(">q", data, pos)[0]
    comp_info = struct.unpack_from(">I", data, pos + 8)[0]
    uncomp_info = struct.unpack_from(">I", data, pos + 12)[0]
    flags = struct.unpack_from(">I", data, pos + 16)[0]
    # SR: no 16-byte alignment, no extra bytes
    pos += 20
    info_kind = flags & 0x3f

    # blocksInfo at the end of the bundle
    if flags & 0x80:
        info_off = base + size - comp_info
        data_pos = pos
    else:
        info_off = pos
        data_pos = pos + comp_info
    blocks_info = data[info_off:info_off + comp_info]
    info = _decompress(blocks_info, uncomp_info, info_kind)

    # SR ENCR: no 16-byte hash
    p = 0
    count = struct.unpack_from(">i", info, p)[0]
    p += 4
    blocks = [struct.unpack_from(">IIH", info, p + i * 10) for i in range(count)]

    out = []
    for uncomp, comp, blk_flags in blocks:
        block = data[data_pos:data_pos + comp]
        data_pos += comp
        out.append(_decompress(block, uncomp, blk_flags & 0x3f))
    end = base + size if size > 0 else data_pos
    return out, max(end, data_pos)


def iter_encr_payload(data):
    total = len(data)
    base = 0
    while base + 8 <= total and data[base:base + 4] == _ENCR_SIG:
        blocks, end = _iter_one_encr(data, base)
        for block in blocks:
            yield block
        if end <= base:
            break
        base = end
        # skip any padding
        while base < total and data[base:base + 4] != _ENCR_SIG:
            base += 1
