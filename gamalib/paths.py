"""Where things live on disk, and loading bundled resources.

Frozen builds keep data next to the executable so presets and schemes
survive between runs; from source everything sits next to gama.py.
"""

import os
import sys


# When bundled by PyInstaller, __file__ points inside the temporary extraction
# directory; use the executable's own folder so things like the presets folder
# live next to the program and persist between runs.
if getattr(sys, "frozen", False):
    _exe_dir = os.path.dirname(sys.executable)
    # macOS .app: .../gama.app/Contents/MacOS/gama -> keep data beside the .app
    if sys.platform == "darwin" and _exe_dir.endswith("/Contents/MacOS"):
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_exe_dir)))
    else:
        BASE_DIR = _exe_dir
else:
    # gamalib/paths.py -> the folder holding gama.py and index.html
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



def _resource_bytes(name):
    """Read a bundled resource (works both as a script and when frozen)."""
    for base in (getattr(sys, "_MEIPASS", None), BASE_DIR):
        if base:
            p = os.path.join(base, name)
            if os.path.isfile(p):
                try:
                    with open(p, "rb") as fh:
                        return fh.read()
                except OSError:
                    pass
    return None



ICON_BYTES = _resource_bytes("icon.png")



# ---------------------------------------------------------------------------
# Single-page web UI.
#
# The UI lives in index.html next to this file.  It is bundled into the
# executable by gama.spec, so a frozen build stays a single file; when running
# from source it is re-read on every request, so you can edit the HTML and just
# refresh the browser.
# ---------------------------------------------------------------------------
HTML_FILENAME = "index.html"

_HTML_CACHE = None


_HTML_MISSING = """<!DOCTYPE html><html><body style="font:14px sans-serif;
padding:2em;background:#0f1115;color:#e6e9ef">
<h2>index.html not found</h2>
<p>gama's user interface lives in <code>{name}</code>, which should sit next to
<code>gama.py</code>. Looked in:</p><pre>{where}</pre>
</body></html>"""



def html_page():
    """The UI markup: cached when frozen, re-read from disk when developing."""
    global _HTML_CACHE
    if _HTML_CACHE is not None:
        return _HTML_CACHE
    data = _resource_bytes(HTML_FILENAME)
    if data is None:
        looked = "\n".join(
            os.path.join(b, HTML_FILENAME)
            for b in (getattr(sys, "_MEIPASS", None), BASE_DIR) if b)
        return _HTML_MISSING.format(name=HTML_FILENAME, where=looked)
    text = data.decode("utf-8")
    if getattr(sys, "frozen", False):
        _HTML_CACHE = text          # bundled copy can never change at runtime
    return text
