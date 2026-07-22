"""Per-row flags and notes, stored in a sidecar beside the .EDF."""

import json
import os
import threading

from .version import __version__

_NOTES_LOCK = threading.Lock()



def _notes_path(edf_path):
    """Sidecar file for a recording's row flags/notes, next to the .EDF."""
    return edf_path + ".gama-notes.json"



def load_notes(edf_path):
    """Return {line_index(str): {"flag": bool, "note": str}} for a recording."""
    p = _notes_path(edf_path)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        notes = data.get("notes", data)          # tolerate a bare mapping
        out = {}
        for k, v in notes.items():
            if isinstance(v, dict):
                out[str(k)] = {"flag": bool(v.get("flag")),
                               "note": str(v.get("note") or "")}
        return out
    except (OSError, ValueError):
        return {}



def save_notes(edf_path, notes):
    """Write the notes sidecar; drop empty entries; remove the file if none."""
    clean = {}
    for k, v in (notes or {}).items():
        flag = bool(v.get("flag"))
        note = str(v.get("note") or "").strip()
        if flag or note:
            clean[str(k)] = {"flag": flag, "note": note}
    p = _notes_path(edf_path)
    with _NOTES_LOCK:
        try:
            if not clean:
                if os.path.isfile(p):
                    os.remove(p)
                return {}
            payload = {"tool": "gama", "version": __version__,
                       "source_file": os.path.basename(edf_path),
                       "notes": clean}
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, p)
        except OSError:
            pass
    return clean
