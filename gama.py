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
def _read_edf(edf_path, progress=None):
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
    seen = 0
    try:
        while True:
            t = E.edf_get_next_data(ef)
            if t == NO_PENDING_ITEMS:
                break
            fd = E.edf_get_float_data(ef)
            seen += 1
            if progress is not None and (seen & 0x3FFF) == 0:  # every ~16k items
                progress(seen)

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

    if progress is not None:
        progress(seen)
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


def build_records(edf_path, converted_from_line=DEFAULT_CONVERTED_FROM,
                  progress=None):
    """Parse an EDF and return (records, blocks).

    ``records`` is the full ordered list of output-line records (see _render);
    joining their ``text`` with CRLF reproduces the byte-identical ASC.  This is
    the entry point used by the interactive explorer.
    """
    preamble, elements, blocks = _read_edf(edf_path, progress)
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
                "val", "raw", "block"]
I_IDX, I_CAT, I_GRP, I_MK, I_START, I_END, I_DUR, I_EYE = 0, 1, 2, 3, 4, 5, 6, 7
I_RAW = 18

EXPORT_COLUMNS = ["idx", "category", "group", "msg_kind", "start", "end",
                  "dur", "eye", "x1", "y1", "x2", "y2", "amp", "pvel", "pupil",
                  "res_x", "res_y", "input", "message"]

DEFAULT_PRESETS_DIR = os.path.join(BASE_DIR, "presets")
_PRESETS_LOCK = threading.Lock()
_NOTES_LOCK = threading.Lock()


def _notes_path(edf_path):
    """Sidecar file for a recording's row flags/notes, next to the .EDF."""
    return edf_path + ".gama-notes.json"


def load_notes(edf_path):
    """Return {line_index(str): {"flag": bool, "note": str}} for a recording."""
    p = _notes_path(edf_path)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        notes = data.get("notes", data)          # tolerate a bare mapping
        out = {}
        for k, v in notes.items():
            if isinstance(v, dict):
                out[str(k)] = {"flag": bool(v.get("flag")),
                               "note": str(v.get("note") or "")}
        return out
    except (OSError, ValueError):
        return {}


def save_notes(edf_path, notes):
    """Write the notes sidecar; drop empty entries; remove the file if none."""
    clean = {}
    for k, v in (notes or {}).items():
        flag = bool(v.get("flag"))
        note = str(v.get("note") or "").strip()
        if flag or note:
            clean[str(k)] = {"flag": flag, "note": note}
    p = _notes_path(edf_path)
    with _NOTES_LOCK:
        try:
            if not clean:
                if os.path.isfile(p):
                    os.remove(p)
                return {}
            payload = {"tool": "gama", "version": __version__,
                       "source_file": os.path.basename(edf_path),
                       "notes": clean}
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, p)
        except OSError:
            pass
    return clean


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
    records, _blocks = em.build_records(edf_path, converted_from_line, progress)
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
        "edfapi": _edfapi_version(),
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
    return em.records_to_bytes(subset), "text/plain"


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


# ---------------------------------------------------------------------------
# Presets on disk -- one JSON file per preset inside a "presets" folder.
# ---------------------------------------------------------------------------
def _preset_slug(name):
    slug = re.sub(r"[^\w.-]+", "_", name).strip("_")
    return slug or "preset"


def _schemes_dir(presets_dir):
    """Trial/AOI schemes live beside the presets folder."""
    return os.path.join(os.path.dirname(os.path.abspath(presets_dir)), "schemes")


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

    def start(self, folder, recursive):
        folder = os.path.abspath(folder)
        with self._lock:
            self.folder = folder
            self.recursive = bool(recursive)
            # seed known set with whatever is already open / present so we only
            # auto-open files that appear AFTER watching begins
            self._known = {}
            self._pending = {}
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self.folder = None
        self._stop.set()

    def _run(self):
        # first pass: record existing files as "known" without opening them
        with self._lock:
            folder, recursive = self.folder, self.recursive
        if folder:
            for p in list_edfs(folder, recursive):
                try:
                    self._known[p] = os.path.getsize(p)
                except OSError:
                    self._known[p] = 0
        while not self._stop.wait(2.0):
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


