# Blb3 container (current Genshin), from AnimeStudio (GPL-3.0).
# Header 'Blb' | size | skip | Header[16] | encrypted blockInfo: bespoke AES + RC4 + GF256, then Oodle/LZ4/LZMA.

import lzma
import struct

from ._keys import (
    GF256Exp, GF256Log,
    Blb3RC4Key, Blb3SBox, Blb3ShiftRow, Blb3Key, Blb3Mul, Blb3AESSBox, Blb3AESShift,
)
from .mhy import _gmul, lz4_decompress, oodle_decompress

_POWER = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
_SHIFT_ROWS = [0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11]


def _blb_expand(key):
    keys = bytearray(176)
    for i in range(16):
        keys[i] = key[Blb3AESShift[i]]
    offset = 0x1F
    for rnd in range(10):
        a = Blb3AESSBox[keys[offset - 0x14]]
        b = Blb3AESSBox[keys[offset - 0x10]]
        c = (Blb3AESSBox[keys[offset - 0x18]] ^ keys[offset - 0x18] ^ _POWER[rnd] ^ keys[offset - 0x1F]) & 0xFF
        d = Blb3AESSBox[keys[offset - 0x1C]]
        keys[offset - 0xF] = c
        temp = (a ^ keys[offset - 0x14] ^ keys[offset - 0x1B]) & 0xFF
        keys[offset - 0xB] = temp
        a = (b ^ keys[offset - 0x10] ^ keys[offset - 0x17]) & 0xFF
        keys[offset - 7] = a
        b = (d ^ keys[offset - 0x1C] ^ keys[offset - 0x13]) & 0xFF
        keys[offset - 3] = b
        c = (c ^ keys[offset - 0x1E]) & 0xFF
        keys[offset - 0xE] = c
        temp = (temp ^ keys[offset - 0x1A]) & 0xFF
        keys[offset - 10] = temp
        a = (a ^ keys[offset - 0x16]) & 0xFF
        keys[offset - 6] = a
        b = (b ^ keys[offset - 0x12]) & 0xFF
        keys[offset - 2] = b
        c = (c ^ keys[offset - 0x1D]) & 0xFF
        keys[offset - 0xD] = c
        temp = (temp ^ keys[offset - 0x19]) & 0xFF
        keys[offset - 9] = temp
        a = (a ^ keys[offset - 0x15]) & 0xFF
        keys[offset - 5] = a
        b = (b ^ keys[offset - 0x11]) & 0xFF
        keys[offset - 1] = b
        keys[offset - 0xC] = (c ^ keys[offset - 0x1C]) & 0xFF
        keys[offset - 8] = (temp ^ keys[offset - 0x18]) & 0xFF
        keys[offset - 4] = (a ^ keys[offset - 0x14]) & 0xFF
        keys[offset] = (b ^ keys[offset - 0x10]) & 0xFF
        offset += 0x10
    return keys


def _blb_encrypt(block16, key16):
    keys = _blb_expand(key16)
    c = bytearray(block16[:16])
    _xor_round_key(c, keys, 0)
    for rnd in range(9):
        for i in range(16):
            c[i] ^= Blb3AESSBox[c[i]]
        _shift_rows(c)
        _mix_cols(c)
        _xor_round_key(c, keys, rnd + 1)
    for i in range(16):
        c[i] ^= Blb3AESSBox[c[i]]
    _shift_rows(c)
    _xor_round_key(c, keys, 10)
    return bytes(c)


def _xor_round_key(state, keys, rnd):
    base = rnd * 16
    for i in range(4):
        for j in range(4):
            state[i * 4 + j] ^= keys[i + j * 4 + base]


def _shift_rows(state):
    tmp = bytes(state)
    for i in range(16):
        state[i] = tmp[_SHIFT_ROWS[i]]


def _mix_cols(state):
    for off in range(0, 16, 4):
        a0, a1, a2, a3 = state[off], state[off + 1], state[off + 2], state[off + 3]
        state[off + 0] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        state[off + 1] = _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3 ^ a0
        state[off + 2] = _gmul(a2, 2) ^ _gmul(a3, 3) ^ a0 ^ a1
        state[off + 3] = _gmul(a3, 2) ^ _gmul(a0, 3) ^ a1 ^ a2


def _gf256_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF256Exp[(GF256Log[a] + GF256Log[b]) % 0xFF]


def _blb_descramble(buf, start, length):
    for i in range(3):
        vector = bytearray(length)
        for j in range(length):
            k = Blb3ShiftRow[(2 - i) * 0x10 + j]
            idx = j % 8
            vector[j] = Blb3Key[idx] ^ Blb3SBox[(j % 4 * 0x100) | _gf256_mul(Blb3Mul[idx], buf[start + (k % length)])]
        buf[start:start + length] = vector


