"""Open files: lazy parsing, the tab registry, browsing and folder watching."""

import gzip
import json
import os
import threading

from .dataset import build_dataset
from .notes import load_notes

# Set when a parse fails, surfaced by the diagnostics payload.
LAST_ERROR = None



# ---------------------------------------------------------------------------
# HTTP server -- multiple files, parsed lazily and cached.
# ---------------------------------------------------------------------------
class FileEntry:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.base = os.path.splitext(self.name)[0]
        self.lock = threading.Lock()
        self.ready = False
        self.error = None
        self.parsing = False       # True while the parse thread is running
        self.progress = 0          # items read so far (no total is available)
        self.tmin = self.tmax = 0
        self.records = self.parsed = self.rows = self.payload_gz = None
        self.notes = {}            # line_index(str) -> {"flag":bool,"note":str}
        self.trials = None         # cached trial/AOI analysis for exports



def _ensure_parsed(entry, converted_from_line):
    with entry.lock:
        if entry.ready or entry.error:
            return
        try:
            print(f"Parsing {entry.name} ...", flush=True)
            entry.parsing = True
            entry.progress = 0

            def _prog(n):
                entry.progress = n

            records, payload, parsed = build_dataset(
                entry.path, converted_from_line, _prog)
            entry.records, entry.parsed, entry.rows = records, parsed, payload["rows"]
            entry.notes = load_notes(entry.path)
            entry.tmin, entry.tmax = payload["meta"]["tmin"], payload["meta"]["tmax"]
            entry.payload_gz = gzip.compress(
                json.dumps(payload, separators=(",", ":")).encode("utf-8"), 6)
            entry.ready = True
            print(f"  {payload['meta']['total']} lines "
                  f"({len(entry.payload_gz) / 1e6:.1f} MB compressed).", flush=True)
        except BaseException as exc:  # incl. SystemExit from edfapi loading
            import traceback
            global LAST_ERROR
            entry.error = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__))
            LAST_ERROR = f"{entry.name}: {entry.error.strip().splitlines()[-1]}"
            # Recorded once; the browser is shown the message instead of the
            # request being retried forever.
            print(f"\nERROR parsing {entry.name}:\n{entry.error}", flush=True)
        finally:
            entry.parsing = False






class Registry:
    """Open files, keyed by a stable id so tabs can be added/closed at runtime."""

    def __init__(self):
        self._by_id = {}
        self._order = []
        self._next = 1
        self._lock = threading.Lock()

    def add(self, path):
        path = os.path.abspath(path)
        with self._lock:
            for fid in self._order:                 # already open -> reuse
                if self._by_id[fid].path == path:
                    return fid
            fid = self._next
            self._next += 1
            self._by_id[fid] = FileEntry(path)
            self._order.append(fid)
            return fid

    def close(self, fid):
        with self._lock:
            if fid in self._by_id:
                del self._by_id[fid]                # frees the parsed data
                self._order.remove(fid)
                return True
            return False

    def clear(self):
        """Close every open file (starting a watch begins a fresh session)."""
        with self._lock:
            self._by_id.clear()
            self._order = []

    def get(self, fid):
        return self._by_id.get(fid)

    def entries(self):
        with self._lock:
            return [self._by_id[i] for i in self._order]

    def listing(self):
        with self._lock:
            return [{"id": i, "name": self._by_id[i].name,
                     "path": self._by_id[i].path} for i in self._order]



def _is_edf(name):
    return name.lower().endswith(".edf")



def _drives():
    """Windows drive roots; empty elsewhere."""
    if os.name != "nt":
        return []
    out = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if os.path.exists(root):
            out.append(root)
    return out



def browse_dir(path):
    """List sub-directories and .EDF files of ``path`` for the in-app browser."""
    if not path:
        path = os.path.expanduser("~")
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        path = os.path.expanduser("~")
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        dirs.append(e.name)
                    elif e.is_file() and _is_edf(e.name):
                        files.append({"name": e.name,
                                      "size": e.stat().st_size})
                except OSError:
                    continue
    except PermissionError:
        return {"path": path, "error": "Permission denied",
                "dirs": [], "files": [], "parent": os.path.dirname(path),
                "drives": _drives(), "sep": os.sep}
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    parent = os.path.dirname(path)
    return {"path": path, "parent": None if parent == path else parent,
            "dirs": dirs, "files": files, "drives": _drives(), "sep": os.sep,
            "home": os.path.expanduser("~")}



def list_edfs(folder, recursive=False):
    """Absolute paths of .EDF files in a folder (optionally recursing)."""
    out = []
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return out
    try:
        if recursive:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for n in files:
                    if _is_edf(n):
                        out.append(os.path.join(root, n))
        else:
            with os.scandir(folder) as it:
                for e in it:
                    if e.is_file() and _is_edf(e.name):
                        out.append(os.path.join(folder, e.name))
    except OSError:
        pass
    out.sort(key=str.lower)
    return out



class Watcher:
    """Polls one folder for new .EDF files and adds them to the registry.

    edfapi/eyelinkio give no file-change events, and portable OS watch APIs vary,
    so a simple periodic scan is the robust choice.  A brief size-stability check
    avoids opening a recording that is still being written.
    """

    def __init__(self, reg):
        self.reg = reg
        self.folder = None
        self.recursive = False
        self._known = {}          # path -> last seen size
        self._pending = {}        # path -> size, waiting to stabilise
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.last_added = []      # paths added on the most recent scan

    def status(self):
        with self._lock:
            return {"watching": bool(self.folder), "folder": self.folder,
                    "recursive": self.recursive, "known": len(self._known)}

    POLL_SECONDS = 2.0      # how often the folder is re-scanned
    _TICK = 0.5             # ...checked in slices so a stop is honoured quickly

    def start(self, folder, recursive):
        # Always tear the previous run down first: a restart that reused a
        # still-winding-down thread would leave the watcher marked as running
        # with nothing actually polling.
        self._halt()
        folder = os.path.abspath(folder)
        # Work out what is already there *before* returning, so a recording that
        # lands moments after watching begins is treated as new rather than
        # being swallowed into the baseline by a still-running seed pass.
        known = {}
        for p in list_edfs(folder, bool(recursive)):
            try:
                known[p] = os.path.getsize(p)
            except OSError:
                known[p] = 0
        with self._lock:
            self.folder = folder
            self.recursive = bool(recursive)
            self._known = known
            self._pending = {}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._halt()

    def _halt(self):
        """Ask the polling thread to finish, and wait until it has."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._thread = None
        with self._lock:
            self.folder = None

    def _run(self):
        # the baseline was captured synchronously in start(), so go straight to
        # polling for anything that appears afterwards
        while True:
            # sleep in short slices so a stop request is picked up quickly,
            # while still only re-scanning once every POLL_SECONDS
            waited = 0.0
            while waited < self.POLL_SECONDS:
                if self._stop.wait(self._TICK):
                    return
                waited += self._TICK
            with self._lock:
                folder, recursive = self.folder, self.recursive
            if not folder:
                break
            try:
                current = list_edfs(folder, recursive)
            except OSError:
                continue
            for p in current:
                if p in self._known:
                    continue
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                # wait one cycle for the size to settle (still-recording guard)
                if self._pending.get(p) == sz and sz > 0:
                    self.reg.add(p)
                    self._known[p] = sz
                    self._pending.pop(p, None)
                    print(f"[watch] opened new file: {os.path.basename(p)}",
                          flush=True)
                else:
                    self._pending[p] = sz