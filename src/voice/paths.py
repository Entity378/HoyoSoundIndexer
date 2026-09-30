import re

from src.vocabulary import VO_CATEGORIES

# These run on lowercased bytes: without IGNORECASE they are three times faster.
VO_PREFIX_RE = re.compile(rb"(?<![0-9a-z_/\\])vo_[0-9a-z_]+(?:[/\\][0-9a-z_]+)*[/\\]")
VO_PATH_RE = re.compile(rb"(?<![0-9a-z_/\\])vo_[0-9a-z_]+(?:[/\\][0-9a-z_]+)+")
VO_LEAF_RE = re.compile(
    (r"(?<![0-9a-z_/])(?:%s)_[0-9a-z_]{2,150}"
     % "|".join(sorted({c.lower() for c in VO_CATEGORIES}, key=len, reverse=True))).encode())


_VOICE_PATH_LEN = (5, 299)


def is_voice_file_path(text):
    low, high = _VOICE_PATH_LEN
    return text.lower().endswith(".wem") and low <= len(text) <= high


# Not lowercased here: callers hash the lowercase path and display the original one.
def voice_tail(path, suffix):
    base = path[:-4] if path.lower().endswith(".wem") else path
    return (base + suffix + ".wem").encode()


def split_voice_path(path):
    cut = max(path.rfind("\\"), path.rfind("/"))
    if cut < 0:
        return "", path
    return path[:cut], path[cut + 1:]


def speaker_of_folder(folder):
    tail = split_voice_path(folder)[1].lower()
    return tail[3:] if tail.startswith("vo_") else ""
