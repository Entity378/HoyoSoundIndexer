# A wem is a RIFF (RIFX when big endian) whose chunks, unlike a real RIFF's, carry no padding byte.

import struct

# Covers the chunks before data on every wem of ZZZ, GI and HSR.
HEADER_BYTES = 4096
CODEC_VORBIS = 0xFFFF
# Platinum ADPCM, which GI and HSR use next to Vorbis.
CODEC_PTADPCM = 0x8311
# Every Wwise Vorbis of these games keeps its setup inside a fmt chunk of this size.
_VORBIS_FMT_SIZE = 0x42


# Milliseconds, or None for another codec or a cut header; checked against vgmstream on both codecs.
# Vorbis stores its sample count in the fmt extension, and a PTADPCM frame decodes (size - 4) * 2 samples.
def wem_duration_ms(head):
    if head[:4] == b"RIFF":
        order = "<"
    elif head[:4] == b"RIFX":
        order = ">"
    else:
        return None
    fmt, fmt_size, data_size = None, 0, None
    pos = 12
    while pos + 8 <= len(head):
        tag = head[pos:pos + 4]
        size = struct.unpack_from(order + "I", head, pos + 4)[0]
        if tag == b"fmt ":
            fmt, fmt_size = pos + 8, size
        elif tag == b"data":
            data_size = size
            break
        pos += 8 + size
    if fmt is None or fmt + 16 > len(head):
        return None
    codec, channels, rate, _byte_rate, block_align = struct.unpack_from(order + "HHIIH", head, fmt)
    if not channels or not rate:
        return None
    if codec == CODEC_VORBIS and fmt_size == _VORBIS_FMT_SIZE and fmt + 0x1C <= len(head):
        samples = struct.unpack_from(order + "I", head, fmt + 0x18)[0]
    elif codec == CODEC_PTADPCM and data_size is not None and block_align >= channels * 6:
        frame = block_align // channels
        samples = data_size // block_align * (frame - 4) * 2
    else:
        return None
    return samples * 1000 // rate
