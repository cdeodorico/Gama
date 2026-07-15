#!/usr/bin/env python3
r"""
gama.py -- EyeLink EDF Explorer (self-contained)
================================================

A single-file tool that opens SR Research EyeLink ``.EDF`` recordings and lets
you explore their converted ASC representation in a fast, spreadsheet-like web
UI, or filter/convert/export them from the command line.  The byte-identical
EDF->ASC engine and the interactive app are bundled together in this one file,
so there is nothing to install or keep alongside it.

Run with no arguments to choose file(s) with a dialog::

    python gama.py                     # file picker
    python gama.py a.EDF b.EDF         # open several files (one tab each)

Head-less / batch::

    python gama.py rec.EDF --stats
    python gama.py rec.EDF --export events.asc --only FIX,SACC,BLINK
    python gama.py *.EDF   --export out_dir --format csv --relative
"""

import argparse
import os
import sys
import ctypes as C
from decimal import Decimal, ROUND_HALF_UP


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

DEFAULT_CONVERTED_FROM = (
    "** CONVERTED FROM C:\\Users\\Canaan\\PycharmProjects\\Landolt_Exp\\data\\"
    "AP_2026_05_19_13_58\\AP_2026_05_19_13_58.EDF using edfapi 4.4.1 Win32  "
    "EyeLink Dataviewer Subcomponent Mar 19 2024 on Sat May 23 22:15:57 2026"
)

SP = " "  # the literal trailing space edf2asc writes after START/END times


# ---------------------------------------------------------------------------
# C / MSVC printf compatible fixed-point formatting.
#
# The reference file was produced by the Windows (MSVC) build of edf2asc, whose
# printf rounds half away from zero (e.g. 42.125 -> "42.13").  Python's native
# formatting uses round-half-to-even (-> "42.12").  We therefore round the
# exact decimal value of the float with ROUND_HALF_UP (== half away from zero
# for the non-negative quantities formatted here).
# ---------------------------------------------------------------------------
def cfmt(value, width, prec):
    """Format ``value`` like C printf ``%<width>.<prec>f`` (MSVC rounding)."""
    q = Decimal(1).scaleb(-prec)
    d = Decimal(float(value)).quantize(q, rounding=ROUND_HALF_UP)
    s = f"{d:.{prec}f}"
    return s.rjust(width) if width else s


def _message_text(fe):
    """Decoded message string for a MESSAGEEVENT FEVENT, or None."""
    if not fe.message:
        return None
    m = fe.message.contents
    raw = C.string_at(C.addressof(m) + 2, m.len)        # skip 2-byte LSTRING len
    raw = raw.split(b"\x00", 1)[0]
    return raw.decode("latin-1")                        # embedded '\n', no '\r'


def _saccade_amplitude(fe):
    """edf2asc saccade amplitude (deg): gaze displacement / mean resolution."""
    avgresx = (fe.supd_x + fe.eupd_x) / 2.0
    avgresy = (fe.supd_y + fe.eupd_y) / 2.0
    dx = (fe.genx - fe.gstx) / avgresx if avgresx else 0.0
    dy = (fe.geny - fe.gsty) / avgresy if avgresy else 0.0
    return (dx * dx + dy * dy) ** 0.5


# Gaze coordinates equal to this sentinel mean "missing"; edf2asc prints them
# as the literal token "   ." and renders the (now huge) saccade amplitude in
# MSVC scientific notation instead of fixed point.
MISSING_GAZE = 1e8


def _msvc_sci(value, prec, width):
    """C/MSVC printf %<width>.<prec>e (three-digit exponent, e.g. 3.2e+006)."""
    mant, exp = ("%.*e" % (prec, value)).split("e")
    return f"{mant}e{exp[0]}{int(exp[1:]):03d}".rjust(width)


def _fmt_gaze(v):
    """Format a gaze coordinate, or edf2asc's missing-data marker."""
    return "   ." if v >= MISSING_GAZE else cfmt(v, 7, 1)


def _tracking_word(record_type):
    # PUPIL_ONLY_250 = 0, PUPIL_ONLY_500 = 1, PUPIL_CR = 2
    return "CR" if record_type == 2 else "P"


def _pos_word(pos_type):
    # PARSEDBY_GAZE = 0xC0, PARSEDBY_HREF = 0x80, PARSEDBY_PUPIL = 0x40
    masked = pos_type & 0xC0
    if masked == 0xC0:
        return "GAZE"
    if masked == 0x80:
        return "HREF"
    return "PUPIL"


class _Block:
    """Per-recording-block resolution accumulator and header info."""

    __slots__ = ("sumx", "sumy", "n", "eyes", "rate", "pos_type",
                 "record_type", "filter_type", "rx", "ry", "eye_word",
                 "start_time")

    def __init__(self, rec):
        self.sumx = 0.0
        self.sumy = 0.0
        self.n = 0
        self.eyes = set()
        self.rate = float(rec.sample_rate)
        self.pos_type = rec.pos_type
        self.record_type = rec.record_type
        self.filter_type = rec.filter_type
        self.start_time = rec.time
        self.rx = self.ry = None
        self.eye_word = "RIGHT"

    def add_start(self, fe):
        # A start parse event (SFIX/SSACC) contributes its start resolution.
        self.sumx += fe.supd_x
        self.sumy += fe.supd_y
        self.n += 1
        self.eyes.add(fe.eye)

    def add_end(self, fe):
        # An end parse event (EFIX/ESACC) contributes start AND end resolution.
        self.sumx += fe.supd_x + fe.eupd_x
        self.sumy += fe.supd_y + fe.eupd_y
        self.n += 2
        self.eyes.add(fe.eye)

    def finalize(self):
        if self.n:
            self.rx = cfmt(self.sumx / self.n, 7, 2)
            self.ry = cfmt(self.sumy / self.n, 7, 2)
        else:
            self.rx = cfmt(0.0, 7, 2)
            self.ry = cfmt(0.0, 7, 2)
        if self.eyes == {0}:
            self.eye_word = "LEFT"
        elif self.eyes == {1}:
            self.eye_word = "RIGHT"
        elif len(self.eyes) > 1:
            self.eye_word = "BINOCULAR"


