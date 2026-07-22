"""The EDF -> ASC converter: byte-identical to SR Research's edf2asc.

Every quirk reproduced here was worked out by comparing against real
edf2asc output, so treat changes with suspicion: the test for this module
is that a full export still matches the reference file byte for byte.
The conversion is driven entirely by the official edfapi shared library.
"""

import argparse
import os
import ctypes as C
from decimal import Decimal, ROUND_HALF_UP

from .edfapi import (_load_edfapi, STARTBLINK, ENDBLINK, STARTSACC,
                     ENDSACC, STARTFIX, ENDFIX, MESSAGEEVENT, BUTTONEVENT,
                     INPUTEVENT, RECORDING_INFO, NO_PENDING_ITEMS,
                     EYE_LETTER)
from .version import __version__


DEFAULT_CONVERTED_FROM = (
    "** CONVERTED FROM GAMA " + __version__
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
