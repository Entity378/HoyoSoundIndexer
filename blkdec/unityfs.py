# Standard UnityFS parser: needed by Star Rail after mr0k and by unencrypted bundles.

import lzma
import struct

from .mhy import lz4_decompress, oodle_decompress

_UNITYFS_SIG = b"UnityFS"

# Block compression types (high values are mihoyo variants).
_C_NONE, _C_LZMA, _C_LZ4, _C_LZ4HC = 0, 1, 2, 3
_C_OODLE_HSR, _C_OODLE = 6, 9


def _lzma_decompress(block, out_size):
    props = block[0]
    dict_size = struct.unpack_from("<I", block, 1)[0]
    lc = props % 9
    rem = props // 9
    dec = lzma.LZMADecompressor(
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                  "lc": lc, "lp": rem % 5, "pb": rem // 5}])
    return dec.decompress(block[5:], out_size)


def _decompress(block, out_size, kind):
    if kind == _C_NONE:
        return bytes(block[:out_size])
    if kind in (_C_LZ4, _C_LZ4HC):
        return lz4_decompress(bytes(block), out_size)
    if kind == _C_LZMA:
        return _lzma_decompress(bytes(block), out_size)
    if kind in (_C_OODLE, _C_OODLE_HSR):
        return oodle_decompress(bytes(block), out_size)
    raise ValueError("compressione UnityFS non supportata: %d" % kind)


def _read_cstring(data, pos):
    end = data.index(b"\x00", pos)
    return data[pos:end], end + 1


def iter_one_unityfs(data, base):
    if not data[base:base + 7] == _UNITYFS_SIG:
        raise ValueError("not a UnityFS bundle")
    pos = data.index(b"\x00", base) + 1
    version = struct.unpack_from(">I", data, pos)[0]
    pos += 4
    # unity version
    _, pos = _read_cstring(data, pos)
    # unity revision
    _, pos = _read_cstring(data, pos)
    size, comp_size, uncomp_size, flags = struct.unpack_from(">qIII", data, pos)
    pos += 20
    # align to 16 bytes
    if version >= 7:
        pos = (pos + 15) & ~15

    # blocksInfo at the end of the file
    if flags & 0x80:
        info_pos = base + size - comp_size
        data_pos = pos
    else:
        info_pos = pos
        data_pos = pos + comp_size

    info = _decompress(data[info_pos:info_pos + comp_size], uncomp_size, flags & 0x3F)

    # skip the hash
    p = 16
    blocks_count = struct.unpack_from(">i", info, p)[0]
    p += 4
    blocks = []
    for _ in range(blocks_count):
        u, c, f = struct.unpack_from(">IIH", info, p)
        p += 10
        blocks.append((c, u, f & 0x3F))

    out = []
    for comp, uncomp, kind in blocks:
        out.append(_decompress(data[data_pos:data_pos + comp], uncomp, kind))
        data_pos += comp
    end = base + size if size > 0 else data_pos
    return out, max(end, data_pos)


def iter_unityfs_payload(data, base=0):
    total = len(data)
    while base + 8 <= total and data[base:base + 7] == _UNITYFS_SIG:
        blocks, end = iter_one_unityfs(data, base)
        for block in blocks:
            yield block
        if end <= base:
            break
        base = end
        # alignment between bundles
        while base < total and data[base] == 0:
            base += 1
