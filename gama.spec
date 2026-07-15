# -*- mode: python ; coding: utf-8 -*-
# Build the EDF Explorer into a single .exe.  Run:  pyinstaller gama.spec
import os
import eyelinkio
from PyInstaller.utils.hooks import collect_all

# eyelinkio (edfapi ctypes wrapper) + its data/binaries
datas, binaries, hiddenimports = collect_all("eyelinkio")

# eyelinkio's native edfapi library lives in a "libedfapi" folder that is a
# SIBLING of the package; place it at the bundle root, where eyelinkio looks.
_sp = os.path.dirname(os.path.dirname(eyelinkio.__file__))
datas += [(os.path.join(_sp, "libedfapi"), "libedfapi")]

# --- application icon -------------------------------------------------------
try:
    _here = SPECPATH                      # dir containing this .spec
except NameError:
    _here = os.getcwd()
_png = os.path.join(_here, "icon.png")

icon_arg = None
if os.path.exists(_png):
    datas += [(_png, ".")]                # bundle it so the web UI can serve it
    icon_arg = _png                       # PyInstaller converts png->ico (needs Pillow)
    try:                                  # make a real multi-size .ico explicitly
        from PIL import Image
        _ico = os.path.join(_here, "icon.ico")
        Image.open(_png).save(
            _ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                         (64, 64), (128, 128), (256, 256)])
        icon_arg = _ico
    except Exception as e:
        print(f"[gama.spec] icon.png -> icon.ico conversion skipped ({e!r}); "
              f"passing icon.png (requires Pillow: pip install pillow)")

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
