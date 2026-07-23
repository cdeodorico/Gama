"""Saved filter presets and trial/AOI schemes, one JSON file each."""

import json
import os
import re
import threading

from .paths import BASE_DIR


DEFAULT_PRESETS_DIR = os.path.join(BASE_DIR, "presets")

_PRESETS_LOCK = threading.Lock()



# ---------------------------------------------------------------------------
# Presets on disk -- one JSON file per preset inside a "presets" folder.
# ---------------------------------------------------------------------------
def _preset_slug(name):
    slug = re.sub(r"[^\w.-]+", "_", name).strip("_")
    return slug or "preset"



def _schemes_dir(presets_dir):
    """Trial/AOI schemes live beside the presets folder."""
    return os.path.join(os.path.dirname(os.path.abspath(presets_dir)), "schemes")



def _load_presets(dir_path):
    """Return {name: config} by reading every *.json in the folder."""
    out = {}
    try:
        names = sorted(os.listdir(dir_path))
    except FileNotFoundError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(dir_path, fn)) as fh:
                obj = json.load(fh)
            name = obj.get("name") or os.path.splitext(fn)[0]
            out[name] = obj.get("config", {})
        except Exception:
            continue
    return out



def _find_preset_file(dir_path, name):
    try:
        files = os.listdir(dir_path)
    except FileNotFoundError:
        return None
    for fn in files:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(dir_path, fn)) as fh:
                if json.load(fh).get("name") == name:
                    return os.path.join(dir_path, fn)
        except Exception:
            continue
    return None



def _save_preset(dir_path, name, config):
    with _PRESETS_LOCK:
        os.makedirs(dir_path, exist_ok=True)
        path = _find_preset_file(dir_path, name)
        if path is None:
            slug = _preset_slug(name)
            path = os.path.join(dir_path, slug + ".json")
            i = 1
            while os.path.exists(path):
                path = os.path.join(dir_path, f"{slug}_{i}.json")
                i += 1
        with open(path, "w") as fh:
            json.dump({"name": name, "config": config}, fh, indent=2)



def _delete_preset(dir_path, name):
    with _PRESETS_LOCK:
        path = _find_preset_file(dir_path, name)
        if path and os.path.isfile(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Recently opened files (a small list beside the presets folder)
# ---------------------------------------------------------------------------
RECENT_MAX = 12
_RECENT_LOCK = threading.Lock()


def _recent_path(presets_dir):
    return os.path.join(os.path.dirname(os.path.abspath(presets_dir)),
                        "recent-files.json")


def load_recent(presets_dir):
    """Recently opened recordings, newest first, with dead paths dropped."""
    try:
        with open(_recent_path(presets_dir), "r", encoding="utf-8") as fh:
            items = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        p = it.get("path") if isinstance(it, dict) else it
        if not p:
            continue
        out.append({"path": p, "name": os.path.basename(p),
                    "folder": os.path.dirname(p),
                    "exists": os.path.isfile(p),
                    "opened": (it.get("opened") if isinstance(it, dict) else None)})
    return out[:RECENT_MAX]


def add_recent(presets_dir, paths):
    """Push paths onto the front of the recent list."""
    import time as _time
    with _RECENT_LOCK:
        current = []
        try:
            with open(_recent_path(presets_dir), "r", encoding="utf-8") as fh:
                current = json.load(fh)
        except (OSError, ValueError):
            current = []
        if not isinstance(current, list):
            current = []
        now = int(_time.time())
        for p in reversed([p for p in paths if p]):
            ap = os.path.abspath(p)
            current = [c for c in current
                       if (c.get("path") if isinstance(c, dict) else c) != ap]
            current.insert(0, {"path": ap, "opened": now})
        current = current[:RECENT_MAX]
        try:
            os.makedirs(os.path.dirname(_recent_path(presets_dir)), exist_ok=True)
            tmp = _recent_path(presets_dir) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2)
            os.replace(tmp, _recent_path(presets_dir))
        except OSError:
            pass
    return load_recent(presets_dir)


def clear_recent(presets_dir):
    try:
        os.remove(_recent_path(presets_dir))
    except OSError:
        pass
    return []