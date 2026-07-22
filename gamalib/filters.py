"""Row filtering shared by the browser UI and the command line."""

import re

from .dataset import I_GRP, I_MK, I_START, I_DUR, I_EYE, I_RAW



# ---------------------------------------------------------------------------
# Filtering (shared with the in-browser logic) for head-less + bulk use.
# ---------------------------------------------------------------------------
def _matcher(pattern, regex):
    if not pattern:
        return None
    if regex:
        try:
            r = re.compile(pattern, re.I)
            return lambda s: bool(r.search(s))
        except re.error:
            return None
    q = pattern.lower()
    return lambda s: q in s.lower()



def filter_indices(rows, parsed, opts):
    groups = opts.get("groups")
    kinds = opts.get("kinds")
    eye = opts.get("eye")
    tmin, tmax = opts.get("tmin"), opts.get("tmax")
    dfix, dsacc = opts.get("min_fix"), opts.get("min_sacc")
    inc = _matcher(opts.get("contains"), opts.get("contains_regex"))
    exc = _matcher(opts.get("exclude"), opts.get("contains_regex"))
    srch = _matcher(opts.get("search"), opts.get("search_regex"))
    out = []
    for i, r in enumerate(rows):
        g = r[I_GRP]
        if groups is not None and g not in groups:
            continue
        if g == "MSG" and kinds is not None and (r[I_MK] or "experiment") not in kinds:
            continue
        if eye and r[I_EYE] and r[I_EYE] != eye:
            continue
        t = r[I_START]
        if tmin is not None and isinstance(t, int) and t < tmin:
            continue
        if tmax is not None and isinstance(t, int) and t > tmax:
            continue
        if g == "FIX" and dfix is not None and isinstance(r[I_DUR], int) and r[I_DUR] < dfix:
            continue
        if g == "SACC" and dsacc is not None and isinstance(r[I_DUR], int) and r[I_DUR] < dsacc:
            continue
        if g == "MSG":
            body = parsed[i]["msg"] or ""
            if inc and not inc(body):
                continue
            if exc and exc(body):
                continue
        if srch and not srch(r[I_RAW]):
            continue
        out.append(i)
    return out
