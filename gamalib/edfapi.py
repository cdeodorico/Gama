"""Access to SR Research's native edfapi library, via eyelinkio.

This is the only place that touches the C library directly.
"""

import os
import sys



# ---------------------------------------------------------------------------
# edfapi access (cross-platform) via eyelinkio's ctypes wrapper.
# ---------------------------------------------------------------------------
def _prepare_frozen_edfapi():
    """When frozen, make eyelinkio's bundled native library loadable.

    eyelinkio locates its library relative to the eyelinkio package (``.. /
    libedfapi/<platform>/...``), which the spec reproduces inside the bundle.
    But the Windows ``edfapi64.dll`` also depends on ``zlibwapi.dll`` sitting
    beside it, and Windows only finds that if the folder is on the DLL search
    path. Add every libedfapi sub-folder so the dependency resolves.
    """
    root = getattr(sys, "_MEIPASS", None)
    if not root:
        return
    base = os.path.join(root, "libedfapi")
    if not os.path.isdir(base):
        return
    dirs = [base]
    for r, _sub, _files in os.walk(base):
        dirs.append(r)
    for d in dirs:
        try:
            if hasattr(os, "add_dll_directory"):    # Windows, Python 3.8+
                os.add_dll_directory(d)
        except (OSError, FileNotFoundError):
            pass
    # also expose it on PATH as a belt-and-braces fallback
    os.environ["PATH"] = os.pathsep.join(
        dirs + [os.environ.get("PATH", "")])


def _load_edfapi():
    """Import the eyelinkio ctypes wrapper around the real edfapi library."""
    if getattr(sys, "frozen", False):
        _prepare_frozen_edfapi()
    else:
        # From source: help the interpreter find a system site-packages install
        # if it isn't already on the path (common on Linux). When frozen,
        # eyelinkio is bundled into the app and these paths must NOT be added.
        import sysconfig
        candidates = [sysconfig.get_paths().get("purelib"),
                      sysconfig.get_paths().get("platlib")]
        try:
            import site
            candidates += list(getattr(site, "getsitepackages", lambda: [])())
        except Exception:
            pass
        for p in candidates:
            if p and os.path.isdir(p) and p not in sys.path:
                sys.path.append(p)
    try:
        from eyelinkio.edf import _edf2py as E
    except Exception as exc:  # pragma: no cover - environment specific
        raise SystemExit(
            "Could not load edfapi via the 'eyelinkio' package.\n"
            "Install it with `pip install eyelinkio` (it bundles the SR "
            "Research edfapi library), or set EYELINKIO_USE_INSTALLED_EDFAPI="
            "true to use a system-installed edfapi.\n"
            "If this is a frozen build, rebuild with the current gama.spec so "
            "eyelinkio and its libedfapi library are collected.\n"
            f"Original error: {exc!r}"
        )
    return E


# ---------------------------------------------------------------------------
# EDF element type codes (from the edfapi headers).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# EDF element type codes (from edfapi headers).
# ---------------------------------------------------------------------------
STARTBLINK = 3

ENDBLINK = 4

STARTSACC = 5

ENDSACC = 6

STARTFIX = 7

ENDFIX = 8

MESSAGEEVENT = 24

BUTTONEVENT = 25

INPUTEVENT = 28

RECORDING_INFO = 30

NO_PENDING_ITEMS = 0


EYE_LETTER = {0: "L", 1: "R", 2: "L"}

EYE_WORD = {0: "LEFT", 1: "RIGHT", 2: "BINOCULAR"}


def edfapi_version():
    """The edfapi build string, or a note about why it is unavailable."""
    try:
        E = _load_edfapi()
        v = E.edf_get_version()
        return v.decode("latin-1", "replace") if isinstance(v, bytes) else str(v)
    except BaseException as exc:
        return "unavailable (%r)" % (exc,)