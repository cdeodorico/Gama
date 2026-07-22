"""Writing the current view out as ASC, CSV, TSV or HTML."""

import json
from io import StringIO

from .version import __version__
from .edfapi import edfapi_version
from .convert import records_to_bytes
from .dataset import EXPORT_COLUMNS, GROUP_ALL, I_CAT, I_GRP, I_MK



def build_provenance(entry, fmt, opts_json, relative, n_rows):
    """A small JSON recording how an export was produced, for reproducibility.

    Written only for CSV/TSV/HTML (never ASC, which is meant to be a faithful
    edf2asc reproduction with nothing extra alongside it).
    """
    import datetime
    filt = {k: v for k, v in (opts_json or {}).items()
            if v not in (None, "", [], {}, False)}
    return {
        "tool": "gama",
        "version": __version__,
        "exported_utc": datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "source_file": entry.name,
        "source_path": entry.path,
        "format": fmt,
        "timestamps": "relative_to_file_start" if relative else "absolute",
        "rows_written": n_rows,
        "source_total_lines": len(entry.records) if entry.records else None,
        "filters": filt,
        "edfapi": edfapi_version(),
    }



def _provenance_bytes(entry, fmt, opts_json, relative, n_rows):
    return json.dumps(
        build_provenance(entry, fmt, opts_json, relative, n_rows),
        indent=2).encode("utf-8")



def _opts_from_json(j):
    kinds = j.get("kinds")
    return {
        "groups": set(j.get("groups") or GROUP_ALL),
        "kinds": set(kinds) if kinds is not None else None,
        "eye": j.get("eye") or None,
        "tmin": j.get("tmin"), "tmax": j.get("tmax"),
        "min_fix": j.get("min_fix"), "min_sacc": j.get("min_sacc"),
        "contains": j.get("contains") or None,
        "exclude": j.get("exclude") or None,
        "contains_regex": bool(j.get("contains_regex")),
        "search": j.get("search") or None,
        "search_regex": bool(j.get("search_regex")),
    }



# ---------------------------------------------------------------------------
# Export.
# ---------------------------------------------------------------------------
def export_asc(records, indices):
    subset = [records[i] for i in indices if 0 <= i < len(records)]
    return records_to_bytes(subset), "text/plain"



def export_table(parsed, rows, indices, delimiter, relative=False, tref=0,
                 notes=None, trials=None):
    import csv
    notes = notes or {}
    has_notes = bool(notes)
    has_tr = bool(trials)
    buf = StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    header = list(EXPORT_COLUMNS)
    if has_tr:
        header += ["trial", "aoi", "aoi_from"]
    if has_notes:
        header += ["flagged", "note"]
    w.writerow(header)
    for i in indices:
        if not (0 <= i < len(parsed)):
            continue
        d, r = parsed[i], rows[i]
        st, en = d["start"], d["end"]
        if relative:
            if isinstance(st, int):
                st -= tref
            if isinstance(en, int):
                en -= tref
        msg = (d["msg"] or "").replace("\r", " ").replace("\n", " ")
        row = [i, r[I_CAT], r[I_GRP], r[I_MK], st, en,
               d["dur"], d["eye"], d["x1"], d["y1"], d["x2"], d["y2"],
               d["amp"], d["vel"], d["pupil"], d["resx"], d["resy"],
               d["val"], msg]
        if has_tr:
            tn = trials["row_trial"][i] if i < len(trials["row_trial"]) else -1
            row += [tn if tn > 0 else "",
                    trials["row_aoi"][i] if i < len(trials["row_aoi"]) else "",
                    trials["row_aoi_from"][i] if i < len(trials["row_aoi_from"]) else ""]
        if has_notes:
            nv = notes.get(str(i))
            if nv:
                row += ["1" if nv.get("flag") else "",
                        (nv.get("note") or "").replace("\r", " ").replace("\n", " ")]
            else:
                row += ["", ""]
        w.writerow(row)
    return buf.getvalue().encode("utf-8"), "text/csv"



