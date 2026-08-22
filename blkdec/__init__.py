# Decryptor for HoYoverse .blk bundles, ported from AnimeStudio (Escartem, GPL-3.0).
# mhy0/mhy1 and Blb3 (GI/ZZZ), mr0k (SR): returns the decompressed payload for string scraping.

from .mhy import decrypt_blk_payload, iter_blk_blocks, GAME_KEYS, oodle_available