# ---------------------------------------------------------------------------
# Pass 1: read the EDF into an ordered element list + per-block accumulators.
#
# Each stored element is a tuple ``(kind, time, payload)``.  Control elements
# that produce no ASC line (button presses, start/end-events markers) and
# samples are dropped.  Blocks are accumulated with a FIFO of open recordings
# so that paused/resumed (same-timestamp) recordings are attributed correctly.
# ---------------------------------------------------------------------------
def _read_edf(edf_path):
    E = _load_edfapi()
    err = C.c_int(0)
    # consistency=0, load_events=1, load_samples=0  (events-only output; END-line
    # RES is rebuilt from parse-event resolution fields, so samples are unneeded)
    ef = E.edf_open_file(edf_path.encode("utf-8"), 0, 1, 0, C.byref(err))
    if not ef or err.value != 0:
        raise SystemExit(f"edf_open_file failed (err={err.value}) for {edf_path}")

    plen = E.edf_get_preamble_text_length(ef)
    pbuf = C.create_string_buffer(plen + 1)
    E.edf_get_preamble_text(ef, pbuf, plen + 1)
    preamble = pbuf.value.decode("latin-1")

    elements = []
    blocks = []
    openq = []          # indices into blocks of open (un-closed) recordings

    # edf2asc reports the start time of a fixation/saccade as the timestamp of
    # the most recent STARTFIX/STARTSACC it has seen, not the sttime stored on
    # the ENDFIX/ENDSACC.  These differ only for "orphan" end events whose start
    # was clipped by a recording pause; matching edf2asc requires the tracked
    # value.
    last_sfix_time = 0
    last_ssacc_time = 0

    devnull = os.open(os.devnull, os.O_WRONLY)
    saved_stdout = os.dup(1)
    os.dup2(devnull, 1)
    try:
        while True:
            t = E.edf_get_next_data(ef)
            if t == NO_PENDING_ITEMS:
                break
            fd = E.edf_get_float_data(ef)

            if t == RECORDING_INFO:
                rec = fd.contents.rec
                if rec.state == 1:                          # REC-START
                    blk = _Block(rec)
                    blocks.append(blk)
                    openq.append(len(blocks) - 1)
                    elements.append(("REC_S", rec.time, ()))
                else:                                       # REC-END
                    if openq:
                        idx = openq.pop(0)                  # FIFO close
                        blocks[idx].finalize()
                    elements.append(("REC_E", rec.time, ()))

            elif t == MESSAGEEVENT:
                fe = fd.contents.fe
                elements.append(("MSG", fe.sttime, (_message_text(fe),)))

            elif t == INPUTEVENT:
                fe = fd.contents.fe
                elements.append(("INPUT", fe.sttime, (fe.input,)))

            elif t == BUTTONEVENT:
                pass

            elif t == STARTFIX:
                fe = fd.contents.fe
                last_sfix_time = fe.sttime
                if openq:
                    blocks[openq[0]].add_start(fe)
                elements.append(("SFIX", fe.sttime, (fe.eye,)))

            elif t == STARTSACC:
                fe = fd.contents.fe
                last_ssacc_time = fe.sttime
                if openq:
                    blocks[openq[0]].add_start(fe)
                elements.append(("SSACC", fe.sttime, (fe.eye,)))

            elif t == STARTBLINK:
                fe = fd.contents.fe
                elements.append(("SBLINK", fe.sttime, (fe.eye,)))

            elif t == ENDFIX:
                fe = fd.contents.fe
                if openq:
                    blocks[openq[0]].add_end(fe)
                elements.append((
                    "EFIX", last_sfix_time,
                    (fe.eye, fe.entime, fe.gavx, fe.gavy, fe.ava),
                ))

            elif t == ENDSACC:
                fe = fd.contents.fe
                if openq:
                    blocks[openq[0]].add_end(fe)
                elements.append((
                    "ESACC", last_ssacc_time,
                    (fe.eye, fe.entime, fe.gstx, fe.gsty, fe.genx, fe.geny,
                     _saccade_amplitude(fe), fe.pvel),
                ))

            elif t == ENDBLINK:
                fe = fd.contents.fe
                elements.append(("EBLINK", fe.sttime, (fe.eye, fe.entime)))

            # everything else (start/end samples & events markers, samples) -> drop
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
        os.close(devnull)
        E.edf_close_file(ef)

    return preamble, elements, blocks


# ---------------------------------------------------------------------------
# Pass 2: render the ordered element list into ASC logical lines.
#
# Recording-boundary "clusters" (consecutive control elements at one timestamp:
# REC-START/REC-END/INPUT) are emitted in canonical order regardless of their
# raw stream order:
#   * a cluster that closes a recording  -> INPUT(s), END, then (if it also
#     re-opens) the new START header;
#   * a cluster that only opens a recording -> START header, then INPUT(s).
# This reproduces edf2asc, which serialises paused/resumed recordings as
# START..END..START..END even though the EDF stream may interleave them.
# ---------------------------------------------------------------------------
CONTROL_KINDS = {"REC_S", "REC_E", "INPUT"}

# Message classification for the explorer UI.  These prefixes identify EyeLink
# tracker setup / configuration messages and DataViewer drawing commands, which
# users typically want to hide when studying experiment output.
_CONFIG_MSG_PREFIXES = (
    "!MODE", "RECCFG", "ELCLCFG", "ELCL_", "GAZE_COORDS", "DISPLAY_COORDS",
    "THRESHOLDS", "CAMERA_LENS", "PUPIL_DATA_TYPE", "ELCL_PROC",
    "ELCL_EFIT_PARAMS", "ELCL_WINDOW_SIZES", "TRACKER_TIME", "RETRACE",
)
_CAL_MSG_PREFIXES = ("!CAL", "VALIDATE", "VALIDATION", "CALIBRATION")


def classify_message(content):
    """Return a coarse kind for a MSG body: cal / config / draw / experiment."""
    if content is None:
        return "experiment"
    head = content.lstrip()
    upper = head.upper()
    for p in _CAL_MSG_PREFIXES:
        if upper.startswith(p):
            return "cal"
    if head.startswith("!V"):
        return "draw"
    for p in _CONFIG_MSG_PREFIXES:
        if upper.startswith(p):
            return "config"
    return "experiment"


def _render(preamble, elements, blocks, converted_from_line):
    """Build the ordered list of output records (text + metadata).

    Each record is a dict with keys:
        text   - the exact ASC logical line (may contain embedded '\\n')
        cat    - line category (see CATEGORIES)
        time   - tracker timestamp or None
        block  - recording block index (0-based) or None
        msg    - message body (MSG lines only) or None
        mkind  - message kind (MSG lines only): cal/config/draw/experiment
    """
    records = []

    def add(text, cat, time=None, block=None, msg=None, mkind=None):
        records.append({"text": text, "cat": cat, "time": time,
                        "block": block, "msg": msg, "mkind": mkind})

    add(converted_from_line, "converted_from")
    for pline in preamble.split("\n"):
        if pline != "":
            add(pline, "preamble")
    add("**", "preamble_end")
    add("", "blank")

    open_count = 0
    close_count = 0
    cur_block = None

    def emit_input(tm, val):
        add(f"INPUT\t{tm}\t{val}", "input", time=tm, block=cur_block)

    def emit_start_header():
        nonlocal open_count, cur_block
        blk = blocks[open_count]
        bidx = open_count
        open_count += 1
        cur_block = bidx
        st = blk.start_time
        add(f"START\t{st}{SP}\t{blk.eye_word}\tEVENTS", "start",
            time=st, block=bidx)
        add("PRESCALER\t1", "prescaler", time=st, block=bidx)
        add("VPRESCALER\t1", "vprescaler", time=st, block=bidx)
        add("PUPIL\tDIAMETER", "pupil", time=st, block=bidx)
        add(
            "EVENTS\tGAZE\t{eye}\tRATE\t{rate}\tTRACKING\t{trk}\tFILTER\t{flt}"
            .format(eye=blk.eye_word, rate=cfmt(blk.rate, 0, 2),
                    trk=_tracking_word(blk.record_type), flt=blk.filter_type),
            "events", time=st, block=bidx,
        )

    def emit_end(end_time):
        nonlocal close_count, cur_block
        blk = blocks[close_count]
        bidx = close_count
        close_count += 1
        add(f"END\t{end_time}{SP}\tEVENTS\tRES\t{blk.rx}\t{blk.ry}", "end",
            time=end_time, block=bidx)
        cur_block = None

    n = len(elements)
    i = 0
    while i < n:
        kind, etime, payload = elements[i]

        if kind in CONTROL_KINDS:
            j = i
            inputs = []
            has_rec_s = has_rec_e = False
            end_time = None
            while j < n and elements[j][0] in CONTROL_KINDS and elements[j][1] == etime:
                k, tm, pl = elements[j]
                if k == "REC_S":
                    has_rec_s = True
                elif k == "REC_E":
                    has_rec_e = True
                    end_time = tm
                else:
                    inputs.append((tm, pl[0]))
                j += 1

            if has_rec_e:
                for tm, val in inputs:
                    emit_input(tm, val)
                emit_end(end_time)
                if has_rec_s:
                    emit_start_header()
            elif has_rec_s:
                emit_start_header()
                for tm, val in inputs:
                    emit_input(tm, val)
            else:
                for tm, val in inputs:
                    emit_input(tm, val)
            i = j
            continue

        if kind == "MSG":
            (text,) = payload
            line = f"MSG\t{etime} " if text is None else f"MSG\t{etime} {text}"
            add(line, "msg", time=etime, block=cur_block,
                msg=(text or ""), mkind=classify_message(text))
        elif kind == "SFIX":
            (eye,) = payload
            add(f"{'SFIX ' + EYE_LETTER[eye]:<9}{etime}", "sfix",
                time=etime, block=cur_block)
        elif kind == "SSACC":
            (eye,) = payload
            add(f"{'SSACC ' + EYE_LETTER[eye]:<9}{etime}", "ssacc",
                time=etime, block=cur_block)
        elif kind == "SBLINK":
            (eye,) = payload
            add(f"{'SBLINK ' + EYE_LETTER[eye]:<9}{etime}", "sblink",
                time=etime, block=cur_block)
        elif kind == "EFIX":
            eye, en, gavx, gavy, ava = payload
            pre = f"{'EFIX ' + EYE_LETTER[eye]:<9}"
            add(f"{pre}{etime}\t{en}\t{en - etime + 1}\t"
                f"{cfmt(gavx, 7, 1)}\t{cfmt(gavy, 7, 1)}\t{cfmt(ava, 7, 0)}",
                "efix", time=etime, block=cur_block)
        elif kind == "ESACC":
            eye, en, sx, sy, ex, ey, ampl, pvel = payload
            pre = f"{'ESACC ' + EYE_LETTER[eye]:<9}"
            missing = (sx >= MISSING_GAZE or sy >= MISSING_GAZE
                       or ex >= MISSING_GAZE or ey >= MISSING_GAZE)
            amp_str = _msvc_sci(ampl, 1, 9) if missing else cfmt(ampl, 7, 2)
            add(f"{pre}{etime}\t{en}\t{en - etime + 1}\t"
                f"{_fmt_gaze(sx)}\t{_fmt_gaze(sy)}\t"
                f"{_fmt_gaze(ex)}\t{_fmt_gaze(ey)}\t"
                f"{amp_str}\t{cfmt(pvel, 7, 0)}",
                "esacc", time=etime, block=cur_block)
        elif kind == "EBLINK":
            eye, en = payload
            pre = f"{'EBLINK ' + EYE_LETTER[eye]:<9}"
            add(f"{pre}{etime}\t{en}\t{en - etime + 1}", "eblink",
                time=etime, block=cur_block)
        i += 1

    return records


