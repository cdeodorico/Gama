"""Checking GitHub for a newer release.

This is the only part of gama that touches the network, so it is deliberately
timid: it runs on a background thread, never blocks startup, gives up quickly,
swallows every error, and remembers the answer for a day so a normal session
makes at most one request.  It never downloads or installs anything -- it only
tells you a newer version exists and where to get it.

Both ways of running gama are catered for: a frozen build is pointed at the
matching release asset, a source checkout is told to pull.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from .version import __version__

GITHUB_REPO = "cdeodorico/Gama"
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
RELEASES_PAGE = "https://github.com/%s/releases" % GITHUB_REPO

CHECK_INTERVAL = 24 * 60 * 60      # at most one request a day
TIMEOUT = 5.0                      # seconds; the app must not wait on this

_LOCK = threading.Lock()
_STATE = None                      # last known status dict
_STATE_PATH = None


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
def parse_version(text):
    """'v1.2.3-beta' -> (1, 2, 3).  Anything unparseable sorts as (0, 0, 0)."""
    s = (text or "").strip().lstrip("vV")
    s = re.split(r"[-+ ]", s)[0]
    parts = []
    for chunk in s.split("."):
        m = re.match(r"\d+", chunk)
        parts.append(int(m.group(0)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(latest, current):
    """True if ``latest`` is a higher version than ``current``."""
    if not latest:
        return False
    return parse_version(latest) > parse_version(current)


# ---------------------------------------------------------------------------
# Talking to GitHub
# ---------------------------------------------------------------------------
def _pick_asset(assets):
    """The release asset that suits this platform, if the release has one."""
    if sys.platform.startswith("win"):
        wanted, words = (".exe", ".msi", ".zip"), ("win", "windows")
    elif sys.platform == "darwin":
        wanted, words = (".dmg", ".pkg", ".zip"), ("mac", "osx", "darwin")
    else:
        wanted, words = (".appimage", ".tar.gz", ".zip"), ("linux",)

    def wrap(a):
        return {"name": a["name"],
                "url": a.get("browser_download_url") or RELEASES_PAGE,
                "size": a.get("size")}

    for ext in wanted:                       # a matching file type wins
        for a in assets:
            if a.get("name", "").lower().endswith(ext):
                return wrap(a)
    for a in assets:                         # else something named for this OS
        if any(w in a.get("name", "").lower() for w in words):
            return wrap(a)
    return None


def fetch_latest(timeout=TIMEOUT):
    """Ask GitHub for the newest release.

    Returns a dict on success, or one with an ``error`` key.  Never raises.
    """
    req = urllib.request.Request(API_LATEST, headers={
        # GitHub rejects requests without a User-Agent
        "User-Agent": "gama/%s (+%s)" % (__version__, RELEASES_PAGE),
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # the repo simply has no releases yet - not an error worth showing
            return {"latest": None, "error": None, "no_releases": True}
        if e.code in (403, 429):
            return {"error": "GitHub rate limit reached; try again later"}
        return {"error": "GitHub returned HTTP %s" % e.code}
    except urllib.error.URLError as e:
        return {"error": "no connection (%s)" % (getattr(e, "reason", e),)}
    except Exception as e:                                  # noqa: BLE001
        return {"error": "check failed (%s)" % type(e).__name__}

    tag = data.get("tag_name") or data.get("name") or ""
    return {
        "latest": tag.lstrip("vV") or None,
        "tag": tag,
        "url": data.get("html_url") or RELEASES_PAGE,
        "published": data.get("published_at"),
        "notes": (data.get("body") or "")[:400],
        "asset": _pick_asset(data.get("assets") or []),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Persisted state (the answer, and whether the user wants checks at all)
# ---------------------------------------------------------------------------
def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def _save_state(path, state):
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass                       # a read-only install just goes uncached


def configure(state_dir):
    """Point the checker at a writable folder for its cache."""
    global _STATE_PATH, _STATE
    _STATE_PATH = os.path.join(state_dir, "update-check.json")
    with _LOCK:
        _STATE = _load_state(_STATE_PATH)


def enabled():
    with _LOCK:
        return bool((_STATE or {}).get("enabled", True))


def set_enabled(on):
    global _STATE
    with _LOCK:
        _STATE = dict(_STATE or {})
        _STATE["enabled"] = bool(on)
        _save_state(_STATE_PATH, _STATE)
    return enabled()


def _install_hint():
    """How this particular copy of gama should be updated."""
    if getattr(sys, "frozen", False):
        return ("frozen",
                "Download the new build from the release page and replace "
                "your gama executable. Your presets, schemes and notes are "
                "kept outside it, so nothing is lost.")
    return ("source",
            "Update your checkout: git pull  (or download the source zip). "
            "Remember to keep gama.py, gamalib/ and index.html together.")


def status():
    """The last known answer, without touching the network."""
    with _LOCK:
        st = dict(_STATE or {})
    how, hint = _install_hint()
    latest = st.get("latest")
    return {
        "enabled": bool(st.get("enabled", True)),
        "current": __version__,
        "latest": latest,
        "update_available": is_newer(latest, __version__),
        "url": st.get("url") or RELEASES_PAGE,
        "asset": st.get("asset"),
        "notes": st.get("notes"),
        "checked": st.get("checked"),
        "error": st.get("error"),
        "no_releases": bool(st.get("no_releases")),
        "repo": GITHUB_REPO,
        "releases_page": RELEASES_PAGE,
        "how": how,
        "hint": hint,
    }


def check(force=False, timeout=TIMEOUT):
    """Check now if it is due (or forced).  Returns the same shape as status()."""
    global _STATE
    with _LOCK:
        st = dict(_STATE or {})
    if not force:
        if not st.get("enabled", True):
            return status()
        last = st.get("checked") or 0
        if time.time() - last < CHECK_INTERVAL:
            return status()                       # answered from cache

    res = fetch_latest(timeout)
    with _LOCK:
        _STATE = dict(_STATE or {})
        if res.get("error"):
            # keep whatever we knew before; just record why this attempt failed
            _STATE["error"] = res["error"]
        else:
            _STATE.update({k: res.get(k) for k in
                           ("latest", "tag", "url", "published", "notes",
                            "asset", "no_releases")})
            _STATE["error"] = None
            _STATE["checked"] = int(time.time())
        _save_state(_STATE_PATH, _STATE)
    return status()


def start_background(state_dir):
    """Kick off a check on a daemon thread; startup never waits for it."""
    configure(state_dir)
    if not enabled():
        return
    t = threading.Thread(target=lambda: check(False), daemon=True,
                         name="gama-update-check")
    t.start()
    return t