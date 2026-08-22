# Container modules import .mhy at the top, so importing them back here can only be done inside the functions.
# Core .blk, from AnimeStudio (Escartem, GPL-3.0): mhy0/mhy1 descramble (GF256 + mhynewec/AES/RC4), then LZ4/Oodle.
# mr0k (SR) decrypts the UnityFS, then standard blocks; only the decompressed payload is returned.

import ctypes
import os
import struct
import sys
from pathlib import Path

from ._keys import (
    GF256Exp, GF256Log, GISBox, GIMhyShiftRow, GIMhyKey, GIMhyMul,
    GIExpansionKey, GIInitVector, GI_INIT_SEED,
    Mr0kExpansionKey, Mr0kInitVector, Mr0kBlockKey, ToTKey, RC4_KEY,
)


_AES_SBOX = []
_AES_INV_SBOX = [0] * 256


def _init_aes_tables():
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        q ^= 0x09 if q & 0x80 else 0
        q &= 0xFF
        xf = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (xf ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    global _AES_SBOX
    _AES_SBOX = sbox
    for i, v in enumerate(sbox):
        _AES_INV_SBOX[v] = i


_init_aes_tables()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _aes128_key_schedule(key):
    rk = list(key[:16])
    for i in range(4, 44):
        t = rk[(i - 1) * 4:(i - 1) * 4 + 4]
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_AES_SBOX[x] for x in t]
            t[0] ^= _RCON[i // 4 - 1]
        prev = rk[(i - 4) * 4:(i - 4) * 4 + 4]
        rk += [prev[j] ^ t[j] for j in range(4)]
    return rk


def aes128_ecb_encrypt_block(key, block):
    rk = _aes128_key_schedule(key)
    s = list(block[:16])
    for i in range(16):
        s[i] ^= rk[i]
    for rnd in range(1, 10):
        s = [_AES_SBOX[x] for x in s]
        s = _shift_rows(s)
        s = _mix_columns(s)
        base = rnd * 16
        for i in range(16):
            s[i] ^= rk[base + i]
    s = [_AES_SBOX[x] for x in s]
    s = _shift_rows(s)
    for i in range(16):
        s[i] ^= rk[160 + i]
    return bytes(s)


def _shift_rows(s):
    return [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]


def _mix_columns(s):
    out = [0] * 16
    for c in range(4):
        col = s[c * 4:c * 4 + 4]
        out[c * 4 + 0] = _gmul(col[0], 2) ^ _gmul(col[1], 3) ^ col[2] ^ col[3]
        out[c * 4 + 1] = col[0] ^ _gmul(col[1], 2) ^ _gmul(col[2], 3) ^ col[3]
        out[c * 4 + 2] = col[0] ^ col[1] ^ _gmul(col[2], 2) ^ _gmul(col[3], 3)
        out[c * 4 + 3] = _gmul(col[0], 3) ^ col[1] ^ col[2] ^ _gmul(col[3], 2)
    return out


# AES.Decrypt from AnimeStudio: inverse rounds with a pre-expanded 176-byte key.
_SHIFT_ROWS_INV = [0x00, 0x0D, 0x0A, 0x07, 0x04, 0x01, 0x0E, 0x0B,
                   0x08, 0x05, 0x02, 0x0F, 0x0C, 0x09, 0x06, 0x03]


def aes_decrypt_expanded(block16, expansion_key):
    c = bytearray(block16[:16])
    for i in range(16):
        c[i] ^= expansion_key[i]
    for rnd in range(9):
        c = bytearray(_AES_INV_SBOX[x] for x in c)
        tmp = bytes(c)
        for i in range(16):
            c[i] = tmp[_SHIFT_ROWS_INV[i]]
        for off in range(0, 16, 4):
            a0, a1, a2, a3 = c[off], c[off + 1], c[off + 2], c[off + 3]
            c[off + 0] = _gmul(a0, 14) ^ _gmul(a3, 9) ^ _gmul(a2, 13) ^ _gmul(a1, 11)
            c[off + 1] = _gmul(a1, 14) ^ _gmul(a0, 9) ^ _gmul(a3, 13) ^ _gmul(a2, 11)
            c[off + 2] = _gmul(a2, 14) ^ _gmul(a1, 9) ^ _gmul(a0, 13) ^ _gmul(a3, 11)
            c[off + 3] = _gmul(a3, 14) ^ _gmul(a2, 9) ^ _gmul(a1, 13) ^ _gmul(a0, 11)
        base = (rnd + 1) * 16
        for i in range(16):
            c[i] ^= expansion_key[base + i]
    c = bytearray(_AES_INV_SBOX[x] for x in c)
    tmp = bytes(c)
    for i in range(16):
        c[i] = tmp[_SHIFT_ROWS_INV[i]]
    for i in range(16):
        c[i] ^= expansion_key[160 + i]
    return bytes(c)


def _gf256_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF256Exp[(GF256Log[a] + GF256Log[b]) % 0xFF]


def _rc4_mhy(buf, start, length, key, op):
    if length <= 0:
        return
    S = bytearray(RC4_KEY)
    T = bytearray(256)
    if len(key) == 256:
        T[:] = key
    else:
        for n in range(256):
            T[n] = key[n % len(key)]
    j = 0
    for i in range(256):
        j = (j + S[i] + T[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
    i = j = 0
    oplen = len(op)
    for it in range(length):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        K = S[(S[j] + S[i]) & 0xFF]
        o = op[i % oplen] % 3
        idx = start + it
        if o == 0:
            buf[idx] ^= K
        elif o == 1:
            buf[idx] = (buf[idx] - K) & 0xFF
        else:
            buf[idx] = (buf[idx] + K) & 0xFF


class _Mhy:
    __slots__ = ("name", "shiftrow", "key", "mul", "sbox")

    def __init__(self, name):
        self.name = name
        self.shiftrow = GIMhyShiftRow
        self.key = GIMhyKey
        self.mul = GIMhyMul
        self.sbox = GISBox


def _descramble_chunk(buf, start, length, mhy):
    for i in range(3):
        vector = bytearray(length)
        for j in range(length):
            k = mhy.shiftrow[(2 - i) * 0x10 + j]
            idx = j % 8
            vector[j] = mhy.key[idx] ^ mhy.sbox[(j % 4 * 0x100) | _gf256_mul(mhy.mul[idx], buf[start + (k % length)])]
        buf[start:start + length] = vector


def _descramble(buf, block_size, entry_size, mhy):
    rounded = (entry_size + 0xF) // 0x10 * 0x10
    if mhy.name in ("ZZZ", "ZZZ_CB2"):
        _descramble_chunk(buf, 4, 16, mhy)
        if bytes(buf[4:12]) != b"mhynewec":
            raise ValueError("bad signature, expected mhynewec got %r" % bytes(buf[4:12]))
        if block_size <= 35:
            return
        _descramble_chunk(buf, 20, 16, mhy)
        seed = bytes(buf[0:16])
        data = bytearray(aes128_ecb_encrypt_block(seed, bytes(buf[20:36])))
        buf[20:36] = data
        for i in range(4):
            buf[i] ^= data[i]
        _rc4_mhy(buf, 20 + rounded, block_size - (20 + rounded), bytes(buf[20:28]), bytes(buf[28:36]))
        return
    total = len(buf)
    for i in range(0, rounded, 0x10):
        _descramble_chunk(buf, i + 4, min(total - 4, 0x10), mhy)
    for i in range(4):
        buf[i] ^= buf[i + 4]
    finished = False
    current = rounded + 4
    while current < block_size and not finished:
        for i in range(entry_size):
            buf[i + current] ^= buf[i + 4]
            if i + current >= block_size - 1:
                finished = True
                break
        current += entry_size


try:
    from lz4.block import decompress as _lz4_native
except Exception:
    _lz4_native = None


def lz4_decompress(cmp, out_size):
    if _lz4_native is not None:
        try:
            return _lz4_native(bytes(cmp), uncompressed_size=out_size)
        except Exception:
            # malformed/truncated stream: fall back to the tolerant decoder below
            pass
    return _lz4_decompress_py(cmp, out_size)


def _lz4_decompress_py(cmp, out_size):
    dec = bytearray(out_size)
    cmp_pos = 0
    dec_pos = 0
    clen = len(cmp)
    while True:
        token = cmp[cmp_pos]
        cmp_pos += 1
        lit = (token >> 4) & 0xF
        if lit == 0xF:
            while True:
                s = cmp[cmp_pos]
                cmp_pos += 1
                lit += s
                if s != 0xFF:
                    break
        dec[dec_pos:dec_pos + lit] = cmp[cmp_pos:cmp_pos + lit]
        cmp_pos += lit
        dec_pos += lit
        if cmp_pos >= clen:
            break
        back = cmp[cmp_pos] | (cmp[cmp_pos + 1] << 8)
        cmp_pos += 2
        match = token & 0xF
        if match == 0xF:
            while True:
                s = cmp[cmp_pos]
                cmp_pos += 1
                match += s
                if s != 0xFF:
                    break
        match += 4
        enc_pos = dec_pos - back
        if match <= back:
            dec[dec_pos:dec_pos + match] = dec[enc_pos:enc_pos + match]
            dec_pos += match
        else:
            for _ in range(match):
                dec[dec_pos] = dec[enc_pos]
                dec_pos += 1
                enc_pos += 1
        if cmp_pos >= clen or dec_pos >= out_size:
            break
    return bytes(dec)


_ooz = None
_ooz_tried = False


def _find_ooz_dll():
    env = os.environ.get("WEM_FINDER_OOZ")
    if env and Path(env).is_file():
        return env
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for name in ("AnimeStudio.Ooz.dll", "ooz.dll", "oo2core_9_win64.dll"):
        for base in (here, here / "blkdec", Path(__file__).resolve().parent):
            p = base / name
            if p.is_file():
                return str(p)
    return None


def _load_ooz():
    global _ooz, _ooz_tried
    if _ooz_tried:
        return _ooz
    _ooz_tried = True
    path = _find_ooz_dll()
    if not path:
        return None
    try:
        dll = ctypes.WinDLL(path)
        fn = dll.Ooz_Decompress
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        _ooz = fn
    except Exception:
        _ooz = None
    return _ooz


def oodle_available():
    return _load_ooz() is not None


def oodle_decompress(cmp, out_size):
    fn = _load_ooz()
    if fn is None:
        raise RuntimeError("Oodle DLL not available (needed for this game/version)")
    # Oodle/Kraken can scribble a few bytes past the decoded end; give it slack so it
    # never overruns the heap (AnimeStudio gets this for free via ArrayPool.Rent).
    buf = ctypes.create_string_buffer(out_size + 0x10000)
    n = fn(bytes(cmp), len(cmp), buf, out_size, 1, 0, 0, None, 0, None, None, None, None, 3)
    if n != out_size:
        raise RuntimeError(f"Oodle decompress wrote {n}, expected {out_size}")
    return buf.raw[:out_size]


def _decompress(cmp, out_size, is_oodle):
    if is_oodle:
        return oodle_decompress(cmp, out_size)
    return lz4_decompress(cmp, out_size)


def _read_mhy_int(buf, pos):
    b = buf[pos:pos + 6]
    return (b[2] | (b[4] << 8) | (b[0] << 16) | (b[5] << 24)), pos + 6


def _read_mhy_uint(buf, pos):
    b = buf[pos:pos + 7]
    return (b[1] | (b[6] << 8) | (b[3] << 16) | (b[2] << 24)) & 0xFFFFFFFF, pos + 7


def _read_mhy_string(buf, pos):
    end = buf.find(b"\x00", pos)
    s = buf[pos:end]
    # fixed-width field
    return s, pos + 0x105


# Yields the blocks of the bundle at base; the end offset arrives via .append on the passed container.
def _iter_one_bundle(data, base, mhy):
    sig = bytes(data[base:base + 4])
    if sig not in (b"mhy0", b"mhy1"):
        raise ValueError("not a mhy file: %r" % sig)
    cbis = struct.unpack_from("<I", data, base + 4)[0]
    blocks_info = bytearray(data[base + 8:base + 8 + cbis])
    is_mhy0 = sig == b"mhy0"
    if is_mhy0:
        _descramble(blocks_info, 0x39, 0x1C, mhy)
        hdr_off = 32
    else:
        _descramble(blocks_info, min(len(blocks_info), 128), 28, mhy)
        hdr_off = 48

    pos = hdr_off
    uncompressed_bi_size, pos = _read_mhy_uint(blocks_info, pos)
    compressed_bi = bytes(blocks_info[pos:])
    is_oodle = len(compressed_bi) > 0 and compressed_bi[0] == 0x8C
    bi = _decompress(compressed_bi, uncompressed_bi_size, is_oodle)

    p = 0
    nodes_count, p = _read_mhy_int(bi, p)
    for _ in range(nodes_count):
        _, p = _read_mhy_string(bi, p)
        p += 1
        _, p = _read_mhy_int(bi, p)
        _, p = _read_mhy_uint(bi, p)
    blocks_count, p = _read_mhy_int(bi, p)
    block_infos = []
    for _ in range(blocks_count):
        csize_signed, p = _read_mhy_int(bi, p)
        usize, p = _read_mhy_uint(bi, p)
        block_infos.append((csize_signed & 0xFFFFFFFF, usize))

    data_pos = base + 8 + cbis
    blocks = []
    for csize, usize in block_infos:
        if csize < 0x10:
            raise ValueError("bad compressed block length %d" % csize)
        block = bytearray(data[data_pos:data_pos + csize])
        data_pos += csize
        if is_mhy0:
            _descramble(block, min(len(block), 0x21), 8, mhy)
            off = 12
        else:
            _descramble(block, min(len(block), 128), 8, mhy)
            off = 28
        blocks.append(_decompress(bytes(block[off:]), usize, is_oodle))
    return blocks, data_pos


def _iter_mhy_payload(data, game_name):
    mhy = _Mhy(game_name)
    n = len(data)
    base = 0
    while base + 8 <= n and bytes(data[base:base + 4]) in (b"mhy0", b"mhy1"):
        blocks, end = _iter_one_bundle(data, base, mhy)
        for b in blocks:
            yield b
        base = end
        # skip alignment padding to next bundle
        while base < n and data[base] == 0:
            base += 1


_MR0K_MAGIC = b"mr0k"


def _mr0k_decrypt(data, expansion_key, init_vector, block_key, sbox=None, post_key=None):
    data = bytearray(data)
    key1 = bytearray(data[4:0x14])
    key2 = bytearray(data[0x74:0x84])
    key3 = bytearray(data[0x84:0x94])
    encrypted_block_size = min(0x10 * ((len(data) - 0x94) >> 7), 0x400)
    if init_vector:
        for i in range(len(init_vector)):
            key2[i] ^= init_vector[i]
    if sbox:
        for i in range(0x10):
            key1[i] = sbox[(i % 4 * 0x100) | key1[i]]
    key1 = bytearray(aes_decrypt_expanded(bytes(key1), expansion_key))
    key3 = bytearray(aes_decrypt_expanded(bytes(key3), expansion_key))
    for i in range(len(key1)):
        key1[i] ^= key3[i]
    data[0x84:0x94] = key1
    seed1 = struct.unpack_from("<Q", key2, 0)[0]
    seed2 = struct.unpack_from("<Q", key3, 0)[0]
    seed = (seed2 ^ seed1 ^ (seed1 + len(data) - 20)) & 0xFFFFFFFFFFFFFFFF
    seed_span = struct.pack("<Q", seed)
    blk = data[0x94:0x94 + encrypted_block_size]
    for i in range(encrypted_block_size):
        blk[i] ^= seed_span[i % 8] ^ block_key[i % len(block_key)]
    data[0x94:0x94 + encrypted_block_size] = blk
    data = data[0x14:]
    if post_key:
        for i in range(0xC00):
            data[i] ^= post_key[i % len(post_key)]
    return bytes(data)


# name -> (kind, params). kind: "mhy" or "mr0k".
GAME_KEYS = {
    "GI": ("mhy", {"name": "GI"}),
    "ZZZ": ("mhy", {"name": "ZZZ"}),
    "SR": ("mr0k", {"expansion_key": Mr0kExpansionKey, "init_vector": Mr0kInitVector,
                    "block_key": Mr0kBlockKey}),
}


# One block at a time, without holding the whole payload: a Genshin .blk exceeds 160 MB.
def iter_blk_blocks(path, game):
    kind, params = GAME_KEYS[game]
    with open(path, "rb") as f:
        head = f.read(4)
        f.seek(0)
        data = f.read()
    if bytes(head) == b"Blb" + bytes([3]):
        from .blb import iter_blb_payload
        yield from iter_blb_payload(data)
        return
    if bytes(head) in (b"mhy0", b"mhy1"):
        name = params.get("name", "GI") if kind == "mhy" else "GI"
        yield from _iter_mhy_payload(data, name)
        return
    # unencrypted UnityFS bundle
    if bytes(head[:4]) == b"Unit":
        from .unityfs import iter_unityfs_payload
        yield from iter_unityfs_payload(data)
        return
    # HSR .block container (mr0k + Oodle/LZ4)
    if bytes(head[:4]) == b"ENCR":
        from .encr import iter_encr_payload
        yield from iter_encr_payload(data)
        return
    if kind == "mr0k":
        if _MR0K_MAGIC == bytes(head):
            data = _mr0k_decrypt(data, **params)
        # After mr0k the content is a UnityFS.
        # Without decompressing it no string would be readable.
        from .unityfs import iter_unityfs_payload
        if data[:7] == b"UnityFS":
            yield from iter_unityfs_payload(data)
        else:
            yield data


# Container picked by signature, not by game: Blb3, mhy0/mhy1 or mr0k; b'' if unrecognized.
def decrypt_blk_payload(path, game):
    kind, params = GAME_KEYS[game]
    with open(path, "rb") as f:
        head = f.read(4)
        f.seek(0)
        data = f.read()
    if bytes(head) == b"Blb\x03":
        from .blb import iter_blb_payload
        return b"".join(iter_blb_payload(data))
    if bytes(head) in (b"mhy0", b"mhy1"):
        name = params.get("name", "GI") if kind == "mhy" else "GI"
        return b"".join(_iter_mhy_payload(data, name))
    if bytes(head[:4]) == b"Unit":
        from .unityfs import iter_unityfs_payload
        return b"".join(iter_unityfs_payload(data))
    if kind == "mr0k":
        if _MR0K_MAGIC == bytes(head):
            data = _mr0k_decrypt(data, **params)
        from .unityfs import iter_unityfs_payload
        if data[:7] == b"UnityFS":
            return b"".join(iter_unityfs_payload(data))
        return data
    return b""