def build_records(edf_path, converted_from_line=DEFAULT_CONVERTED_FROM):
    """Parse an EDF and return (records, blocks).

    ``records`` is the full ordered list of output-line records (see _render);
    joining their ``text`` with CRLF reproduces the byte-identical ASC.  This is
    the entry point used by the interactive explorer.
    """
    preamble, elements, blocks = _read_edf(edf_path)
    elements = _coalesce_blinks(elements)
    records = _render(preamble, elements, blocks, converted_from_line)
    return records, blocks


def records_to_bytes(records):
    """Serialise records to the exact ASC byte stream (CRLF line endings)."""
    body = "".join(r["text"] + "\n" for r in records)
    return body.replace("\n", "\r\n").encode("latin-1")


def _coalesce_blinks(elements):
    """Merge all blink events that fall within one saccade into a single blink.

    EyeLink can report several STARTBLINK/ENDBLINK pairs while the eye is lost
    during a single saccade (the pupil briefly reappears then vanishes again),
    sometimes with messages interleaved between them.  edf2asc collapses every
    blink inside a saccade into one SBLINK (emitted at the first blink's start,
    in its original position) and one EBLINK spanning the first start to the
    last end (emitted in the position of that last ENDBLINK).  Intermediate
    blink events are dropped; non-blink events keep their positions.  Blinks
    outside any saccade are left untouched.
    """
    n = len(elements)
    managed = set()                 # indices of in-saccade blink events
    replace = {}                    # index -> replacement element tuple
    in_sacc = False
    group = []                      # (idx, kind, sttime, entime, eye)

    def flush(grp):
        if not grp:
            return
        starts = [g for g in grp if g[1] == "SBLINK"]
        ends = [g for g in grp if g[1] == "EBLINK"]
        eye = grp[0][4]
        first_start = starts[0][2] if starts else ends[0][2]
        if starts:
            replace[starts[0][0]] = ("SBLINK", first_start, (eye,))
        if ends:
            replace[ends[-1][0]] = ("EBLINK", first_start, (eye, ends[-1][3]))

    for i, el in enumerate(elements):
        k = el[0]
        if k == "SSACC":
            in_sacc = True
        elif k == "ESACC":
            flush(group)
            group = []
            in_sacc = False
        elif k in ("SBLINK", "EBLINK") and in_sacc:
            managed.add(i)
            en = el[2][1] if k == "EBLINK" else None
            group.append((i, k, el[1], en, el[2][0]))
    flush(group)                    # in case of a trailing (unterminated) saccade

    out = []
    for i, el in enumerate(elements):
        if i in managed:
            if i in replace:
                out.append(replace[i])
            # else: dropped
        else:
            out.append(el)
    return out


def convert(edf_path, asc_path, converted_from_line=DEFAULT_CONVERTED_FROM):
    records, _ = build_records(edf_path, converted_from_line)
    data = records_to_bytes(records)
    with open(asc_path, "wb") as fh:
        fh.write(data)
    return asc_path, len(data)


def _edf2asc_main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert an EyeLink .EDF to a byte-identical events .ASC"
    )
    ap.add_argument("edf", help="path to the input .EDF file")
    ap.add_argument("-o", "--output", help="path to the output .asc file")
    ap.add_argument(
        "--converted-from-line",
        default=DEFAULT_CONVERTED_FROM,
        help="verbatim first line ('** CONVERTED FROM ... on <date>')",
    )
    args = ap.parse_args(argv)

    out = args.output
    if out is None:
        base, _ = os.path.splitext(args.edf)
        out = base + ".asc"

    path, nbytes = convert(args.edf, out, args.converted_from_line)
    print(f"Wrote {nbytes} bytes to {path}")


# ==========================================================================
# Interactive explorer application
# ==========================================================================
r"""
gama.py
===============

Open one or more SR Research EyeLink ``.EDF`` files and explore their converted
ASC representation interactively -- like opening the ASC in a spreadsheet, but
with purpose-built filters for eye-tracking data, and a tab per file.

Reuses ``edf2asc_emulator.py`` (which must sit next to this file) for the exact,
byte-identical parse, then serves a small local web app that can:

* open several files at once, each in its own tab (parsed lazily on first view);
* view every line in a fast, virtualised table (handles 80k+ rows smoothly);
* show/hide categories, split messages into experiment/config/cal/draw kinds,
  filter message bodies (substring/regex), filter by eye, time range, and
  per-type (fixation / saccade) minimum duration, and search raw lines;
* show timestamps as absolute or relative to each file's first timestamp;
* save and load named filter presets (persisted to a JSON file on disk);
* export the current view for the active file, or bulk-export every loaded file
  with the current settings as a single ZIP -- as ASC, CSV or TSV.
  Exporting all rows with no filters reproduces the byte-identical ASC.

Interactive (web) mode::

    python gama.py a.EDF b.EDF c.EDF     # or no args for a file picker

Head-less / batch mode::

    python gama.py rec.EDF --stats
    python gama.py rec.EDF --export events.asc --only FIX,SACC,BLINK
    python gama.py *.EDF --export out_dir --format csv --relative \
        --only MSG --msg-kinds experiment --contains TRIAL_
"""

import argparse
import gzip
import io
import json
import os
import re
import sys
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from urllib.parse import urlparse, parse_qs

# When bundled by PyInstaller, __file__ points inside the temporary extraction
# directory; use the executable's own folder so things like the presets folder
# live next to the program and persist between runs.
if getattr(sys, "frozen", False):
    _exe_dir = os.path.dirname(sys.executable)
    # macOS .app: .../gama.app/Contents/MacOS/gama -> keep data beside the .app
    if sys.platform == "darwin" and _exe_dir.endswith("/Contents/MacOS"):
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_exe_dir)))
    else:
        BASE_DIR = _exe_dir
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE_DIR)
em = sys.modules[__name__]      # engine lives in this same file


def _resource_bytes(name):
    """Read a bundled resource (works both as a script and when frozen)."""
    for base in (getattr(sys, "_MEIPASS", None), BASE_DIR):
        if base:
            p = os.path.join(base, name)
            if os.path.isfile(p):
                try:
                    with open(p, "rb") as fh:
                        return fh.read()
                except OSError:
                    pass
    return None


ICON_BYTES = _resource_bytes("icon.png")

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
                "val", "raw"]
I_IDX, I_CAT, I_GRP, I_MK, I_START, I_END, I_DUR, I_EYE = 0, 1, 2, 3, 4, 5, 6, 7
I_RAW = 18

EXPORT_COLUMNS = ["idx", "category", "group", "msg_kind", "start", "end",
                  "dur", "eye", "x1", "y1", "x2", "y2", "amp", "pvel", "pupil",
                  "res_x", "res_y", "input", "message"]

