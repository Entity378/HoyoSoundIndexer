# FNV-1 the way Wwise computes ids: the name is lowercased and hashed as UTF-8.
# 32 bits name events, banks and syncs; 64 bits name the paths of the streamed voice files.

FNV32_OFFSET = 0x811C9DC5
FNV32_PRIME = 0x01000193
FNV64_OFFSET = 0xCBF29CE484222325
FNV64_PRIME = 0x100000001B3
_MASK32 = 0xFFFFFFFF
_MASK64 = 0xFFFFFFFFFFFFFFFF


# Inlined instead of calling fnv32_feed, since the crackers call it tens of millions of times.
def fnv1_32(name):
    state = FNV32_OFFSET
    for byte in name.lower().encode("utf-8"):
        state = (state * FNV32_PRIME) & _MASK32
        state ^= byte
    return state


def fnv1_64(name):
    state = FNV64_OFFSET
    for byte in name.lower().encode("utf-8"):
        state = (state * FNV64_PRIME) & _MASK64
        state ^= byte
    return state


# Continues from a given state, so a prefix shared by many candidates is hashed once.
def fnv32_feed(data, state=FNV32_OFFSET):
    for byte in data:
        state = (state * FNV32_PRIME) & _MASK32
        state ^= byte
    return state


def fnv64_feed(data, state=FNV64_OFFSET):
    for byte in data:
        state = (state * FNV64_PRIME) & _MASK64
        state ^= byte
    return state
