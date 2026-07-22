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