def _blb_rc4(buf):
    S = bytearray(Blb3RC4Key)
    T = bytearray(256)
    for i in range(0, 256, 2):
        T[i] = buf[i & 6]
        T[i + 1] = buf[(i + 1) & 7]
    j = 0
    for i in range(256):
        j = (j + S[i] + T[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
    i = j = 0
    n = len(buf) - 0x10
    for it in range(n):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        K = S[(S[j] + S[i]) & 0xFF]
        op = buf[(i % 8) + 8] % 3
        idx = it + 0x10
        if op == 0:
            buf[idx] ^= K
        elif op == 1:
            buf[idx] = (buf[idx] - K) & 0xFF
        else:
            buf[idx] = (buf[idx] + K) & 0xFF


def _blb_decrypt(header16, buffer):
    length = min(128, len(buffer))
    for i in range(min(length, 16)):
        buffer[i] ^= header16[i]
    if length >= 16:
        enc = _blb_encrypt(bytes(buffer[:16]), header16)
        buffer[:16] = enc
        if length > 16:
            _blb_rc4_region(buffer, length)
        _blb_descramble(buffer, 0, 16)


def _blb_rc4_region(buffer, length):
    # RC4 modifies bytes [0x10:length]; it keys off buffer[:16]. Operate on the sub-view.
    view = bytearray(buffer[:length])
    _blb_rc4(view)
    buffer[:length] = view


_COMP_NONE, _COMP_LZMA, _COMP_LZ4, _COMP_LZ4HC, _COMP_OODLE = 0, 1, 2, 3, 9


def _lzma_decompress(block, out_size):
    props = block[0]
    dict_size = struct.unpack_from("<I", block, 1)[0]
    lc = props % 9
    rem = props // 9
    lp = rem % 5
    pb = rem // 5
    dec = lzma.LZMADecompressor(
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}])
    return dec.decompress(block[5:], out_size)


_BLB_SIG = b"Blb" + bytes([3])


def _iter_one_blb(data, base):
    if bytes(data[base:base + 4]) != _BLB_SIG:
        raise ValueError("not a Blb3 file")
    size = struct.unpack_from("<I", data, base + 4)[0]
    header16 = bytes(data[base + 12:base + 28])
    block_info = bytearray(data[base + 28:base + 28 + size])
    _blb_decrypt(header16, block_info)

    p = 0

    def u32():
        nonlocal p
        v = struct.unpack_from("<I", block_info, p)[0]
        p += 4
        return v

    def i32():
        nonlocal p
        v = struct.unpack_from("<i", block_info, p)[0]
        p += 4
        return v

    def i64():
        nonlocal p
        v = struct.unpack_from("<q", block_info, p)[0]
        p += 8
        return v

    # m_Header.size
    u32()
    last_uncompressed = u32()
    # skip
    p += 4
    # blobOffset
    i32()
    # blobSize
    u32()
    compression_type = block_info[p]
    p += 1
    default_uncompressed = 1 << block_info[p]
    p += 1
    # align to 4
    p = (p + 3) & ~3

    blocks_count = i32()
    # nodesCount, not needed for name harvesting
    i32()
    blocks_info_offset = p + i64()

    p = blocks_info_offset
    comp_sizes = [u32() for _ in range(blocks_count)]

    # Sizes are cumulative: diff them to get the per-block ones.
    blocks = []
    prev = 0
    for idx, cs in enumerate(comp_sizes):
        usize = last_uncompressed if idx == blocks_count - 1 else default_uncompressed
        if idx == 0:
            csize, flag = cs, compression_type
        else:
            csize = cs - prev
            flag = _COMP_NONE if csize == usize else compression_type
        blocks.append((csize, usize, flag))
        prev = cs

    data_pos = base + 28 + size
    out = []
    for csize, usize, flag in blocks:
        block = bytearray(data[data_pos:data_pos + csize])
        data_pos += csize
        if flag == _COMP_NONE:
            _blb_decrypt(header16, block)
            out.append(bytes(block[:usize]))
        elif flag == _COMP_OODLE:
            if csize > 6:
                _blb_decrypt(header16, block)
            out.append(oodle_decompress(bytes(block), usize))
        elif flag in (_COMP_LZ4, _COMP_LZ4HC):
            _blb_decrypt(header16, block)
            out.append(lz4_decompress(bytes(block), usize))
        elif flag == _COMP_LZMA:
            out.append(_lzma_decompress(bytes(block), usize))
        else:
            raise ValueError("unsupported Blb compression %d" % flag)
    return out, data_pos


# Yields the blocks of every Blb3 bundle concatenated in the file.
# Stopping at the first one lost over 90% of the content.
def iter_blb_payload(data):
    total = len(data)
    base = 0
    while base + 28 <= total and bytes(data[base:base + 4]) == _BLB_SIG:
        blocks, end = _iter_one_blb(data, base)
        for block in blocks:
            yield block
        # invalid offset: stop
        if end <= base:
            break
        base = end
        # alignment between bundles
        while base < total and data[base] == 0:
            base += 1