_HTML_DOC = """<!DOCTYPE html>
<meta charset="utf-8"><title>{title}</title>
<style>
 body{{font:13px -apple-system,"Segoe UI",Roboto,sans-serif;margin:24px;color:#111}}
 h1{{font-size:16px;margin:0 0 2px}}
 .sub{{color:#666;font-size:12px;margin-bottom:14px}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{border:1px solid #ddd;padding:3px 6px;text-align:left;
   font-variant-numeric:tabular-nums;white-space:nowrap}}
 th{{background:#f3f4f6;position:sticky;top:0}}
 td.msg{{white-space:normal;font-variant-numeric:normal}}
 tr:nth-child(even){{background:#fafafa}}
 tr.flagged{{background:#fff3cd}}
 tr.flagged:nth-child(even){{background:#ffeeba}}
 @media print{{body{{margin:8px}} th{{position:static}}}}
</style>
<h1>{title}</h1>
<div class="sub">{sub}</div>
<table><thead><tr>{head}</tr></thead><tbody>
{rows}
</tbody></table>
"""



def _esc(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))



def export_html(parsed, rows, indices, relative=False, tref=0, title="EDF view",
                notes=None, trials=None):
    """A standalone, self-contained HTML copy of the current view (printable)."""
    notes = notes or {}
    has_notes = bool(notes)
    has_tr = bool(trials)
    cols = list(EXPORT_COLUMNS)
    if has_tr:
        cols += ["trial", "aoi", "aoi_from"]
    if has_notes:
        cols += ["flagged", "note"]
    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    out = []
    for i in indices:
        if not (0 <= i < len(parsed)):
            continue
        d, r = parsed[i], rows[i]
        st, en = d["start"], d["end"]
        if relative:
            if isinstance(st, int):
                st -= tref
            if isinstance(en, int):
                en -= tref
        msg = (d["msg"] or "").replace("\r", " ").replace("\n", " ")
        cells = [i, r[I_CAT], r[I_GRP], r[I_MK], st, en, d["dur"], d["eye"],
                 d["x1"], d["y1"], d["x2"], d["y2"], d["amp"], d["vel"],
                 d["pupil"], d["resx"], d["resy"], d["val"]]
        tds = "".join(f"<td>{_esc(c)}</td>" for c in cells)
        tds += f'<td class="msg">{_esc(msg)}</td>'
        if has_tr:
            tn = trials["row_trial"][i] if i < len(trials["row_trial"]) else -1
            tds += "<td>%s</td>" % _esc(tn if tn > 0 else "")
            tds += "<td>%s</td>" % _esc(trials["row_aoi"][i] if i < len(trials["row_aoi"]) else "")
            tds += "<td>%s</td>" % _esc(trials["row_aoi_from"][i] if i < len(trials["row_aoi_from"]) else "")
        nv = notes.get(str(i)) if has_notes else None
        if has_notes:
            flag_mark = "\u2691" if (nv and nv.get("flag")) else ""
            tds += "<td>" + flag_mark + "</td>"
            tds += f'<td class="msg">{_esc(nv.get("note") if nv else "")}</td>'
        cls = ' class="flagged"' if (nv and nv.get("flag")) else ""
        out.append(f"<tr{cls}>{tds}</tr>")
    sub = (f"{len(out):,} rows &middot; times "
           f"{'relative to file start' if relative else 'absolute'} &middot; "
           f"exported by gama {__version__}")
    doc = _HTML_DOC.format(title=_esc(title), sub=sub, head=head,
                           rows="\n".join(out))
    return doc.encode("utf-8"), "text/html"



def export_bytes(entry, indices, fmt, relative):
    notes = getattr(entry, "notes", None) or None
    trials = getattr(entry, "trials", None) or None
    if fmt == "asc":
        body, _ = export_asc(entry.records, indices)
        return body, ".asc"
    if fmt == "tsv":
        body, _ = export_table(entry.parsed, entry.rows, indices, "\t",
                               relative, entry.tmin, notes, trials)
        return body, ".tsv"
    if fmt == "html":
        body, _ = export_html(entry.parsed, entry.rows, indices, relative,
                              entry.tmin, entry.name, notes, trials)
        return body, ".html"
    body, _ = export_table(entry.parsed, entry.rows, indices, ",",
                           relative, entry.tmin, notes, trials)
    return body, ".csv"
