# -*- mode: python ; coding: utf-8 -*-
# Cross-platform build for the EDF Explorer.  Run:  pyinstaller gama.spec
#   * Windows  -> dist/gama.exe   (icon from icon.ico)
#   * macOS    -> dist/gama       (executable) and dist/gama.app (icon from icon.icns)
#   * Linux    -> dist/gama       (no icon)
# NOTE: PyInstaller does NOT cross-compile -- build ON the OS you are targeting.
import os
import sys
import eyelinkio
from PyInstaller.utils.hooks import collect_all

# eyelinkio (edfapi ctypes wrapper) + its data/binaries
datas, binaries, hiddenimports = collect_all("eyelinkio")

# eyelinkio's native edfapi library lives in a "libedfapi" folder that is a
# SIBLING of the package; place it at the bundle root (this includes the
# Windows DLLs, the Linux .so AND the macOS edfapi.framework).
_sp = os.path.dirname(os.path.dirname(eyelinkio.__file__))
datas += [(os.path.join(_sp, "libedfapi"), "libedfapi")]

try:
    _here = SPECPATH                       # dir containing this .spec
except NameError:
    _here = os.getcwd()

# --- the web UI ------------------------------------------------------------
_html = os.path.join(_here, "index.html")
if not os.path.exists(_html):
    raise SystemExit("gama.spec: index.html is missing -- it must sit next to "
                     "gama.py and this spec.")
datas += [(_html, ".")]

# --- application icon (per-platform) ---------------------------------------
_png = os.path.join(_here, "icon.png")

if os.path.exists(_png):
    datas += [(_png, ".")]                 # bundle it so the web UI can serve it

_fmt = {"win32": ".ico", "darwin": ".icns"}.get(sys.platform)  # None on Linux
icon_arg = None
if _fmt:
    _premade = os.path.join(_here, "icon" + _fmt)  # a hand-made .ico/.icns wins
    if os.path.exists(_premade):
        icon_arg = _premade
    elif os.path.exists(_png):
        try:
            from PIL import Image
            _out = os.path.join(_here, "icon" + _fmt)
            _src = Image.open(_png).convert("RGBA")
            if _fmt == ".ico":
                _src.save(_out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                       (64, 64), (128, 128), (256, 256)])
            else:  # .icns
                _src.resize((1024, 1024)).save(_out)
            icon_arg = _out
        except Exception as e:
            print(f"[gama.spec] could not build icon{_fmt} ({e!r}); "
                  f"install Pillow (pip install pillow) or drop an icon{_fmt} "
                  f"next to this spec.")
            icon_arg = _png

a = Analysis(
    ["gama.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="gama",
    console=True,            # keep the console: shows the URL, allows Ctrl-C
    upx=True,
    icon=icon_arg,
)

# On macOS also wrap the executable in a double-clickable .app carrying the icon.
# (Delete this block if you only want the bare Unix executable.)
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="gama.app",
        icon=icon_arg,
        bundle_identifier="edu.gama.edfexplorer",
    )
