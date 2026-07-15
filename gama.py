#!/usr/bin/env python3
r"""
Caleb De Odorico
gama.py -- Easy EyeLinplorer and Exporter
================================================

A single-file tool that opens SR Research EyeLink ``.EDF`` recordings and lets
one explore their converted ASC representation in a fast, spreadsheet-like web
UI, or filter/convert/export them from the command line.  Using a byte-identical
EDF->ASC engine, Gama allows the user to explore and manipulate recordings.

Run with no arguments to choose file(s) with a dialog::

    python gama.py                     # file picker
    python gama.py a.EDF b.EDF         # open several files (one tab each)

Head-less / batch::

    python gama.py rec.EDF --stats
    python gama.py rec.EDF --export events.asc --only FIX,SACC,BLINK
    python gama.py *.EDF   --export out_dir --format csv --relative

Note to anyone trying to improve on this: Please spare yourselves the trouble.
SR Research has seen to it that these files remain annoying to parse. I imagine
in a pursuit to sell their own software. I hate that, with a passion. Science
is about openly sharing information with one-another. I understand that in a
capitalist society one must create capital to survive. I hate this. In order
to be a human, one must create wonder, passion, creativity, being. These aren't
valued under capitalism. I hate capitalism. I am wasting precious compiler space
to tell you this. Find wonder in the world and keep it from those who seek to
exploit it. Be free and be good.
"""

import argparse
import os
import sys
import ctypes as C
from decimal import Decimal, ROUND_HALF_UP

__version__ = "1.0.0"


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


def export_html(parsed, rows, indices, relative=False, tref=0, title="EDF view"):
    """A standalone, self-contained HTML copy of the current view (printable)."""
    head = "".join(f"<th>{_esc(c)}</th>" for c in EXPORT_COLUMNS)
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
        out.append(f"<tr>{tds}</tr>")
    sub = (f"{len(out):,} rows &middot; times "
           f"{'relative to file start' if relative else 'absolute'} &middot; "
           f"exported by gama {__version__}")
    doc = _HTML_DOC.format(title=_esc(title), sub=sub, head=head,
                           rows="\n".join(out))
    return doc.encode("utf-8"), "text/html"


def export_bytes(entry, indices, fmt, relative):
    if fmt == "asc":
        body, _ = export_asc(entry.records, indices)
        return body, ".asc"
    if fmt == "tsv":
        body, _ = export_table(entry.parsed, entry.rows, indices, "\t",
                               relative, entry.tmin)
        return body, ".tsv"
    if fmt == "html":
        body, _ = export_html(entry.parsed, entry.rows, indices, relative,
                              entry.tmin, entry.name)
        return body, ".html"
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
            global LAST_ERROR
            entry.error = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__))
            LAST_ERROR = f"{entry.name}: {entry.error.strip().splitlines()[-1]}"
            # Recorded once; the browser is shown the message instead of the
            # request being retried forever.
            print(f"\nERROR parsing {entry.name}:\n{entry.error}", flush=True)


LAST_ERROR = None


def _edfapi_version():
    try:
        E = _load_edfapi()
        v = E.edf_get_version()
        return v.decode("latin-1", "replace") if isinstance(v, bytes) else str(v)
    except BaseException as exc:
        return f"unavailable ({exc!r})"


def diagnostics(presets_dir):
    """Everything worth pasting into a bug report."""
    import platform
    try:
        import eyelinkio
        elio = getattr(eyelinkio, "__version__", "?")
    except Exception:
        elio = "not importable"
    return {
        "gama": __version__,
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "eyelinkio": elio,
        "edfapi": _edfapi_version(),
        "base_dir": BASE_DIR,
        "presets_dir": presets_dir,
        "ui": "index.html" if _resource_bytes(HTML_FILENAME) else "MISSING",
        "last_error": LAST_ERROR,
    }


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


def make_handler(reg, converted_from_line, presets_dir):
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
                return int(q.get("file", ["0"])[0])
            except ValueError:
                return 0

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                self._send(200, html_page().encode("utf-8"),
                           "text/html; charset=utf-8")
            elif route == "/icon.png" or route == "/favicon.ico":
                if ICON_BYTES:
                    self._send(200, ICON_BYTES, "image/png",
                               {"Cache-Control": "max-age=86400"})
                else:
                    self._send(404, b"", "text/plain")
            elif route == "/api/info":
                self._json(diagnostics(presets_dir))
            elif route == "/api/files":
                self._json(reg.listing())
            elif route == "/api/browse":
                q = parse_qs(urlparse(self.path).query)
                self._json(browse_dir(q.get("path", [""])[0]))
            elif route == "/api/presets":
                self._json(_load_presets(presets_dir))
            elif route == "/api/rows":
                entry = reg.get(self._file_arg())
                if entry is None:
                    self._send(404, b"file not open", "text/plain")
                    return
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
            elif route == "/api/open":
                self._open_files()
            elif route == "/api/close":
                self._close_file()
            else:
                self._send(404, b"not found", "text/plain")

        def _open_files(self):
            req = self._body()
            added, skipped = [], []
            for p in req.get("paths", []):
                if os.path.isfile(p) and _is_edf(p):
                    added.append(reg.add(p))
                else:
                    skipped.append(p)
            self._json({"files": reg.listing(), "added": added,
                        "skipped": skipped})

        def _close_file(self):
            req = self._body()
            reg.close(req.get("file"))
            self._json({"files": reg.listing()})

        def _export_one(self):
            req = self._body()
            entry = reg.get(req.get("file"))
            if entry is None:
                self._send(404, b"file not open", "text/plain")
                return
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
                for entry in reg.entries():
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
    reg = Registry()
    for p in paths:
        reg.add(p)
    try:
        os.makedirs(presets_dir, exist_ok=True)
    except OSError:
        pass
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port),
        make_handler(reg, converted_from_line, presets_dir))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    n = len(paths)
    print(f"Gama: {n} file(s) preloaded"
          if n else "Gama: add files from the browser (+ tab)")
    print(f"Presets folder: {presets_dir}")
    print(f"Running at {url}\nPress Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


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
    ap.add_argument("--converted-from-line", default=em.DEFAULT_CONVERTED_FROM,
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


# ---------------------------------------------------------------------------
# Single-page web UI.
#
# The UI lives in index.html next to this file.  It is bundled into the
# executable by gama.spec, so a frozen build stays a single file; when running
# from source it is re-read on every request, so you can edit the HTML and just
# refresh the browser.
# ---------------------------------------------------------------------------
HTML_FILENAME = "index.html"
_HTML_CACHE = None

_HTML_MISSING = """<!DOCTYPE html><html><body style="font:14px sans-serif;
padding:2em;background:#0f1115;color:#e6e9ef">
<h2>index.html not found</h2>
<p>gama's user interface lives in <code>{name}</code>, which should sit next to
<code>gama.py</code>. Looked in:</p><pre>{where}</pre>
</body></html>"""


def html_page():
    """The UI markup: cached when frozen, re-read from disk when developing."""
    global _HTML_CACHE
    if _HTML_CACHE is not None:
        return _HTML_CACHE
    data = _resource_bytes(HTML_FILENAME)
    if data is None:
        looked = "\n".join(
            os.path.join(b, HTML_FILENAME)
            for b in (getattr(sys, "_MEIPASS", None), BASE_DIR) if b)
        return _HTML_MISSING.format(name=HTML_FILENAME, where=looked)
    text = data.decode("utf-8")
    if getattr(sys, "frozen", False):
        _HTML_CACHE = text          # bundled copy can never change at runtime
    return text



if __name__ == "__main__":
    main()