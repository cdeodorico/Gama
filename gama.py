#!/usr/bin/env python3
r"""
gama -- EyeLink EDF Explorer
============================

Open SR Research EyeLink ``.EDF`` recordings and explore them in a fast,
filterable, spreadsheet-like web UI, or filter and convert them from the
command line.

This file is only the entry point.  The code lives in the ``gamalib``
package next to it, a module per concern -- see ``gamalib/__init__.py``.

    python gama.py                     # open the app in a browser
    python gama.py a.EDF b.EDF         # ...with files already open
    python gama.py rec.EDF --stats     # head-less summary
    python gama.py *.EDF --export out/ --format csv --only FIX
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULES = ("version", "edfapi", "convert", "paths", "dataset", "filters",
            "exports", "notes", "preset_store", "files", "trials",
            "diagnostics", "server", "cli")

# Running from source, make sure the folder holding this script is importable
# even when it was launched by absolute path from somewhere else.
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _import_failed(exc):
    """Explain the ways this import realistically breaks, then bail."""
    pkg = os.path.join(_HERE, "gamalib")
    sys.stderr.write("gama could not load its own code.\n\n")
    sys.stderr.write("  %s: %s\n\n" % (type(exc).__name__, exc))

    if not os.path.isdir(pkg):
        sys.stderr.write(
            "The 'gamalib' folder is missing. It must sit next to gama.py.\n")
        raise SystemExit(2)

    missing = [m for m in ("__init__",) + _MODULES
               if not os.path.isfile(os.path.join(pkg, m + ".py"))]
    if missing:
        sys.stderr.write("These files are missing from gamalib/: %s\n"
                         % ", ".join(m + ".py" for m in missing))
        raise SystemExit(2)

    # Everything is present, so the imports themselves were edited.  The usual
    # culprit is an editor rewriting "from .module import x" into
    # "from module import x", which then collides with whatever else is on
    # sys.path -- including folders sitting beside the app.
    bad = []
    for fn in sorted(os.listdir(pkg)):
        if not fn.endswith(".py"):
            continue
        try:
            with open(os.path.join(pkg, fn), encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    t = line.strip()
                    if t.startswith("from ") and " import " in t:
                        mod = t.split()[1]
                        if (not mod.startswith(".")
                                and mod.split(".")[0] in _MODULES):
                            bad.append("    gamalib/%s:%d  %s" % (fn, i, t))
        except OSError:
            pass
    if bad:
        sys.stderr.write(
            "Some imports inside gamalib/ were changed from relative to\n"
            "absolute. They need the leading dot -- 'from .files import x',\n"
            "not 'from files import x':\n\n")
        sys.stderr.write("\n".join(bad[:20]) + "\n\n")
        sys.stderr.write("Re-copy those files, or put the dots back.\n")
    raise SystemExit(2)


try:
    from gamalib.cli import main
except Exception as exc:
    _import_failed(exc)


if __name__ == "__main__":
    main()