def make_handler(reg, converted_from_line, presets_dir, watcher):
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
            elif route == "/api/watch":
                self._json(watcher.status())
            elif route == "/api/schemes":
                self._json(_load_presets(_schemes_dir(presets_dir)))
            elif route == "/api/presets":
                self._json(_load_presets(presets_dir))
            elif route == "/api/progress":
                entry = reg.get(self._file_arg())
                if entry is None:
                    self._json({"state": "gone"})
                elif entry.error:
                    self._json({"state": "error"})
                elif entry.ready:
                    self._json({"state": "ready", "seen": entry.progress})
                else:
                    # read the plain int without taking entry.lock, which the
                    # parse thread holds for its whole duration
                    self._json({"state": "parsing" if entry.parsing else "queued",
                                "seen": entry.progress})
            elif route == "/api/notes":
                entry = reg.get(self._file_arg())
                if entry is None:
                    self._send(404, b"file not open", "text/plain")
                    return
                _ensure_parsed(entry, converted_from_line)
                self._json({"notes": entry.notes})
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
            elif route == "/api/open_folder":
                self._open_folder()
            elif route == "/api/watch":
                self._watch()
            elif route == "/api/close":
                self._close_file()
            elif route == "/api/notes":
                self._write_note()
            elif route == "/api/schemes":
                self._schemes_write()
            elif route == "/api/trials/suggest":
                self._trials_suggest()
            elif route == "/api/trials/run":
                self._trials_run()
            elif route == "/api/trials/export":
                self._trials_export()
            else:
                self._send(404, b"not found", "text/plain")

        # ---- trial / AOI analysis -----------------------------------
        def _entry_for(self, req):
            entry = reg.get(req.get("file"))
            if entry is None:
                self._send(404, b"file not open", "text/plain")
                return None
            _ensure_parsed(entry, converted_from_line)
            if entry.error:
                self._send(500, entry.error.encode("utf-8"), "text/plain")
                return None
            return entry

        def _trials_suggest(self):
            req = self._body()
            entry = self._entry_for(req)
            if entry is None:
                return
            self._json(suggest_markers(entry.rows, entry.parsed))

        def _trials_run(self):
            req = self._body()
            entry = self._entry_for(req)
            if entry is None:
                return
            res = analyse_trials(entry.rows, entry.parsed, req.get("scheme") or {},
                                 req.get("limit"))
            if not req.get("preview") and "row_trial" in res:
                entry.trials = {"row_trial": res["row_trial"],
                                "row_aoi": res["row_aoi"],
                                "row_aoi_from": res["row_aoi_from"]}
            if req.get("preview"):
                res.pop("row_trial", None)
                res.pop("row_aoi", None)
                res.pop("row_aoi_from", None)
                res["summary"]["rows"] = res["summary"]["rows"][:25]
            body = gzip.compress(json.dumps(res).encode("utf-8"), 5)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _trials_export(self):
            req = self._body()
            entry = self._entry_for(req)
            if entry is None:
                return
            res = analyse_trials(entry.rows, entry.parsed, req.get("scheme") or {})
            fmt = req.get("format", "csv")
            cols, srows = res["summary"]["columns"], res["summary"]["rows"]
            if fmt == "html":
                head = "".join("<th>%s</th>" % _esc(c) for c in cols)
                body_rows = "\n".join(
                    "<tr>" + "".join("<td>%s</td>" % _esc(v) for v in r) + "</tr>"
                    for r in srows)
                sub = ("%d trials &middot; exported by gama %s"
                       % (len(srows), __version__))
                body = _HTML_DOC.format(title=_esc(entry.name + " — trials"),
                                        sub=sub, head=head,
                                        rows=body_rows).encode("utf-8")
                ext = ".html"
            else:
                import csv as _csv
                buf = StringIO()
                w = _csv.writer(buf, delimiter=("\t" if fmt == "tsv" else ","),
                                lineterminator="\n")
                w.writerow(cols)
                for r in srows:
                    w.writerow(r)
                body = buf.getvalue().encode("utf-8")
                ext = ".tsv" if fmt == "tsv" else ".csv"
            self._send(200, body, "application/octet-stream",
                       {"Content-Disposition":
                        'attachment; filename="%s_trials%s"' % (entry.base, ext)})

        def _schemes_write(self):
            req = self._body()
            d = _schemes_dir(presets_dir)
            name = (req.get("name") or "").strip()
            if req.get("action") == "delete":
                _delete_preset(d, name)
            elif name:
                _save_preset(d, name, req.get("scheme") or {})
            self._json(_load_presets(d))

        def _write_note(self):
            req = self._body()
            entry = reg.get(req.get("file"))
            if entry is None:
                self._send(404, b"file not open", "text/plain")
                return
            _ensure_parsed(entry, converted_from_line)
            line = str(req.get("line"))
            flag = bool(req.get("flag"))
            note = str(req.get("note") or "").strip()
            with entry.lock:
                if flag or note:
                    entry.notes[line] = {"flag": flag, "note": note}
                else:
                    entry.notes.pop(line, None)
                entry.notes = save_notes(entry.path, entry.notes)
            self._json({"notes": entry.notes,
                        "path": _notes_path(entry.path)})

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

        def _open_folder(self):
            req = self._body()
            folder = req.get("path") or ""
            recursive = bool(req.get("recursive"))
            paths = list_edfs(folder, recursive)
            added = [reg.add(p) for p in paths]
            self._json({"files": reg.listing(), "added": added,
                        "folder": os.path.abspath(folder) if folder else None,
                        "count": len(added)})

        def _watch(self):
            req = self._body()
            action = req.get("action", "start")
            if action == "stop":
                watcher.stop()
                self._json({"files": reg.listing(), "watch": watcher.status()})
                return
            folder = req.get("path") or ""
            recursive = bool(req.get("recursive"))
            added = []
            if req.get("open_existing", True):
                added = [reg.add(p) for p in list_edfs(folder, recursive)]
            watcher.start(folder, recursive)
            self._json({"files": reg.listing(), "added": added,
                        "watch": watcher.status()})

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
            fmt = req.get("format", "asc")
            relative = req.get("relative", False)
            indices = req.get("indices", [])
            body, ext = export_bytes(entry, indices, fmt, relative)
            # A provenance sidecar is offered for data exports (never ASC).  Since
            # a browser download is a single file, we bundle export + sidecar in a
            # small ZIP when it's requested.
            if req.get("sidecar") and fmt != "asc":
                prov = _provenance_bytes(entry, fmt, req.get("opts", {}),
                                         relative, len(indices))
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr(f"{entry.base}_filtered{ext}", body)
                    z.writestr(f"{entry.base}_filtered.gama.json", prov)
                self._send(200, buf.getvalue(), "application/zip",
                           {"Content-Disposition":
                            f'attachment; filename="{entry.base}_filtered.zip"'})
                return
            self._send(200, body, "application/octet-stream",
                       {"Content-Disposition":
                        f'attachment; filename="{entry.base}_filtered{ext}"'})

        def _export_all(self):
            req = self._body()
            fmt = req.get("format", "asc")
            relative = req.get("relative", False)
            sidecar = bool(req.get("sidecar")) and fmt != "asc"
            opts_json = req.get("opts", {})
            opts = _opts_from_json(opts_json)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for entry in reg.entries():
                    _ensure_parsed(entry, converted_from_line)
                    if entry.error:
                        continue
                    idx = filter_indices(entry.rows, entry.parsed, opts)
                    body, ext = export_bytes(entry, idx, fmt, relative)
                    z.writestr(f"{entry.base}_filtered{ext}", body)
                    if sidecar:
                        z.writestr(f"{entry.base}_filtered.gama.json",
                                   _provenance_bytes(entry, fmt, opts_json,
                                                     relative, len(idx)))
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
    watcher = Watcher(reg)
    try:
        os.makedirs(presets_dir, exist_ok=True)
    except OSError:
        pass
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port),
        make_handler(reg, converted_from_line, presets_dir, watcher))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    n = len(paths)
    print(f"EDF Explorer: {n} file(s) preloaded"
          if n else "EDF Explorer: add files from the browser (+ tab)")
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