DEFAULT_PRESETS_DIR = os.path.join(BASE_DIR, "presets")
_PRESETS_LOCK = threading.Lock()


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


def build_dataset(edf_path, converted_from_line):
    """Parse the EDF and return (records, wire_payload, parsed_rows)."""
    records, _blocks = em.build_records(edf_path, converted_from_line)
    rows, parsed, times = [], [], []
    group_counts, kind_counts = {}, {}
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
        rows.append([
            idx, CAT_LABEL[cat], grp, rec["mkind"] or "",
            d["start"], d["end"], d["dur"], d["eye"],
            d["x1"], d["y1"], d["x2"], d["y2"], d["amp"], d["vel"],
            d["pupil"], d["resx"], d["resy"], d["val"], rec["text"],
        ])
    meta = {
        "filename": os.path.basename(edf_path),
        "total": len(records),
        "tmin": min(times) if times else 0,
        "tmax": max(times) if times else 0,
        "groups": GROUP_ALL,
        "group_counts": group_counts,
        "msg_kinds": ["experiment", "config", "cal", "draw"],
        "kind_counts": kind_counts,
    }
    return records, {"columns": WIRE_COLUMNS, "rows": rows, "meta": meta}, parsed


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
    return em.records_to_bytes(subset), "text/plain"


def export_table(parsed, rows, indices, delimiter, relative=False, tref=0):
    import csv
    buf = StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    w.writerow(EXPORT_COLUMNS)
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
        w.writerow([i, r[I_CAT], r[I_GRP], r[I_MK], st, en,
                    d["dur"], d["eye"], d["x1"], d["y1"], d["x2"], d["y2"],
                    d["amp"], d["vel"], d["pupil"], d["resx"], d["resy"],
                    d["val"], msg])
    return buf.getvalue().encode("utf-8"), "text/csv"


def export_bytes(entry, indices, fmt, relative):
    if fmt == "asc":
        body, _ = export_asc(entry.records, indices)
        return body, ".asc"
    if fmt == "tsv":
        body, _ = export_table(entry.parsed, entry.rows, indices, "\t",
                               relative, entry.tmin)
        return body, ".tsv"
    body, _ = export_table(entry.parsed, entry.rows, indices, ",",
                           relative, entry.tmin)
    return body, ".csv"


# ---------------------------------------------------------------------------
# Presets on disk -- one JSON file per preset inside a "presets" folder.
# ---------------------------------------------------------------------------
def _preset_slug(name):
    slug = re.sub(r"[^\w.-]+", "_", name).strip("_")
    return slug or "preset"


def _load_presets(dir_path):
    """Return {name: config} by reading every *.json in the folder."""
    out = {}
    try:
        names = sorted(os.listdir(dir_path))
    except FileNotFoundError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(dir_path, fn)) as fh:
                obj = json.load(fh)
            name = obj.get("name") or os.path.splitext(fn)[0]
            out[name] = obj.get("config", {})
        except Exception:
            continue
    return out


def _find_preset_file(dir_path, name):
    try:
        files = os.listdir(dir_path)
    except FileNotFoundError:
        return None
    for fn in files:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(dir_path, fn)) as fh:
                if json.load(fh).get("name") == name:
                    return os.path.join(dir_path, fn)
        except Exception:
            continue
    return None


def _save_preset(dir_path, name, config):
    with _PRESETS_LOCK:
        os.makedirs(dir_path, exist_ok=True)
        path = _find_preset_file(dir_path, name)
        if path is None:
            slug = _preset_slug(name)
            path = os.path.join(dir_path, slug + ".json")
            i = 1
            while os.path.exists(path):
                path = os.path.join(dir_path, f"{slug}_{i}.json")
                i += 1
        with open(path, "w") as fh:
            json.dump({"name": name, "config": config}, fh, indent=2)


def _delete_preset(dir_path, name):
    with _PRESETS_LOCK:
        path = _find_preset_file(dir_path, name)
        if path and os.path.isfile(path):
            os.remove(path)


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
        self.tmin = self.tmax = 0
        self.records = self.parsed = self.rows = self.payload_gz = None


def _ensure_parsed(entry, converted_from_line):
    with entry.lock:
        if entry.ready or entry.error:
            return
        try:
            print(f"Parsing {entry.name} ...", flush=True)
            records, payload, parsed = build_dataset(entry.path, converted_from_line)
            entry.records, entry.parsed, entry.rows = records, parsed, payload["rows"]
            entry.tmin, entry.tmax = payload["meta"]["tmin"], payload["meta"]["tmax"]
            entry.payload_gz = gzip.compress(
                json.dumps(payload, separators=(",", ":")).encode("utf-8"), 6)
            entry.ready = True
            print(f"  {payload['meta']['total']} lines "
                  f"({len(entry.payload_gz) / 1e6:.1f} MB compressed).", flush=True)
        except BaseException as exc:  # incl. SystemExit from edfapi loading
            import traceback
            entry.error = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__))
            # Recorded once; the browser is shown the message instead of the
            # request being retried forever.
            print(f"\nERROR parsing {entry.name}:\n{entry.error}", flush=True)


def make_handler(files, converted_from_line, presets_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def _body(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def _file_arg(self):
            q = parse_qs(urlparse(self.path).query)
            try:
                fid = int(q.get("file", ["0"])[0])
            except ValueError:
                fid = 0
            return fid if 0 <= fid < len(files) else 0

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                self._send(200, HTML_PAGE.encode("utf-8"),
                           "text/html; charset=utf-8")
            elif route == "/icon.png" or route == "/favicon.ico":
                if ICON_BYTES:
                    self._send(200, ICON_BYTES, "image/png",
                               {"Cache-Control": "max-age=86400"})
                else:
                    self._send(404, b"", "text/plain")
            elif route == "/api/files":
                self._json([{"id": i, "name": f.name}
                            for i, f in enumerate(files)])
            elif route == "/api/presets":
                self._json(_load_presets(presets_dir))
            elif route == "/api/rows":
                entry = files[self._file_arg()]
                _ensure_parsed(entry, converted_from_line)
                if entry.error:
                    self._send(500, ("Failed to parse " + entry.name + ":\n\n"
                                     + entry.error).encode("utf-8"),
                               "text/plain; charset=utf-8")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(entry.payload_gz)))
                self.end_headers()
                self.wfile.write(entry.payload_gz)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/api/export":
                self._export_one()
            elif route == "/api/export_all":
                self._export_all()
            elif route == "/api/presets":
                self._presets_write()
            else:
                self._send(404, b"not found", "text/plain")

        def _export_one(self):
            req = self._body()
            fid = req.get("file", 0)
            fid = fid if 0 <= fid < len(files) else 0
            entry = files[fid]
            _ensure_parsed(entry, converted_from_line)
            if entry.error:
                self._send(500, entry.error.encode("utf-8"), "text/plain")
                return
            body, ext = export_bytes(entry, req.get("indices", []),
                                     req.get("format", "asc"),
                                     req.get("relative", False))
            self._send(200, body, "application/octet-stream",
                       {"Content-Disposition":
                        f'attachment; filename="{entry.base}_filtered{ext}"'})

        def _export_all(self):
            req = self._body()
            fmt = req.get("format", "asc")
            relative = req.get("relative", False)
            opts = _opts_from_json(req.get("opts", {}))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for entry in files:
                    _ensure_parsed(entry, converted_from_line)
                    if entry.error:
                        continue
                    idx = filter_indices(entry.rows, entry.parsed, opts)
                    body, ext = export_bytes(entry, idx, fmt, relative)
                    z.writestr(f"{entry.base}_filtered{ext}", body)
            data = buf.getvalue()
            self._send(200, data, "application/zip",
                       {"Content-Disposition":
                        f'attachment; filename="edf_export_{fmt}.zip"'})

        def _presets_write(self):
            req = self._body()
            action = req.get("action")
            name = (req.get("name") or "").strip()
            if action == "save" and name:
                _save_preset(presets_dir, name, req.get("config", {}))
            elif action == "delete" and name:
                _delete_preset(presets_dir, name)
            self._json(_load_presets(presets_dir))
    return Handler


