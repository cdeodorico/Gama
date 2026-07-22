"""Command line: head-less filtering and export, plus the app entry point."""

import argparse
import os
import json

from .version import __version__
from .convert import DEFAULT_CONVERTED_FROM
from .dataset import GROUP_ALL, build_dataset, I_CAT, I_GRP, I_START
from .filters import filter_indices
from .exports import (export_asc, export_table, export_html,
                      build_provenance)
from .preset_store import DEFAULT_PRESETS_DIR
from .server import serve



# ---------------------------------------------------------------------------
# Head-less helpers.
# ---------------------------------------------------------------------------
def _opts_from_args(args):
    groups = set(GROUP_ALL)
    if args.only:
        groups = {g.strip().upper() for g in args.only.split(",") if g.strip()}
    if args.hide:
        groups -= {g.strip().upper() for g in args.hide.split(",") if g.strip()}
    kinds = None
    if args.msg_kinds:
        kinds = {k.strip().lower() for k in args.msg_kinds.split(",") if k.strip()}
    return {
        "groups": groups, "kinds": kinds, "eye": args.eye,
        "tmin": args.tmin, "tmax": args.tmax,
        "min_fix": args.min_fix_dur, "min_sacc": args.min_sacc_dur,
        "contains": args.contains, "exclude": args.exclude,
        "contains_regex": args.regex,
        "search": args.search, "search_regex": args.regex,
    }



def _opts_json_from_args(args):
    """The GUI-shaped opts dict (for provenance), from CLI args."""
    o = _opts_from_args(args)
    return {
        "groups": sorted(o["groups"]) if o["groups"] else None,
        "kinds": sorted(o["kinds"]) if o["kinds"] else None,
        "eye": o["eye"], "tmin": o["tmin"], "tmax": o["tmax"],
        "min_fix": o["min_fix"], "min_sacc": o["min_sacc"],
        "contains": o["contains"], "exclude": o["exclude"],
        "contains_regex": o["contains_regex"],
        "search": o["search"], "search_regex": o["search_regex"],
    }



def _print_stats(rows, indices):
    from collections import Counter
    counts = Counter(rows[i][I_GRP] for i in indices)
    cat_counts = Counter(rows[i][I_CAT] for i in indices)
    times = [rows[i][I_START] for i in indices if isinstance(rows[i][I_START], int)]
    print(f"Matched {len(indices):,} of {len(rows):,} lines")
    if times:
        print(f"Time range: {min(times)} - {max(times)}")
    print("By group:")
    for g in GROUP_ALL:
        if counts.get(g):
            print(f"  {g:<9} {counts[g]:>8,}")
    print("By type:")
    for c, n in cat_counts.most_common():
        print(f"  {c:<11} {n:>8,}")



def _resolve_fmt(args, multi):
    if args.format:
        return "." + args.format
    if args.export and not multi:
        ext = os.path.splitext(args.export)[1].lower()
        if ext in (".asc", ".csv", ".tsv", ".html"):
            return ext
    return ".csv"



