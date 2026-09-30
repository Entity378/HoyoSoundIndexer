# Downloads try urllib with certifi's bundle, then the system one, then curl, whichever gets through.

import json
import ssl
import subprocess
import urllib.request

from src.config import hidden_window_kwargs

try:
    import certifi
except ImportError:
    certifi = None

# Some hosts refuse urllib's default agent.
_USER_AGENT = "HoyoSoundIndexer"
_DEFAULT_GITEA_HOST = "https://git.mero.moe"


def http_get(url, timeout=300):
    contexts = []
    if certifi is not None:
        try:
            contexts.append(ssl.create_default_context(cafile=certifi.where()))
        except Exception:
            pass
    try:
        contexts.append(ssl.create_default_context())
    except Exception:
        pass
    last = None
    for context in contexts:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.read()
        except Exception as e:
            last = e
    try:
        out = subprocess.run(["curl", "-sfL", url], capture_output=True, timeout=timeout,
                             **hidden_window_kwargs())
        if out.returncode == 0 and out.stdout:
            return out.stdout
        last = RuntimeError(out.stderr.decode("utf-8", "replace")[:200] or "curl failed")
    except Exception as e:
        last = e
    raise RuntimeError(f"download failed: {url}: {last}")


# A tar.gz of one subtree instead of the whole repo.
def gitlab_archive(source, subtree):
    project = source["project"].replace("/", "%2F")
    return http_get(f"https://gitlab.com/api/v4/projects/{project}/repository/archive.tar.gz"
                    f"?path={subtree}&sha={source['ref']}")


def gitlab_raw(source, relative_path):
    return http_get(f"https://gitlab.com/{source['project']}/-/raw/{source['ref']}/{relative_path}")


def gitea_raw(source, relative_path):
    host = source.get("host", _DEFAULT_GITEA_HOST)
    return http_get(f"{host}/{source['project']}/raw/branch/{source['ref']}/{relative_path}")


def gitea_list_dir(source, dirpath):
    host = source.get("host", _DEFAULT_GITEA_HOST)
    data = http_get(f"{host}/api/v1/repos/{source['project']}/contents/{dirpath}?ref={source['ref']}")
    entries = json.loads(data.decode("utf-8", "ignore"))
    return [entry["name"] for entry in entries if isinstance(entry, dict) and entry.get("type") == "file"]


def source_raw(source, relative_path):
    if source.get("host"):
        return gitea_raw(source, relative_path)
    return gitlab_raw(source, relative_path)


def source_json(source, relative_path):
    return json.loads(source_raw(source, relative_path).decode("utf-8", "ignore"))
