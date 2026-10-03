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
# The server caps a page at 1000 entries, and 100 pages bound a server that ignores the page number.
_GITEA_TREE_PAGE = 1000
_GITEA_TREE_MAX_PAGES = 100


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


# The contents API reads the last commit of every file and times out on the 1700 ZZZ tables.
# The git trees API walks down from the ref instead, and lists a folder in pages.
def gitea_list_dir(source, dirpath):
    host = source.get("host", _DEFAULT_GITEA_HOST)
    trees = f"{host}/api/v1/repos/{source['project']}/git/trees"
    sha = source["ref"]
    for part in [part for part in dirpath.split("/") if part]:
        sha = next((entry.get("sha") for entry in _gitea_tree(trees, sha)
                    if entry.get("path") == part and entry.get("type") == "tree"), None)
        if not sha:
            raise RuntimeError(f"no folder {dirpath} in {source['project']}")
    return [entry["path"] for entry in _gitea_tree(trees, sha) if entry.get("type") == "blob"]


# Gitea leaves truncated set up to the last page, so the count tells when the listing is complete.
def _gitea_tree(trees, sha):
    entries = []
    for page in range(1, _GITEA_TREE_MAX_PAGES + 1):
        doc = json.loads(http_get(f"{trees}/{sha}?page={page}&per_page={_GITEA_TREE_PAGE}")
                         .decode("utf-8", "ignore"))
        tree = [entry for entry in doc.get("tree") or [] if isinstance(entry, dict)]
        entries += tree
        total = doc.get("total_count")
        if not tree or not doc.get("truncated") or (isinstance(total, int) and len(entries) >= total):
            break
    return entries


def source_raw(source, relative_path):
    if source.get("host"):
        return gitea_raw(source, relative_path)
    return gitlab_raw(source, relative_path)


def source_json(source, relative_path):
    return json.loads(source_raw(source, relative_path).decode("utf-8", "ignore"))