def _headless(args, paths):
    multi = len(paths) > 1
    fmt = _resolve_fmt(args, multi)
    if args.export and multi:
        os.makedirs(args.export, exist_ok=True)
    for path in paths:
        records, payload, parsed = build_dataset(path, args.converted_from_line)
        rows = payload["rows"]
        tref = payload["meta"]["tmin"]
        indices = filter_indices(rows, parsed, _opts_from_args(args))
        if args.stats:
            if multi:
                print(f"\n== {os.path.basename(path)} ==")
            _print_stats(rows, indices)
        if args.export:
            if multi:
                stem = os.path.splitext(os.path.basename(path))[0]
                out = os.path.join(args.export, f"{stem}_filtered{fmt}")
            else:
                out = args.export
            if fmt == ".asc":
                body, _ = export_asc(records, indices)
            elif fmt == ".tsv":
                body, _ = export_table(parsed, rows, indices, "\t",
                                       args.relative, tref)
            elif fmt == ".html":
                body, _ = export_html(parsed, rows, indices, args.relative,
                                      tref, os.path.basename(path))
            else:
                body, _ = export_table(parsed, rows, indices, ",",
                                       args.relative, tref)
            with open(out, "wb") as fh:
                fh.write(body)
            print(f"Wrote {len(indices):,} rows ({len(body):,} bytes) -> {out}")
            if getattr(args, "sidecar", False) and fmt != ".asc":
                shim = type("E", (), {"name": os.path.basename(path),
                                      "path": os.path.abspath(path),
                                      "records": records})()
                prov = build_provenance(
                    shim, fmt.lstrip("."),
                    _opts_json_from_args(args), args.relative, len(indices))
                side = os.path.splitext(out)[0] + ".gama.json"
                with open(side, "w") as fh:
                    json.dump(prov, fh, indent=2)
                print(f"  + provenance -> {side}")



def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edf", nargs="*", help="path(s) to input .EDF file(s)")
    ap.add_argument("--version", action="version",
                    version=f"gama {__version__}")
    ap.add_argument("--port", type=int, default=0, help="web port (default auto)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not auto-open a browser")
    ap.add_argument("--converted-from-line", default=DEFAULT_CONVERTED_FROM,
                    help="verbatim '** CONVERTED FROM ...' first line")
    ap.add_argument("--presets-dir", default=DEFAULT_PRESETS_DIR,
                    help=f"folder holding saved presets (default: {DEFAULT_PRESETS_DIR})")
    # head-less filter / export
    ap.add_argument("--export", metavar="OUT",
                    help="head-less: write filtered rows to OUT (a file for one "
                         "input, or a directory for several)")
    ap.add_argument("--format", choices=["asc", "csv", "tsv", "html"],
                    help="export format (overrides extension; needed for batch)")
    ap.add_argument("--relative", action="store_true",
                    help="write CSV/TSV times relative to each file's start")
    ap.add_argument("--sidecar", action="store_true",
                    help="also write a .gama.json provenance file next to each "
                         "CSV/TSV/HTML export (never for ASC)")
    ap.add_argument("--stats", action="store_true",
                    help="head-less: print a summary of the (filtered) data")
    ap.add_argument("--only", help="keep only these groups (comma list): " +
                    ",".join(GROUP_ALL))
    ap.add_argument("--hide", help="hide these groups (comma list)")
    ap.add_argument("--msg-kinds",
                    help="keep only these MSG kinds: experiment,config,cal,draw")
    ap.add_argument("--contains", help="keep MSG lines whose body matches")
    ap.add_argument("--exclude", help="drop MSG lines whose body matches")
    ap.add_argument("--search", help="keep lines whose raw text matches")
    ap.add_argument("--regex", action="store_true",
                    help="treat --contains/--exclude/--search as regex")
    ap.add_argument("--eye", choices=["R", "L"], help="keep only this eye")
    ap.add_argument("--tmin", type=int, help="minimum tracker time")
    ap.add_argument("--tmax", type=int, help="maximum tracker time")
    ap.add_argument("--min-fix-dur", type=int, help="minimum fixation duration (ms)")
    ap.add_argument("--min-sacc-dur", type=int, help="minimum saccade duration (ms)")
    args = ap.parse_args(argv)

    headless = bool(args.export or args.stats)
    paths = list(args.edf)
    if headless and not paths:
        raise SystemExit("No EDF file(s) provided.")
    for p in paths:
        if not os.path.isfile(p):
            raise SystemExit(f"File not found: {p}")

    if headless:
        _headless(args, paths)
    else:
        # Interactive mode needs no file up front: the app opens in the browser
        # and files are added/removed there.
        serve(paths, args.converted_from_line, args.port,
              not args.no_browser, args.presets_dir)
