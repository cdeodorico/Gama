"""The "copy diagnostics" payload: everything worth pasting into a report."""

import sys

from .version import __version__
from .edfapi import edfapi_version
from .paths import BASE_DIR, HTML_FILENAME, _resource_bytes
from . import updates
from . import files as _files



def diagnostics(presets_dir):
    """Everything worth pasting into a bug report."""
    import platform
    try:
        import eyelinkio
        elio = getattr(eyelinkio, "__version__", "?")
    except Exception:
        elio = "not importable"
    return {
        "gama": __version__,
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "eyelinkio": elio,
        "edfapi": edfapi_version(),
        "base_dir": BASE_DIR,
        "presets_dir": presets_dir,
        "ui": "index.html" if _resource_bytes(HTML_FILENAME) else "MISSING",
        "last_error": _files.LAST_ERROR,
        "latest_version": updates.status().get("latest"),
    }