"""Access to SR Research's native edfapi library, via eyelinkio.

This is the only place that touches the C library directly.
"""

import os
import sys



# ---------------------------------------------------------------------------
# edfapi access (cross-platform) via eyelinkio's ctypes wrapper.
# ---------------------------------------------------------------------------
def _load_edfapi():
    """Import the eyelinkio ctypes wrapper around the real edfapi library."""
    for p in (
        "/usr/local/lib/python3.12/dist-packages",
        "/usr/lib/python3/dist-packages",
    ):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    try:
        from eyelinkio.edf import _edf2py as E
    except Exception as exc:  # pragma: no cover - environment specific
        raise SystemExit(
            "Could not load edfapi via the 'eyelinkio' package.\n"
            "Install it with `pip install eyelinkio` (it bundles the SR "
            "Research edfapi library), or set EYELINKIO_USE_INSTALLED_EDFAPI="
            "true to use a system-installed edfapi.\n"
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
