"""Turning converted records into the row/column data the app works with."""

import os

from .convert import build_records


GROUP_ALL = ["PREAMBLE", "HEADER", "END", "INPUT", "MSG",
             "FIX", "SACC", "BLINK"]


CAT_GROUP = {
    "converted_from": "PREAMBLE", "preamble": "PREAMBLE",
    "preamble_end": "PREAMBLE", "blank": "PREAMBLE",
    "start": "HEADER", "prescaler": "HEADER", "vprescaler": "HEADER",
    "pupil": "HEADER", "events": "HEADER",
    "end": "END", "input": "INPUT", "msg": "MSG",
    "sfix": "FIX", "efix": "FIX",
    "ssacc": "SACC", "esacc": "SACC",
    "sblink": "BLINK", "eblink": "BLINK",
}

CAT_LABEL = {
    "converted_from": "CONVERTED", "preamble": "PREAMBLE",
    "preamble_end": "SEP", "blank": "BLANK",
    "start": "START", "prescaler": "PRESCALER", "vprescaler": "VPRESCALER",
    "pupil": "PUPIL", "events": "EVENTS",
    "end": "END", "input": "INPUT", "msg": "MSG",
    "sfix": "SFIX", "efix": "EFIX", "ssacc": "SSACC", "esacc": "ESACC",
    "sblink": "SBLINK", "eblink": "EBLINK",
}


WIRE_COLUMNS = ["idx", "cat", "grp", "mkind", "start", "end", "dur", "eye",
                "x1", "y1", "x2", "y2", "amp", "vel", "pupil", "resx", "resy",
                "val", "raw", "block"]

I_IDX, I_CAT, I_GRP, I_MK, I_START, I_END, I_DUR, I_EYE = 0, 1, 2, 3, 4, 5, 6, 7
I_RAW = 18


EXPORT_COLUMNS = ["idx", "category", "group", "msg_kind", "start", "end",
                  "dur", "eye", "x1", "y1", "x2", "y2", "amp", "pvel", "pupil",
                  "res_x", "res_y", "input", "message"]



def _parse(rec):
    """Derive spreadsheet columns from one record (dict from build_records)."""
    cat = rec["cat"]
    text = rec["text"]
    d = {k: "" for k in ("start", "end", "dur", "eye", "x1", "y1", "x2", "y2",
                         "amp", "vel", "pupil", "resx", "resy", "val", "msg")}
    if rec["time"] is not None:
        d["start"] = rec["time"]

    if cat == "efix":
        f = text.split("\t"); p = f[0].split()
        d["eye"], d["start"], d["end"], d["dur"] = p[1], int(p[2]), int(f[1]), int(f[2])
        d["x1"], d["y1"], d["pupil"] = f[3].strip(), f[4].strip(), f[5].strip()
    elif cat == "esacc":
        f = text.split("\t"); p = f[0].split()
        d["eye"], d["start"], d["end"], d["dur"] = p[1], int(p[2]), int(f[1]), int(f[2])
        d["x1"], d["y1"] = f[3].strip(), f[4].strip()
        d["x2"], d["y2"] = f[5].strip(), f[6].strip()
        d["amp"], d["vel"] = f[7].strip(), f[8].strip()
    elif cat in ("sfix", "ssacc", "sblink"):
        p = text.split()
        d["eye"], d["start"] = p[1], int(p[2])
    elif cat == "eblink":
        f = text.split("\t"); p = f[0].split()
        d["eye"], d["start"], d["end"], d["dur"] = p[1], int(p[2]), int(f[1]), int(f[2])
    elif cat == "start":
        f = text.split("\t")
        d["start"], d["eye"] = int(f[1].strip()), f[2]
    elif cat == "events":
        f = text.split("\t")
        d["eye"] = f[2] if len(f) > 2 else ""
        d["msg"] = text.replace("\t", " ")
    elif cat in ("prescaler", "vprescaler", "pupil"):
        d["msg"] = text.replace("\t", " ")
    elif cat == "end":
        f = text.split("\t")
        d["start"], d["resx"], d["resy"] = int(f[1].strip()), f[4].strip(), f[5].strip()
    elif cat == "input":
        f = text.split("\t")
        d["start"], d["val"] = int(f[1]), f[2]
    elif cat == "msg":
        d["msg"] = rec["msg"] or ""
    else:  # converted_from / preamble / preamble_end / blank
        d["msg"] = text
    return d



def build_dataset(edf_path, converted_from_line, progress=None):
    """Parse the EDF and return (records, wire_payload, parsed_rows)."""
    records, _blocks = build_records(edf_path, converted_from_line, progress)
    rows, parsed, times = [], [], []
    group_counts, kind_counts = {}, {}
    blocks = {}                        # block index -> summary accumulator
    for idx, rec in enumerate(records):
        cat = rec["cat"]
        grp = CAT_GROUP[cat]
        d = _parse(rec)
        parsed.append(d)
        group_counts[grp] = group_counts.get(grp, 0) + 1
        if grp == "MSG":
            mk = rec["mkind"] or "experiment"
            kind_counts[mk] = kind_counts.get(mk, 0) + 1
        if isinstance(d["start"], int):
            times.append(d["start"])
        blk = rec["block"]
        rows.append([
            idx, CAT_LABEL[cat], grp, rec["mkind"] or "",
            d["start"], d["end"], d["dur"], d["eye"],
            d["x1"], d["y1"], d["x2"], d["y2"], d["amp"], d["vel"],
            d["pupil"], d["resx"], d["resy"], d["val"], rec["text"],
            blk if blk is not None else -1,
        ])
        if blk is not None:
            b = blocks.get(blk)
            if b is None:
                b = blocks[blk] = {"block": blk, "first": idx, "last": idx,
                                   "tmin": None, "tmax": None, "n": 0,
                                   "counts": {}}
            b["last"] = idx
            b["n"] += 1
            b["counts"][grp] = b["counts"].get(grp, 0) + 1
            if isinstance(d["start"], int):
                b["tmin"] = d["start"] if b["tmin"] is None else min(b["tmin"], d["start"])
                t2 = d["end"] if isinstance(d["end"], int) else d["start"]
                b["tmax"] = t2 if b["tmax"] is None else max(b["tmax"], t2)
    meta = {
        "filename": os.path.basename(edf_path),
        "total": len(records),
        "tmin": min(times) if times else 0,
        "tmax": max(times) if times else 0,
        "groups": GROUP_ALL,
        "group_counts": group_counts,
        "msg_kinds": ["experiment", "config", "cal", "draw"],
        "kind_counts": kind_counts,
        "blocks": [blocks[k] for k in sorted(blocks)],
    }
    return records, {"columns": WIRE_COLUMNS, "rows": rows, "meta": meta}, parsed