def serve(paths, converted_from_line, port, open_browser, presets_dir):
    files = [FileEntry(p) for p in paths]
    try:
        os.makedirs(presets_dir, exist_ok=True)
    except OSError:
        pass
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port),
        make_handler(files, converted_from_line, presets_dir))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"EDF Explorer: {len(files)} file(s) loaded (parsed on first view).")
    print(f"Presets folder: {presets_dir}")
    print(f"Running at {url}\nPress Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


def _pick_files():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        paths = filedialog.askopenfilenames(
            title="Open EyeLink .EDF file(s)",
            filetypes=[("EyeLink EDF", "*.EDF *.edf"), ("All files", "*.*")])
        root.destroy()
        return list(paths)
    except Exception:
        return []


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
        if ext in (".asc", ".csv", ".tsv"):
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
            else:
                body, _ = export_table(parsed, rows, indices, ",",
                                       args.relative, tref)
            with open(out, "wb") as fh:
                fh.write(body)
            print(f"Wrote {len(indices):,} rows ({len(body):,} bytes) -> {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edf", nargs="*", help="path(s) to input .EDF file(s)")
    ap.add_argument("--port", type=int, default=0, help="web port (default auto)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not auto-open a browser")
    ap.add_argument("--converted-from-line", default=em.DEFAULT_CONVERTED_FROM,
                    help="verbatim '** CONVERTED FROM ...' first line")
    ap.add_argument("--presets-dir", default=DEFAULT_PRESETS_DIR,
                    help=f"folder holding saved presets (default: {DEFAULT_PRESETS_DIR})")
    # head-less filter / export
    ap.add_argument("--export", metavar="OUT",
                    help="head-less: write filtered rows to OUT (a file for one "
                         "input, or a directory for several)")
    ap.add_argument("--format", choices=["asc", "csv", "tsv"],
                    help="export format (overrides extension; needed for batch)")
    ap.add_argument("--relative", action="store_true",
                    help="write CSV/TSV times relative to each file's start")
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
    paths = list(args.edf) or (None if headless else _pick_files())
    if not paths:
        raise SystemExit("No EDF file(s) provided.")
    for p in paths:
        if not os.path.isfile(p):
            raise SystemExit(f"File not found: {p}")

    if headless:
        _headless(args, paths)
    else:
        serve(paths, args.converted_from_line, args.port,
              not args.no_browser, args.presets_dir)


# ---------------------------------------------------------------------------
# Embedded single-page web UI.
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EDF Explorer</title>
<link rel="icon" type="image/png" href="/icon.png">
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1d212b; --line:#2a2f3a;
    --txt:#e6e9ef; --muted:#9aa3b2; --accent:#5b9dff; --accent2:#36c08f;
    --row:#13161c; --rowalt:#161a21; --rowhover:#222838; --rowsel:#2a3550;
    --rowh:24px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--txt);
    font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  button,input,select{font:inherit;color:inherit}
  .app{display:flex;flex-direction:column;height:100vh}
  header{display:flex;align-items:center;gap:14px;padding:8px 14px;
    background:var(--panel);border-bottom:1px solid var(--line);flex:none}
  header h1{font-size:14px;font-weight:600;margin:0;letter-spacing:.3px}
  header img.appicon{height:22px;width:22px;object-fit:contain;border-radius:5px}
  header .file{color:var(--accent);font-weight:600}
  header .count{color:var(--muted)}
  header .spacer{flex:1}
  .btn{background:var(--panel2);border:1px solid var(--line);border-radius:6px;
    padding:6px 11px;cursor:pointer;color:var(--txt)}
  .btn:hover{border-color:var(--accent);background:#222838}
  .btn.sm{padding:5px 8px;font-size:12px}
  .tabs{display:flex;gap:3px;background:var(--panel);flex:none;
    border-bottom:1px solid var(--line);padding:4px 8px 0;overflow-x:auto}
  .tab{padding:7px 14px;cursor:pointer;color:var(--muted);
    border:1px solid transparent;border-bottom:none;border-radius:7px 7px 0 0;
    white-space:nowrap;font-size:12.5px;max-width:280px;overflow:hidden;
    text-overflow:ellipsis}
  .tab:hover{color:var(--txt);background:var(--panel2)}
  .tab.active{color:var(--txt);background:var(--bg);border-color:var(--line);
    font-weight:600}
  .main{display:flex;flex:1;min-height:0}
  aside{width:268px;flex:none;background:var(--panel);border-right:1px solid var(--line);
    overflow-y:auto;padding:12px}
  aside h3{font-size:11px;text-transform:uppercase;letter-spacing:.6px;
    color:var(--muted);margin:16px 0 7px}
  aside h3:first-child{margin-top:2px}
  .grp{display:flex;align-items:center;gap:7px;padding:3px 4px;border-radius:5px;cursor:pointer}
  .grp:hover{background:var(--panel2)}
  .grp .c{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums;font-size:11px}
  .grp.sub{padding-left:20px;font-size:12px}
  .swatch{width:9px;height:9px;border-radius:2px;flex:none}
  input[type=text],input[type=number],select{width:100%;background:var(--panel2);
    border:1px solid var(--line);border-radius:5px;padding:5px 7px;margin-bottom:6px}
  .row2{display:flex;gap:6px}
  .row2 input{margin-bottom:6px}
  label.chk{display:flex;align-items:center;gap:7px;cursor:pointer;margin-bottom:4px}
  .presets{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:4px}
  .saved{display:flex;flex-direction:column;gap:3px;margin-bottom:6px}
  .pri{display:flex;align-items:center;gap:6px;padding:4px 7px;border-radius:5px;
    cursor:pointer;background:var(--panel2);border:1px solid var(--line)}
  .pri:hover{border-color:var(--accent)}
  .pri .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pri .del{color:var(--muted);cursor:pointer;padding:0 3px;font-weight:700}
  .pri .del:hover{color:#f472b6}
  .empty{color:var(--muted);font-size:11px;font-style:italic;margin-bottom:6px}
  .tablewrap{flex:1;min-width:0;display:flex;flex-direction:column}
  .scroll{flex:1;overflow:auto;position:relative}
  .thead{display:flex;position:sticky;top:0;z-index:5;background:var(--panel2);
    border-bottom:1px solid var(--line)}
  .th{flex:none;padding:6px 8px;font-weight:600;font-size:12px;color:var(--muted);
    border-right:1px solid var(--line);cursor:pointer;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis;user-select:none}
  .th:hover{color:var(--txt)}
  .th .ar{color:var(--accent);margin-left:3px}
  .vport{position:relative}
  .tr{display:flex;position:absolute;left:0;right:0;height:var(--rowh);
    align-items:center;border-bottom:1px solid #0c0e12}
  .tr:nth-child(even){background:var(--rowalt)}
  .tr:nth-child(odd){background:var(--row)}
  .tr:hover{background:var(--rowhover)}
  .tr.sel{background:var(--rowsel)}
  .td{flex:none;padding:0 8px;height:var(--rowh);line-height:var(--rowh);
    overflow:hidden;white-space:nowrap;text-overflow:ellipsis;
    font-variant-numeric:tabular-nums;border-right:1px solid #0c0e12}
  .td.msg{font-variant-numeric:normal;color:#cfd6e4}
  .badge{display:inline-block;padding:0 6px;border-radius:9px;font-size:10.5px;
    font-weight:700;line-height:16px;color:#0c0e12}
  .details{flex:none;height:0;overflow:hidden;background:var(--panel);
    border-top:1px solid var(--line);transition:height .12s}
  .details.open{height:190px;overflow:auto;padding:10px 14px}
  .details table{border-collapse:collapse}
  .details td{padding:2px 12px 2px 0;vertical-align:top}
  .details td.k{color:var(--muted);white-space:nowrap}
  .details .raw{margin-top:8px;padding:8px;background:#0c0e12;border-radius:6px;
    white-space:pre-wrap;font-family:ui-monospace,Menlo,Consolas,monospace;
    color:#cbd3e1;max-height:90px;overflow:auto}
  .menu{position:absolute;background:var(--panel2);border:1px solid var(--line);
    border-radius:8px;padding:6px;z-index:20;box-shadow:0 8px 24px rgba(0,0,0,.5);
    display:none;min-width:170px}
  .menu.open{display:block}
  .menu label{display:flex;gap:7px;align-items:center;padding:3px 5px;cursor:pointer}
  .mtitle{font-size:10px;text-transform:uppercase;letter-spacing:.5px;
    color:var(--muted);padding:6px 8px 3px}
  .mi{padding:6px 10px;border-radius:5px;cursor:pointer;white-space:nowrap}
  .mi:hover{background:#2a3550}
  .loading{position:absolute;inset:0;display:flex;align-items:center;
    justify-content:center;color:var(--muted);font-size:14px;
    background:rgba(15,17,21,.6);z-index:8}
  .loading.hidden{display:none}
  .hint{color:var(--muted);font-size:11px;margin:-2px 0 8px}
</style>
</head>
<body>
<div class="app">
  <header>
    <img class="appicon" src="/icon.png" alt="" onerror="this.remove()">
    <h1>EDF&nbsp;Explorer</h1>
    <span class="file" id="fname">…</span>
    <span class="count" id="count"></span>
    <span class="spacer"></span>
    <button class="btn" id="colsBtn">Columns ▾</button>
    <button class="btn" id="expBtn">Export ▾</button>
  </header>
  <div class="tabs" id="tabs" style="display:none"></div>
  <div class="main">
    <aside>
      <h3>Presets</h3>
      <div class="presets">
        <button class="btn sm" data-preset="reset">All</button>
        <button class="btn sm" data-preset="events">Events only</button>
        <button class="btn sm" data-preset="messages">Messages only</button>
        <button class="btn sm" data-preset="clean">Hide setup</button>
      </div>

      <h3>Saved presets</h3>
      <div class="saved" id="savedPresets"></div>
      <button class="btn sm" id="savePreset" style="width:100%">Save current…</button>

      <h3>Categories</h3>
      <div id="groups"></div>

      <h3>Message kinds</h3>
      <div id="kinds"></div>

      <h3>Message filter</h3>
      <input type="text" id="msgInc" placeholder="message contains…">
      <input type="text" id="msgExc" placeholder="message excludes…">
      <label class="chk"><input type="checkbox" id="msgRe"> regex</label>

      <h3>Eye</h3>
      <select id="eyeSel">
        <option value="">All</option><option value="R">Right</option>
        <option value="L">Left</option>
      </select>

      <h3>Timestamps</h3>
      <select id="tsMode">
        <option value="abs">Absolute</option>
        <option value="rel">Relative to file start</option>
      </select>

      <h3>Time range</h3>
      <div class="row2">
        <input type="number" id="tmin" placeholder="min">
        <input type="number" id="tmax" placeholder="max">
      </div>
      <div class="hint" id="trange"></div>

      <h3>Min duration (ms)</h3>
      <div class="row2">
        <input type="number" id="durFix" placeholder="fixation">
        <input type="number" id="durSacc" placeholder="saccade">
      </div>

      <h3>Global search</h3>
      <input type="text" id="search" placeholder="search raw line…">
      <label class="chk"><input type="checkbox" id="searchRe"> regex</label>
    </aside>

    <div class="tablewrap">
      <div class="scroll" id="scroll">
        <div class="thead" id="thead"></div>
        <div class="vport" id="vport"></div>
        <div class="loading" id="loading">Loading…</div>
      </div>
      <div class="details" id="details"></div>
    </div>
    <div class="menu" id="colmenu" style="right:14px;top:46px"></div>
    <div class="menu" id="expmenu" style="right:14px;top:46px">
      <div class="mtitle">This file</div>
      <div class="mi" data-scope="one" data-fmt="asc">ASC (.asc)</div>
      <div class="mi" data-scope="one" data-fmt="csv">CSV (.csv)</div>
      <div class="mi" data-scope="one" data-fmt="tsv">TSV (.tsv)</div>
      <div class="mtitle" id="allTitle">All loaded files (ZIP)</div>
      <div class="mi" data-scope="all" data-fmt="asc">ASC × all</div>
      <div class="mi" data-scope="all" data-fmt="csv">CSV × all</div>
      <div class="mi" data-scope="all" data-fmt="tsv">TSV × all</div>
    </div>
  </div>
</div>

<script>
const ROWH = 24, BUF = 12;
const GROUP_COLOR = {PREAMBLE:'#6b7280',HEADER:'#a78bfa',END:'#f472b6',
  INPUT:'#fbbf24',MSG:'#5b9dff',FIX:'#36c08f',SACC:'#ff8a5b',BLINK:'#e879f9'};
const C = {idx:0,cat:1,grp:2,mkind:3,start:4,end:5,dur:6,eye:7,x1:8,y1:9,
  x2:10,y2:11,amp:12,vel:13,pupil:14,resx:15,resy:16,val:17,raw:18};
const GROUPS=['PREAMBLE','HEADER','END','INPUT','MSG','FIX','SACC','BLINK'];
const KINDS=['experiment','config','cal','draw'];

let REL=false, TREF=0;
function tdisp(v){ return (REL && typeof v==='number') ? v-TREF : v; }

const COLS = [
  {k:'idx',  label:'#',     w:64,  num:true,  get:r=>r[C.idx]},
  {k:'cat',  label:'Type',  w:92,  badge:true,get:r=>r[C.cat]},
  {k:'start',label:'Start', w:100, num:true,  get:r=>tdisp(r[C.start])},
  {k:'end',  label:'End',   w:100, num:true,  get:r=>tdisp(r[C.end])},
  {k:'dur',  label:'Dur',   w:58,  num:true,  get:r=>r[C.dur]},
  {k:'eye',  label:'Eye',   w:46,            get:r=>r[C.eye]},
  {k:'x1',   label:'X1',    w:74,  num:true,  get:r=>r[C.x1]},
  {k:'y1',   label:'Y1',    w:74,  num:true,  get:r=>r[C.y1]},
  {k:'x2',   label:'X2',    w:74,  num:true,  get:r=>r[C.x2]},
  {k:'y2',   label:'Y2',    w:74,  num:true,  get:r=>r[C.y2]},
  {k:'amp',  label:'Amp',   w:74,  num:true,  get:r=>r[C.amp]},
  {k:'vel',  label:'PVel',  w:62,  num:true,  get:r=>r[C.vel]},
  {k:'pupil',label:'Pupil', w:64,  num:true,  get:r=>r[C.pupil]},
  {k:'resx', label:'ResX',  w:62,  num:true,  get:r=>r[C.resx], hidden:true},
  {k:'resy', label:'ResY',  w:62,  num:true,  get:r=>r[C.resy], hidden:true},
  {k:'val',  label:'In',    w:44,  num:true,  get:r=>r[C.val],  hidden:true},
  {k:'msg',  label:'Message / Detail', w:680, msg:true, get:msgCell},
];

function msgCell(r){
  const g=r[C.grp], raw=r[C.raw];
  if(g==='MSG'){ const b=raw.replace(/^MSG\t\d+ ?/,''); return b.split('\n')[0]
      + (b.indexOf('\n')>=0?'  ⏎':''); }
  if(['PREAMBLE','HEADER'].includes(g)) return raw.replace(/\t/g,' ');
  return '';
}

let FILES=[], active=-1, cache={};
let ROWS=[], META=null, view=[];
let sortCol=null, sortDir=1, selIdx=-1;
const F = {groups:new Set(GROUPS), kinds:new Set(KINDS), eye:'', tmin:null,
  tmax:null, durFix:null, durSacc:null, msgInc:'', msgExc:'', msgRe:false,
  search:'', searchRe:false};
const $=id=>document.getElementById(id);

async function boot(){
  buildSidebar(); buildHead(); buildColMenu();
  const res=await fetch('/api/files'); FILES=await res.json();
  buildTabs(); await refreshPresets();
  await loadFile(0);
}

function buildTabs(){
  const el=$('tabs');
  if(FILES.length<=1){ el.style.display='none'; }
  else { el.style.display='flex'; el.innerHTML='';
    FILES.forEach(f=>{ const t=document.createElement('div'); t.className='tab';
      t.textContent=f.name; t.title=f.name; t.dataset.id=f.id;
      t.onclick=()=>loadFile(f.id); el.appendChild(t); }); }
  $('allTitle').textContent=`All loaded files (ZIP × ${FILES.length})`;
}
function markTabs(){
  document.querySelectorAll('.tab').forEach(t=>
    t.classList.toggle('active', +t.dataset.id===active));
}

async function loadFile(id){
  const loading=$('loading');
  loading.classList.remove('hidden'); loading.textContent='Loading '+FILES[id].name+'…';
  if(!cache[id]){
    const res=await fetch('/api/rows?file='+id);
    if(!res.ok){
      const msg=await res.text();
      loading.style.whiteSpace='pre-wrap'; loading.style.padding='16px';
      loading.style.textAlign='left'; loading.style.fontFamily='monospace';
      loading.textContent=msg;
      return;
    }
    cache[id]=await res.json();
  }
  active=id; ROWS=cache[id].rows; META=cache[id].meta; TREF=META.tmin;
  selIdx=-1; $('details').classList.remove('open');
  $('fname').textContent=META.filename;
  updateCounts(); updatePlaceholders(); markTabs();
  loading.classList.add('hidden');
  apply();
}

function buildSidebar(){
  const gel=$('groups'); gel.innerHTML='';
  GROUPS.forEach(g=>{
    const d=document.createElement('label'); d.className='grp';
    d.innerHTML=`<input type="checkbox" checked>
      <span class="swatch" style="background:${GROUP_COLOR[g]}"></span>
      <span>${g}</span><span class="c" data-grp="${g}"></span>`;
    d.querySelector('input').onchange=e=>{
      e.target.checked?F.groups.add(g):F.groups.delete(g); apply();};
    gel.appendChild(d);
  });
  const kel=$('kinds'); kel.innerHTML='';
  KINDS.forEach(k=>{
    const d=document.createElement('label'); d.className='grp sub';
    d.innerHTML=`<input type="checkbox" checked><span>${k}</span>
      <span class="c" data-kind="${k}"></span>`;
    d.querySelector('input').onchange=e=>{
      e.target.checked?F.kinds.add(k):F.kinds.delete(k); apply();};
    kel.appendChild(d);
  });
}
function updateCounts(){
  GROUPS.forEach(g=>{ const s=document.querySelector(`[data-grp="${g}"]`);
    if(s) s.textContent=(META.group_counts[g]||0).toLocaleString();});
  KINDS.forEach(k=>{ const s=document.querySelector(`[data-kind="${k}"]`);
    if(s) s.textContent=(META.kind_counts[k]||0).toLocaleString();});
}
function updatePlaceholders(){
  if(REL){ $('tmin').placeholder='0'; $('tmax').placeholder=(META.tmax-META.tmin);
    $('trange').textContent=`data: 0 – ${META.tmax-META.tmin} (relative)`; }
  else { $('tmin').placeholder=META.tmin; $('tmax').placeholder=META.tmax;
    $('trange').textContent=`data: ${META.tmin} – ${META.tmax}`; }
}

function buildHead(){
  const el=$('thead'); el.innerHTML='';
  COLS.forEach((c,i)=>{
    if(c.hidden) return;
    const d=document.createElement('div'); d.className='th'; d.style.width=c.w+'px';
    d.textContent=c.label;
    if(sortCol===i) d.innerHTML=c.label+`<span class="ar">${sortDir>0?'▲':'▼'}</span>`;
    d.onclick=()=>{ sortDir = (sortCol===i)? -sortDir : 1; sortCol=i; apply(); };
    el.appendChild(d);
  });
}
function buildColMenu(){
  const m=$('colmenu'); m.innerHTML='';
  COLS.forEach((c,i)=>{
    const l=document.createElement('label');
    l.innerHTML=`<input type="checkbox" ${c.hidden?'':'checked'}> ${c.label}`;
    l.querySelector('input').onchange=e=>{ c.hidden=!e.target.checked; buildHead(); render(); };
    m.appendChild(l);
  });
}

function makeMatcher(str, isRe){
  if(!str) return null;
  if(isRe){ try{ const re=new RegExp(str,'i'); return s=>re.test(s);}catch(e){ return null;} }
  const q=str.toLowerCase(); return s=>s.toLowerCase().includes(q);
}

function apply(){
  if(!META) return;
  const incM=makeMatcher(F.msgInc,F.msgRe), excM=makeMatcher(F.msgExc,F.msgRe);
  const srch=makeMatcher(F.search,F.searchRe);
  let tlo=F.tmin, thi=F.tmax;
  if(REL){ if(tlo!==null) tlo+=TREF; if(thi!==null) thi+=TREF; }
  const eye=F.eye;
  view=[];
  for(let i=0;i<ROWS.length;i++){
    const r=ROWS[i], g=r[C.grp];
    if(!F.groups.has(g)) continue;
    if(g==='MSG' && !F.kinds.has(r[C.mkind]||'experiment')) continue;
    if(eye && r[C.eye] && r[C.eye]!==eye) continue;
    const t=r[C.start];
    if(tlo!==null && typeof t==='number' && t<tlo) continue;
    if(thi!==null && typeof t==='number' && t>thi) continue;
    const dv=r[C.dur];
    if(g==='FIX' && F.durFix!==null && typeof dv==='number' && dv<F.durFix) continue;
    if(g==='SACC' && F.durSacc!==null && typeof dv==='number' && dv<F.durSacc) continue;
    if(g==='MSG'){
      const body=r[C.raw].replace(/^MSG\t\d+ ?/,'');
      if(incM && !incM(body)) continue;
      if(excM && excM(body)) continue;
    }
    if(srch && !srch(r[C.raw])) continue;
    view.push(r);
  }
  if(sortCol!==null){
    const col=COLS[sortCol];
    view.sort((a,b)=>{
      let x=col.get(a), y=col.get(b);
      if(col.num){ x=parseFloat(x); y=parseFloat(y);
        if(isNaN(x)) x=-Infinity; if(isNaN(y)) y=-Infinity; }
      else { x=(''+x).toLowerCase(); y=(''+y).toLowerCase(); }
      return x<y?-sortDir : x>y?sortDir : 0;
    });
  }
  $('count').textContent =
    `${view.length.toLocaleString()} of ${ROWS.length.toLocaleString()} lines`;
  buildHead(); layout(); render();
}

const vport=$('vport'), scroll=$('scroll');
function rowWidth(){ return COLS.reduce((s,c)=>s+(c.hidden?0:c.w),0); }
function layout(){
  vport.style.height=(view.length*ROWH)+'px';
  vport.style.width=rowWidth()+'px';
  $('thead').style.width=rowWidth()+'px';
}
function render(){
  const top=scroll.scrollTop, h=scroll.clientHeight;
  let first=Math.max(0,Math.floor(top/ROWH)-BUF);
  let last=Math.min(view.length,Math.ceil((top+h)/ROWH)+BUF);
  let html='';
  for(let i=first;i<last;i++){
    const r=view[i];
    html+=`<div class="tr${r[C.idx]===selIdx?' sel':''}" style="top:${i*ROWH}px;width:${rowWidth()}px" data-i="${r[C.idx]}">`;
    for(const c of COLS){
      if(c.hidden) continue;
      let v=c.get(r); if(v===''||v===undefined||v===null) v='';
      if(c.badge){
        const col=GROUP_COLOR[r[C.grp]]||'#888';
        html+=`<div class="td" style="width:${c.w}px"><span class="badge" style="background:${col}">${v}</span></div>`;
      } else {
        const cls='td'+(c.msg?' msg':'');
        html+=`<div class="${cls}" style="width:${c.w}px">${escapeHtml(''+v)}</div>`;
      }
    }
    html+='</div>';
  }
  vport.innerHTML=html;
}
function escapeHtml(s){return s.replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}

scroll.addEventListener('scroll',render);
window.addEventListener('resize',render);

vport.addEventListener('click',e=>{
  const tr=e.target.closest('.tr'); if(!tr) return;
  selIdx=parseInt(tr.dataset.i,10); showDetails(selIdx); render();
});
function showDetails(idx){
  const r=ROWS[idx]; const det=$('details');
  const fields=[['Index',r[C.idx]],['Type',r[C.cat]],['Group',r[C.grp]],
    ['Msg kind',r[C.mkind]],['Start',tdisp(r[C.start])],['End',tdisp(r[C.end])],
    ['Dur',r[C.dur]],['Eye',r[C.eye]],['X1',r[C.x1]],['Y1',r[C.y1]],
    ['X2',r[C.x2]],['Y2',r[C.y2]],['Amplitude',r[C.amp]],['Peak vel',r[C.vel]],
    ['Pupil',r[C.pupil]],['Res X',r[C.resx]],['Res Y',r[C.resy]],['Input',r[C.val]]];
  let t='<table>';
  for(const [k,v] of fields){ if(v!==''&&v!==undefined&&v!==null)
    t+=`<tr><td class="k">${k}</td><td>${escapeHtml(''+v)}</td></tr>`; }
  t+='</table>';
  const raw=r[C.raw].replace(/\t/g,'\u2192').replace(/\n/g,'\u21b5\n');
  det.innerHTML=t+`<div class="raw">${escapeHtml(raw)}</div>`;
  det.classList.add('open');
}

function deb(fn,ms){let h;return(...a)=>{clearTimeout(h);h=setTimeout(()=>fn(...a),ms);};}
const onType=deb(()=>{
  F.msgInc=$('msgInc').value; F.msgExc=$('msgExc').value; F.search=$('search').value; apply(); },180);
['msgInc','msgExc','search'].forEach(id=>$(id).addEventListener('input',onType));
$('msgRe').onchange=e=>{F.msgRe=e.target.checked;apply();};
$('searchRe').onchange=e=>{F.searchRe=e.target.checked;apply();};
$('eyeSel').onchange=e=>{F.eye=e.target.value;apply();};
$('tsMode').onchange=e=>{ REL=e.target.value==='rel'; updatePlaceholders(); apply(); };
const onNum=deb(()=>{
  F.tmin=$('tmin').value===''?null:+$('tmin').value;
  F.tmax=$('tmax').value===''?null:+$('tmax').value;
  F.durFix=$('durFix').value===''?null:+$('durFix').value;
  F.durSacc=$('durSacc').value===''?null:+$('durSacc').value; apply(); },180);
['tmin','tmax','durFix','durSacc'].forEach(id=>$(id).addEventListener('input',onNum));

// ---- built-in presets ----
document.querySelectorAll('[data-preset]').forEach(b=>b.onclick=()=>{
  const p=b.dataset.preset;
  const setGroups=(arr)=>{F.groups=new Set(arr);
    document.querySelectorAll('#groups input').forEach((c,i)=>c.checked=arr.includes(GROUPS[i]));};
  if(p==='reset'){ setGroups(GROUPS.slice());
    F.kinds=new Set(KINDS); document.querySelectorAll('#kinds input').forEach(c=>c.checked=true);
    F.msgInc=F.msgExc=F.search=''; $('msgInc').value=$('msgExc').value=$('search').value='';
    F.eye='';$('eyeSel').value=''; F.tmin=F.tmax=null; F.durFix=F.durSacc=null;
    $('tmin').value=$('tmax').value=$('durFix').value=$('durSacc').value=''; }
  if(p==='events') setGroups(['FIX','SACC','BLINK']);
  if(p==='messages') setGroups(['MSG']);
  if(p==='clean') setGroups(['HEADER','END','INPUT','MSG','FIX','SACC','BLINK']);
  apply();
});

// ---- saved presets (persisted on disk) ----
function currentConfig(){
  return {groups:[...F.groups], kinds:[...F.kinds], eye:F.eye,
    tmin:F.tmin, tmax:F.tmax, durFix:F.durFix, durSacc:F.durSacc,
    msgInc:F.msgInc, msgExc:F.msgExc, msgRe:F.msgRe,
    search:F.search, searchRe:F.searchRe, rel:REL,
    cols:COLS.filter(c=>!c.hidden).map(c=>c.k)};
}
function applyConfig(cfg){
  F.groups=new Set(cfg.groups||GROUPS);
  F.kinds=new Set(cfg.kinds||KINDS);
  document.querySelectorAll('#groups input').forEach((c,i)=>c.checked=F.groups.has(GROUPS[i]));
  document.querySelectorAll('#kinds input').forEach((c,i)=>c.checked=F.kinds.has(KINDS[i]));
  F.eye=cfg.eye||''; $('eyeSel').value=F.eye;
  F.tmin=cfg.tmin??null; F.tmax=cfg.tmax??null;
  F.durFix=cfg.durFix??null; F.durSacc=cfg.durSacc??null;
  $('tmin').value=F.tmin??''; $('tmax').value=F.tmax??'';
  $('durFix').value=F.durFix??''; $('durSacc').value=F.durSacc??'';
  F.msgInc=cfg.msgInc||''; F.msgExc=cfg.msgExc||''; F.search=cfg.search||'';
  $('msgInc').value=F.msgInc; $('msgExc').value=F.msgExc; $('search').value=F.search;
  F.msgRe=!!cfg.msgRe; F.searchRe=!!cfg.searchRe;
  $('msgRe').checked=F.msgRe; $('searchRe').checked=F.searchRe;
  REL=!!cfg.rel; $('tsMode').value=REL?'rel':'abs';
  if(cfg.cols){ COLS.forEach(c=>c.hidden=!cfg.cols.includes(c.k));
    buildColMenu(); }
  if(META) updatePlaceholders();
  apply();
}
async function refreshPresets(){
  const res=await fetch('/api/presets'); const data=await res.json();
  const el=$('savedPresets'); el.innerHTML='';
  const names=Object.keys(data);
  if(!names.length){ el.innerHTML='<div class="empty">none saved yet</div>'; return; }
  names.forEach(n=>{
    const d=document.createElement('div'); d.className='pri';
    d.innerHTML=`<span class="nm">${escapeHtml(n)}</span><span class="del" title="delete">✕</span>`;
    d.querySelector('.nm').onclick=()=>applyConfig(data[n]);
    d.querySelector('.del').onclick=async(e)=>{ e.stopPropagation();
      await fetch('/api/presets',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'delete',name:n})}); refreshPresets(); };
    el.appendChild(d);
  });
}
$('savePreset').onclick=async()=>{
  const name=prompt('Save current filters as preset:'); if(!name) return;
  await fetch('/api/presets',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'save',name:name.trim(),config:currentConfig()})});
  refreshPresets();
};

// ---- menus ----
function closeMenus(except){ ['colmenu','expmenu'].forEach(id=>{ if(id!==except) $(id).classList.remove('open'); }); }
$('colsBtn').onclick=(e)=>{ e.stopPropagation(); closeMenus('colmenu'); $('colmenu').classList.toggle('open'); };
$('expBtn').onclick=(e)=>{ e.stopPropagation(); closeMenus('expmenu'); $('expmenu').classList.toggle('open'); };
document.addEventListener('click',e=>{
  if(!e.target.closest('#colmenu')&&!e.target.closest('#colsBtn')) $('colmenu').classList.remove('open');
  if(!e.target.closest('#expmenu')&&!e.target.closest('#expBtn')) $('expmenu').classList.remove('open');
});

// ---- export ----
function currentOpts(){
  let lo=F.tmin, hi=F.tmax;
  if(REL){ if(lo!==null) lo+=TREF; if(hi!==null) hi+=TREF; }
  return {groups:[...F.groups], kinds:[...F.kinds], eye:F.eye,
    tmin:lo, tmax:hi, min_fix:F.durFix, min_sacc:F.durSacc,
    contains:F.msgInc, exclude:F.msgExc, contains_regex:F.msgRe,
    search:F.search, search_regex:F.searchRe};
}
function download(blob, fallback){
  const cd=blob.cd||''; const m=cd.match(/filename="(.+?)"/);
  const name=m?m[1]:fallback;
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob.data);
  a.download=name; a.click(); URL.revokeObjectURL(a.href);
}
async function post(url, body){
  const res=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  return {data:await res.blob(), cd:res.headers.get('Content-Disposition')||''};
}
async function exportOne(fmt){
  const indices=view.map(r=>r[C.idx]);
  download(await post('/api/export',{file:active,indices,format:fmt,relative:REL}),
    'export.'+fmt);
}
async function exportAll(fmt){
  download(await post('/api/export_all',{opts:currentOpts(),format:fmt,relative:REL}),
    'edf_export_'+fmt+'.zip');
}
$('expmenu').querySelectorAll('.mi').forEach(mi=>mi.onclick=()=>{
  const fmt=mi.dataset.fmt;
  if(mi.dataset.scope==='one') exportOne(fmt); else exportAll(fmt);
  $('expmenu').classList.remove('open');
});

boot();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()