import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from shutil import which

from src.config import hidden_window_kwargs, tools_dir
from src.online.http import http_get

# vgmstream is not bundled: the latest release is downloaded once into the tools folder.
VGMSTREAM_RELEASE_API = "https://api.github.com/repos/vgmstream/vgmstream/releases/latest"
VGMSTREAM_ASSETS = {"win32": "vgmstream-win64.zip", "linux": "vgmstream-linux.zip",
                    "darwin": "vgmstream-mac.zip"}
VGMSTREAM_ENV = "HSI_VGMSTREAM"
_CONVERT_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 180


def extract_wem_bytes(location):
    with open(location.file_path, "rb") as f:
        f.seek(location.offset)
        return f.read(location.size)


# Two jobs with one stem, a bank stub and the streamed file of one wem, get _2, _3 and so on.
# Returns (written, failed).
def export_wems(jobs, out_dir, progress=None, cancelled=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written, failed, used = 0, 0, set()
    total = len(jobs)
    for i, (stem, location) in enumerate(jobs):
        if cancelled and cancelled():
            break
        name, number = stem, 2
        while name.lower() in used:
            name, number = f"{stem}_{number}", number + 1
        used.add(name.lower())
        try:
            (out / f"{name}.wem").write_bytes(extract_wem_bytes(location))
            written += 1
        except Exception:
            failed += 1
        if progress and (i % 8 == 0 or i == total - 1):
            progress(i + 1, total, f"Exporting {i + 1:,} of {total:,}...")
    return written, failed


def safe_file_stem(text, limit=80):
    text = str(text).strip()
    if text.lower().endswith(".wem"):
        text = text[:-4]
    text = re.sub(r"[\\/]+", "_", text)
    text = re.sub(r'[<>:"|?*\x00-\x1f]+', "_", text)
    text = text.strip(" ._")
    return text[:limit] or "wem"


def _vgmstream_exe_name():
    return "vgmstream-cli.exe" if sys.platform == "win32" else "vgmstream-cli"


def vgmstream_dir():
    return tools_dir() / "vgmstream"


# An existing install is never downloaded twice: the override, our copy, the exe's folder, XXAR's, PATH.
def find_vgmstream():
    exe = _vgmstream_exe_name()
    override = os.environ.get(VGMSTREAM_ENV)
    if override and Path(override).is_file():
        return override
    candidates = [vgmstream_dir() / exe]
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).parent
        candidates += [here / exe, here / "vgmstream" / exe]
    if sys.platform == "win32":
        data_home = Path(os.environ.get("LOCALAPPDATA", ""))
    else:
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    candidates.append(data_home / "XXAR" / "tools" / "audio" / "vgmstream" / exe)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return which("vgmstream-cli")


# The zip has its own top folder, so only the files next to the exe are kept, flattened.
def download_vgmstream(progress=None):
    exe = _vgmstream_exe_name()
    asset_name = VGMSTREAM_ASSETS.get(sys.platform)
    if not asset_name:
        raise RuntimeError(f"no vgmstream build for {sys.platform}")
    if progress:
        progress(0, 1, "Looking up the latest vgmstream release...")
    release = json.loads(http_get(VGMSTREAM_RELEASE_API).decode("utf-8", "ignore"))
    tag = release.get("tag_name", "")
    asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
    if not asset:
        raise RuntimeError(f"{asset_name} missing from vgmstream {tag or '?'}")
    if progress:
        progress(0, 1, f"Downloading vgmstream {tag} ({asset['size'] // 1000000} MB)...")
    blob = http_get(asset["browser_download_url"], timeout=_DOWNLOAD_TIMEOUT)

    target = vgmstream_dir()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        root = next((name[:-len(exe)] for name in members if name.rsplit("/", 1)[-1] == exe), "")
        for member in members:
            if root and not member.startswith(root):
                continue
            name = member[len(root):]
            if "/" in name:
                continue
            (target / name).write_bytes(archive.read(member))
    result = target / exe
    if not result.is_file():
        raise RuntimeError(f"{exe} not found inside {asset_name}")
    if sys.platform != "win32":
        result.chmod(0o755)
    if progress:
        progress(1, 1, f"vgmstream {tag} ready")
    return str(result)


def wem_to_wav(wem_bytes, vgmstream, out_dir, stem="hsi_current"):
    wem_path = Path(out_dir) / f"{stem}.wem"
    wav_path = Path(out_dir) / f"{stem}.wav"
    wem_path.write_bytes(wem_bytes)
    result = subprocess.run([vgmstream, "-o", str(wav_path), str(wem_path)],
                            capture_output=True, timeout=_CONVERT_TIMEOUT, **hidden_window_kwargs())
    if result.returncode != 0 or not wav_path.exists():
        raise RuntimeError(result.stderr.decode(errors="replace")[:300] or "vgmstream failed")
    return str(wav_path)
