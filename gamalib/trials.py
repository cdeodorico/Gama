"""Trial segmentation and area-of-interest matching.

Nothing here is tied to one lab's message conventions: a "scheme"
describes which messages open and close a trial, which carry variables and
which place stimuli, plus how to pull fields out of each.
"""

import re

from .dataset import I_CAT, I_GRP, I_MK, I_START, I_DUR



# ---------------------------------------------------------------------------
# Trial segmentation and area-of-interest (AOI) analysis.
#
# Everybody labels their experiment messages differently, so nothing here is
# hard-coded to one convention.  A "scheme" describes which messages open and
# close a trial, which messages carry variables, and which messages place the
# stimuli, together with how to pull fields out of each.  Three parsing modes
# cover the common cases:
#
#   kv         key=value pairs.  With ``greedy`` (the default) a value runs to
#              the next ``key=``, so "type=Relational Distractor" survives
#              intact; without it, values are single tokens and anything else
#              becomes a positional field (_0, _1, ...).
#   positional whitespace-separated tokens addressed by index ("0", "1", ...)
#   regex      a regular expression; named groups become fields.
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r'(?:(?<=\s)|^)([A-Za-z_][\w.\-]*)=')



def _isnum(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False



def _fnum(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default



def parse_msg_fields(text, mode="kv", greedy=True, regex=None):
    """Parse a message body into an ordered {field: value} mapping."""
    text = (text or "").strip()
    if not text:
        return {}
    if mode == "regex":
        if not regex:
            return {}
        try:
            m = re.search(regex, text)
        except re.error:
            return {}
        if not m:
            return {}
        out = {k: (v if v is not None else "") for k, v in m.groupdict().items()}
        for i, g in enumerate(m.groups()):
            out.setdefault(str(i + 1), g if g is not None else "")
        return out
    if mode == "positional":
        return {str(i): t for i, t in enumerate(text.split())}

    out = {}
    if greedy:
        keys = list(_KEY_RE.finditer(text))
        if keys:
            for i, t in enumerate(text[:keys[0].start()].split()):
                out["_%d" % i] = t
            for n, m in enumerate(keys):
                end = keys[n + 1].start() if n + 1 < len(keys) else len(text)
                out[m.group(1)] = text[m.end():end].strip()
            return out
    bare = 0
    for tok in text.split():
        k, sep, v = tok.partition("=")
        if sep and k and _KEY_RE.fullmatch(k + "="):
            out[k] = v
        else:
            out["_%d" % bare] = tok
            bare += 1
    return out



def _spec_fields(spec, body):
    """Fields for one message body, using that spec's parsing options."""
    return parse_msg_fields(body,
                            (spec or {}).get("mode", "kv"),
                            bool((spec or {}).get("greedy", True)),
                            (spec or {}).get("regex"))



def _make_msg_matcher(spec):
    """A predicate over the message body, or None if the spec is blank."""
    pat = ((spec or {}).get("match") or "").strip()
    if not pat:
        return None
    how = (spec or {}).get("match_mode", "prefix")
    if how == "regex":
        try:
            r = re.compile(pat)
        except re.error:
            return None
        return lambda b: bool(r.search(b))
    if how == "contains":
        low = pat.lower()
        return lambda b: low in b.lower()
    if how == "exact":
        return lambda b: b.strip() == pat
    return lambda b: b.startswith(pat)



def _body_after(body, spec):
    """Drop the marker itself so only the trailing payload is parsed."""
    pat = ((spec or {}).get("match") or "").strip()
    how = (spec or {}).get("match_mode", "prefix")
    if pat and how == "prefix" and body.startswith(pat):
        return body[len(pat):].strip()
    return body



# ---------------------------------------------------------------------------
# Suggestions: what do the messages in this recording look like?
# ---------------------------------------------------------------------------
# Hints are ranked: an earlier entry is a stronger signal than a later one, so
# TRIAL_END beats TRIAL_RESULT for the trial-end role even though both look
# plausible, and DISPLAY_ONSET beats fix_onset for the start of an inner window.
_START_HINTS = ("trial_start", "trialstart", "start_trial", "trialid",
                "start", "begin")

_END_HINTS = ("trial_end", "trialend", "end_trial", "trial_result",
              "end", "stop", "result")

_WIN_FROM_HINTS = ("display_onset", "stim_onset", "stimulus_onset", "display",
                   "stim", "onset")

_WIN_TO_HINTS = ("response", "resp", "keypress", "button", "key", "answer")

_AOI_HINTS = ("stim_pos", "stim", "iarea", "aoi", "pos", "target", "obj")



def _hint_score(name, hints):
    low = name.lower()
    for i, h in enumerate(hints):
        if h in low:
            return 100 - i * 6
    return 0



def suggest_markers(rows, parsed, top=40):
    """Rank experiment-message prefixes as candidate trial / AOI markers."""
    from collections import Counter, defaultdict
    counts = Counter()
    examples = {}
    times = defaultdict(list)
    for i, r in enumerate(rows):
        if r[I_GRP] != "MSG" or (r[I_MK] or "experiment") != "experiment":
            continue
        body = (parsed[i]["msg"] or "").strip()
        if not body:
            continue
        tok = body.split()[0]
        counts[tok] += 1
        examples.setdefault(tok, body)
        t = r[I_START]
        if isinstance(t, int):
            times[tok].append(t)

    cands = []
    for tok, n in counts.most_common(top):
        ex = examples[tok]
        payload = ex[len(tok):].strip()
        greedy = _spec_fields({"greedy": True}, payload)
        strict = _spec_fields({"greedy": False}, payload)
        ts = times[tok]
        reg = None
        if len(ts) > 3:
            gaps = [b - a for a, b in zip(ts, ts[1:]) if b >= a]
            if gaps:
                mean = sum(gaps) / len(gaps)
                if mean:
                    var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
                    reg = round((var ** 0.5) / mean, 3)
        numeric = [k for k, v in strict.items() if _isnum(v)]
        cands.append({
            "name": tok, "count": n, "example": ex,
            "fields": list(greedy.keys()),
            "fields_strict": list(strict.keys()),
            "numeric": numeric,
            "regularity": reg,
        })

    def pick(hints, extra=None, exclude=()):
        best, score = None, 0
        for c in cands:
            if c["name"] in exclude:
                continue
            s = _hint_score(c["name"], hints)
            if c["regularity"] is not None and c["regularity"] < 0.6:
                s += 5
            s += min(c["count"], 5000) / 1000.0
            if extra:
                s += extra(c)
            if s > score:
                best, score = c["name"], s
        return best

    def aoi_bonus(c):
        s = 0
        low = [f.lower() for f in c["fields_strict"]]
        if "x" in low and "y" in low:
            s += 60
        if len(c["numeric"]) >= 2:
            s += 10
        return s

    start = pick(_START_HINTS)
    end = pick(_END_HINTS, exclude=(start,) if start else ())
    wfrom = pick(_WIN_FROM_HINTS, exclude=tuple(x for x in (start, end) if x))
    wto = pick(_WIN_TO_HINTS, exclude=tuple(x for x in (start, end, wfrom) if x))
    roles = {
        "start": start, "end": end,
        "aoi": pick(_AOI_HINTS, aoi_bonus),
        "window_from": wfrom, "window_to": wto,
    }
    return {"candidates": cands, "roles": roles}



# ---------------------------------------------------------------------------
# Hit testing
# ---------------------------------------------------------------------------
def _aoi_hit(aois, x, y, shape):
    """Label of the AOI at (x, y), or None."""
    if x is None or y is None or not aois:
        return None
    typ = (shape or {}).get("type", "circle")
    if typ == "nearest":
        maxd = _fnum((shape or {}).get("max_distance"), 0) or 0
        best, bd = None, None
        for a in aois:
            d = ((a["x"] - x) ** 2 + (a["y"] - y) ** 2) ** 0.5
            if bd is None or d < bd:
                best, bd = a, d
        if best is not None and (maxd <= 0 or bd <= maxd):
            return best["label"]
        return None
    if typ == "rect":
        w = _fnum((shape or {}).get("w"), 100) or 100
        h = _fnum((shape or {}).get("h"), 100) or 100
        for a in aois:
            aw = a.get("w") or w
            ah = a.get("h") or h
            if abs(a["x"] - x) <= aw / 2.0 and abs(a["y"] - y) <= ah / 2.0:
                return a["label"]
        return None
    if typ == "fields":
        rdef = _fnum((shape or {}).get("radius"), 100) or 100
        for a in aois:
            aw, ah = a.get("w"), a.get("h")
            if aw and ah:
                if abs(a["x"] - x) <= aw / 2.0 and abs(a["y"] - y) <= ah / 2.0:
                    return a["label"]
            else:
                rr = a.get("r") or rdef
                if ((a["x"] - x) ** 2 + (a["y"] - y) ** 2) ** 0.5 <= rr:
                    return a["label"]
        return None
    # circle (default): closest AOI whose radius contains the point
    rdef = _fnum((shape or {}).get("radius"), 100) or 100
    best, bd = None, None
    for a in aois:
        rr = a.get("r") or rdef
        d = ((a["x"] - x) ** 2 + (a["y"] - y) ** 2) ** 0.5
        if d <= rr and (bd is None or d < bd):
            best, bd = a, d
    return best["label"] if best else None



# ---------------------------------------------------------------------------
# The analysis itself
# ---------------------------------------------------------------------------
def analyse_trials(rows, parsed, spec, limit=None):
    """Segment trials, place AOIs, and match fixations / saccades to them.

    Returns per-row assignments (for the table) and a per-trial summary.
    """
    spec = spec or {}
    s_start, s_end = spec.get("start") or {}, spec.get("end") or {}
    s_aoi = spec.get("aoi") or {}
    win = spec.get("window") or {}
    var_specs = spec.get("vars") or []

    m_start = _make_msg_matcher(s_start)
    m_end = _make_msg_matcher(s_end)
    m_aoi = _make_msg_matcher(s_aoi)
    win_on = bool(win.get("enabled"))
    m_wfrom = _make_msg_matcher(win.get("from")) if win_on else None
    m_wto = _make_msg_matcher(win.get("to")) if win_on else None
    w_off_start = _fnum(win.get("offset_start"), 0) or 0
    w_off_end = _fnum(win.get("offset_end"), 0) or 0
    var_matchers = [(_make_msg_matcher(v), v) for v in var_specs]

    fx = (s_aoi.get("x") or "x")
    fy = (s_aoi.get("y") or "y")
    flabel = (s_aoi.get("label") or "")
    fw, fh, fr = s_aoi.get("w"), s_aoi.get("h"), s_aoi.get("r")
    shape = s_aoi.get("shape") or {"type": "circle", "radius": 100}

    warnings = []
    if not m_start:
        return {"error": "No trial-start message defined."}

    trials = []
    cur = None
    n_rows = len(rows)

    def close(cur, end_i, end_time, reason):
        cur["end_i"] = end_i
        cur["t_end"] = end_time
        cur["closed_by"] = reason
        trials.append(cur)

    for i in range(n_rows):
        r = rows[i]
        if r[I_GRP] != "MSG":
            continue
        body = (parsed[i]["msg"] or "").strip()
        if not body:
            continue
        t = r[I_START] if isinstance(r[I_START], int) else None

        if m_start and m_start(body):
            if cur is not None:                      # unterminated previous trial
                close(cur, i - 1, cur.get("t_last", cur["t_start"]), "next-start")
                warnings.append("trial %d had no end marker" % (len(trials)))
            cur = {"n": len(trials) + 1, "start_i": i, "t_start": t, "t_last": t,
                   "vars": dict(_spec_fields(s_start, _body_after(body, s_start))),
                   "aois": [], "w_from": None, "w_to": None}
            if limit and len(trials) >= limit:
                cur = None
                break
            continue

        if cur is None:
            continue
        if t is not None:
            cur["t_last"] = t

        if m_end and m_end(body):
            cur["vars"].update(_spec_fields(s_end, _body_after(body, s_end)))
            close(cur, i, t, "end-marker")
            cur = None
            continue

        for vm, vs in var_matchers:
            if vm and vm(body):
                pre = vs.get("prefix") or ""
                for k, v in _spec_fields(vs, _body_after(body, vs)).items():
                    cur["vars"][pre + k] = v

        if m_wfrom and cur["w_from"] is None and m_wfrom(body):
            cur["w_from"] = t
        if m_wto and cur["w_to"] is None and m_wto(body):
            cur["w_to"] = t

        if m_aoi and m_aoi(body):
            f = _spec_fields(s_aoi, _body_after(body, s_aoi))
            x, y = _fnum(f.get(fx)), _fnum(f.get(fy))
            if x is None or y is None:
                continue
            lab = f.get(flabel) if flabel else None
            if not lab:
                lab = "aoi%d" % (len(cur["aois"]) + 1)
            cur["aois"].append({
                "x": x, "y": y, "label": str(lab),
                "w": _fnum(f.get(fw)) if fw else None,
                "h": _fnum(f.get(fh)) if fh else None,
                "r": _fnum(f.get(fr)) if fr else None,
                "fields": f,
            })

    if cur is not None:
        close(cur, n_rows - 1, cur.get("t_last"), "eof")
        warnings.append("last trial had no end marker")

    if limit:
        trials = trials[:limit]

    # ---- match events to AOIs ------------------------------------------
    row_trial = [-1] * n_rows
    row_aoi = [""] * n_rows
    row_aoi_from = [""] * n_rows
    labels_seen = {}

    for tr in trials:
        lo = tr["w_from"] if (win_on and tr["w_from"] is not None) else tr["t_start"]
        hi = tr["w_to"] if (win_on and tr["w_to"] is not None) else tr["t_end"]
        if lo is not None:
            lo += w_off_start
        if hi is not None:
            hi += w_off_end
        tr["win_start"], tr["win_end"] = lo, hi
        aois = tr["aois"]
        fixes, saccs = [], []
        for i in range(tr["start_i"], min(tr["end_i"], n_rows - 1) + 1):
            r = rows[i]
            grp = r[I_GRP]
            if grp not in ("FIX", "SACC"):
                continue
            t = r[I_START]
            if not isinstance(t, int):
                continue
            row_trial[i] = tr["n"]
            if lo is not None and t < lo:
                continue
            if hi is not None and t > hi:
                continue
            cat = r[I_CAT]
            if cat == "EFIX":
                x, y = _fnum(r[8]), _fnum(r[9])
                lab = _aoi_hit(aois, x, y, shape)
                row_aoi[i] = lab or ""
                if lab:
                    labels_seen[lab] = labels_seen.get(lab, 0) + 1
                fixes.append({"t": t, "dur": r[I_DUR] if isinstance(r[I_DUR], int) else 0,
                              "aoi": lab, "x": x, "y": y})
            elif cat == "ESACC":
                x1, y1 = _fnum(r[8]), _fnum(r[9])
                x2, y2 = _fnum(r[10]), _fnum(r[11])
                a_from = _aoi_hit(aois, x1, y1, shape)
                a_to = _aoi_hit(aois, x2, y2, shape)
                row_aoi[i] = a_to or ""
                row_aoi_from[i] = a_from or ""
                saccs.append({"t": t, "aoi": a_to, "from": a_from,
                              "amp": _fnum(r[12]), "vel": _fnum(r[13])})
        tr["_fixes"], tr["_saccs"] = fixes, saccs

    # ---- per-trial summary ---------------------------------------------
    var_names = []
    for tr in trials:
        for k in tr["vars"]:
            if k not in var_names:
                var_names.append(k)
    dwell_labels = sorted(labels_seen)
    use_dwell = 0 < len(dwell_labels) <= 12

    cols = (["trial", "t_start", "t_end", "duration"]
            + (["win_start", "win_end"] if win_on else [])
            + var_names
            + ["n_aoi", "n_fix", "n_sacc",
               "first_fix_aoi", "first_fix_latency", "first_fix_rank",
               "first_sacc_aoi", "first_sacc_from", "first_sacc_latency",
               "first_sacc_amp", "first_sacc_rank"])
    if use_dwell:
        cols += ["dwell_" + l for l in dwell_labels]
        cols += ["nfix_" + l for l in dwell_labels]

    srows = []
    for tr in trials:
        base = tr["win_start"] if tr.get("win_start") is not None else tr["t_start"]
        fixes, saccs = tr["_fixes"], tr["_saccs"]
        # "First" means the first event that actually landed in an AOI: events
        # that hit nothing (the initial central fixation, a stray saccade) are
        # skipped rather than reported as the first.  The rank says how many
        # in-window events were passed over, which is a useful sanity check.
        f0 = f0rank = None
        for k, f in enumerate(fixes):
            if f["aoi"]:
                f0, f0rank = f, k + 1
                break
        s0 = s0rank = None
        for k, sc in enumerate(saccs):
            if sc["aoi"]:
                s0, s0rank = sc, k + 1
                break
        dur = (tr["t_end"] - tr["t_start"]
               if isinstance(tr["t_end"], int) and isinstance(tr["t_start"], int) else "")
        row = [tr["n"], tr["t_start"], tr["t_end"], dur]
        if win_on:
            row += [tr.get("win_start"), tr.get("win_end")]
        row += [tr["vars"].get(k, "") for k in var_names]
        row += [
            len(tr["aois"]), len(fixes), len(saccs),
            (f0 or {}).get("aoi") or "",
            (f0["t"] - base) if (f0 and base is not None) else "",
            f0rank if f0rank else "",
            (s0 or {}).get("aoi") or "",
            (s0 or {}).get("from") or "",
            (s0["t"] - base) if (s0 and base is not None) else "",
            (s0 or {}).get("amp") if s0 else "",
            s0rank if s0rank else "",
        ]
        if use_dwell:
            dw = {l: 0 for l in dwell_labels}
            nf = {l: 0 for l in dwell_labels}
            for f in fixes:
                if f["aoi"] in dw:
                    dw[f["aoi"]] += f["dur"] or 0
                    nf[f["aoi"]] += 1
            row += [dw[l] for l in dwell_labels]
            row += [nf[l] for l in dwell_labels]
        srows.append(row)

    for tr in trials:
        tr.pop("_fixes", None)
        tr.pop("_saccs", None)

    return {
        "n_trials": len(trials),
        "row_trial": row_trial,
        "row_aoi": row_aoi,
        "row_aoi_from": row_aoi_from,
        "summary": {"columns": cols, "rows": srows},
        "warnings": warnings[:20],
        "labels": dwell_labels,
        "matched_fix": sum(1 for v in row_aoi if v),
        "trials_preview": [
            {"n": t["n"], "t_start": t["t_start"], "t_end": t["t_end"],
             "vars": t["vars"], "n_aoi": len(t["aois"]),
             "aois": t["aois"][:12], "win": [t.get("win_start"), t.get("win_end")]}
            for t in trials[:8]
        ],
    }
