"""Cubase's built-in channel EQ, carried to REAPER as a ReaEQ.

Every Cubase channel has a four-band EQ that is not an insert: it sits in
the mixer channel's attribute tree (`EQ/Band[n]/Enable,Type,Gain,Freq,Q`),
after the inserts and before the fader. Nothing of it used to cross, and a
darbuka with a -23 dB low shelf arrived 16 dB too loud in the lows.

REAPER's ReaEQ holds the same kind of bands, and its saved state is plain:
after REAPER's VST header, `u32 33, u32 count`, then per band 33 bytes -
`i32 type, i32 enabled, f64 freq, f64 gain (linear), f64 bandwidth
(octaves), u8 1` - and a 32-byte tail (`i32 1, i32 1, f64 master gain 1.0,
f64 0, u32 0, u16 2, u16 1`). Measured by saving a ReaEQ from REAPER's own
API and reading the chunk back. ReaEQ's band types in the file: 0 low
shelf, 1 high shelf, 4 high pass, 8 band (parametric).

Cubase's band types: type 5 on band 1 is a low shelf and on band 4 a high
shelf (measured on the export: -22.8 dB below ~415 Hz, exactly the band's
gain and frequency); the middle bands are parametric. The other type
numbers (Cubase offers Parametric I/II, Shelf I-IV, Cut) are mapped by
position - low band, high band, middle - which is what they are for.
"""
import math
import struct

from . import plugins as P

REAEQ_NUM = 0x72656571          # 'reeq'
REAEQ_IDENT = '56535472656571726561657100000000'   # what REAPER writes for it

LO_SHELF, HI_SHELF, HI_PASS, LO_PASS, BAND = 0, 1, 4, 5, 8
SHELF_BW = 1.2


def read(tree):
    """[(cubase band index, type, gain dB, freq Hz, Q)] of the enabled bands."""
    eq = tree.get('EQ') if tree is not None else None
    if eq is None or not hasattr(eq, 'multi') or eq.get('Bypass'):
        return []
    out = []
    n = 0
    # the four bands come as one 'Band' entry holding a list
    bands = eq.all('Band') if hasattr(eq, 'all') else []
    for b in bands:
        if not hasattr(b, 'get'):
            continue
        if b.get('Enable'):
            try:
                out.append((n, int(b.get('Type') or 0), float(b.get('Gain') or 0.0),
                            float(b.get('Freq') or 1000.0), float(b.get('Q') or 1.0)))
            except (TypeError, ValueError):
                pass
        n += 1
    return out


# The channel EQ is really an internal plug-in, VstCtrlEQ (class
# 297BA567D83144E1AE921DEF07B41156), in the channel strip's slot of type 4;
# its state holds the values Cubase plays by (records as in builtins: name,
# id, f64). The 'EQ' attribute tree is a mirror for the mixer's display and
# keeps Q in another scale (0.0917 where the plug-in has Q 1.0) - writing
# only the mirror changed nothing in Cubase's export.
EQ_UID = '297BA567D83144E1AE921DEF07B41156'
BAND_KEYS = (('on1', 'lftype', 'gainlf', 'freqlf', 'qlf'),
             ('on2', 'p1type', 'gainp1', 'freqp1', 'qp1'),
             ('on3', 'p2type', 'gainp2', 'freqp2', 'qp2'),
             ('on4', 'hftype', 'gainhf', 'freqhf', 'qhf'))


def read_state(recs):
    """[(band index, type, gain dB, freq Hz, Q)] of the bands switched on in
    a VstCtrlEQ state's records (builtins._records)."""
    if not recs or recs.get('bypass', (0, 0.0))[1] >= 0.5:
        return []
    out = []
    for i, (on, ty, g, f, q) in enumerate(BAND_KEYS):
        if on in recs and recs[on][1] >= 0.5:
            out.append((i, int(round(recs[ty][1])), float(recs[g][1]),
                        float(recs[f][1]), float(recs[q][1])))
    return out


def q_to_octaves(qv):
    """Bandwidth in octaves for a parametric Q."""
    qv = max(0.05, float(qv))
    return 2.0 * math.asinh(1.0 / (2.0 * qv)) / math.log(2.0)


def reaeq_bands(bands):
    """Cubase bands -> ReaEQ (type, enabled, freq, gain_lin, bw) x5."""
    slots = [[BAND, 0, 300.0, 1.0, 2.0], [BAND, 0, 300.0, 1.0, 2.0],
             [BAND, 0, 1000.0, 1.0, 2.0], [BAND, 0, 1000.0, 1.0, 2.0],
             [HI_PASS, 0, 100.0, 1.0, 2.0]]
    used = 0
    for idx, ty, gain_db, freq, qv in bands:
        if used >= 4:
            break
        lin = 10.0 ** (gain_db / 20.0)
        # Cubase's shelf reaches its full gain about two octaves from its
        # frequency and sits at half of it there; ReaEQ's shelf with a
        # bandwidth of 1.2 octaves comes within +-1 dB of that curve over
        # 30 Hz-6 kHz (measured on a -23 dB low shelf; 1.0 and 1.4 err the
        # other way). It is not the same filter, so the two do not null:
        # a Cubase channel EQ is the one thing on a channel that cannot be
        # made sample-identical outside Cubase.
        if idx == 0 and ty in (5, 6, 7, 8, 2, 3, 4):
            slots[used] = [LO_SHELF, 1, freq, lin, SHELF_BW]
        elif idx == 3 and ty in (5, 6, 7, 8, 2, 3, 4):
            slots[used] = [HI_SHELF, 1, freq, lin, SHELF_BW]
        else:
            slots[used] = [BAND, 1, freq, lin, q_to_octaves(qv)]
        used += 1
    return slots


def state(bands):
    st = struct.pack('<II', 33, 5)
    for ty, en, freq, lin, bw in reaeq_bands(bands):
        st += struct.pack('<iiddd', ty, en, freq, lin, bw) + b'\x01'
    st += struct.pack('<iidd', 1, 1, 1.0, 0.0) + struct.pack('<IHH', 0, 2, 1)
    return st


def entry(index):
    """The plug-in index entry for ReaEQ, from REAPER's scanned list, or a
    stand-in that names the file REAPER ships it as."""
    e = None
    try:
        e = index.by_num.get(REAEQ_NUM)
    except Exception:
        e = None
    if e is None:
        e = {'num': REAEQ_NUM, 'uid': '', 'disp': 'ReaEQ (Cockos)', 'file': 'reaeq.dll',
             'inst': False, 'vst3': False}
    return e


def block(bands, index, indent='      '):
    """The RPP lines for one ReaEQ carrying `bands` (a BYPASS line included)."""
    st = state(bands)
    e = entry(index)
    # a VST2 keeps its chunk raw between REAPER's header and tail (vst_block
    # wraps a state the VST3 way, as two blobs, which is not what ReaEQ reads)
    head = P.vst_header(e['num'], 2, 2, len(st))
    # REAPER's own save of a VST2 ends the header with 0x100000 where a
    # VST3's has 0 (compared against a ReaEQ REAPER saved itself)
    head = head[:-4] + struct.pack('<I', 0x100000)
    fname = e['file']
    fq = '"%s"' % fname if (' ' in fname or not fname) else fname
    lines = ['%sBYPASS 0 0 0' % indent,
             '%s<VST "VST: %s" %s 0 "" %d<%s> ""' % (indent, e['disp'], fq, e['num'], REAEQ_IDENT)]
    for blob in (head, st, P.vst_tail('')):
        for ln in P.wrap_b64(blob):
            lines.append(indent + '  ' + ln)
    lines.append(indent + '>')
    return lines


def describe(bands):
    names = {0: 'lo', 1: 'lo-mid', 2: 'hi-mid', 3: 'hi'}
    return ', '.join('%s %+.1f dB @ %.0f Hz' % (names.get(i, i), g, f) for i, ty, g, f, qv in bands)


# ---- ReaEQ -> Cubase's channel EQ -------------------------------------
# ReaEQ, measured on REAPER renders of noise (2026-09-30): its Band is the
# RBJ peaking filter with the bandwidth in octaves (digital form, alpha =
# sin w sinh(ln2/2 bw w / sin w)); its shelves are RBJ shelves with slope
# S = 1/bw^2 (bw no less than 0.915); its low pass the RBJ low pass. In
# Cubase a Band becomes Parametric II with the same width 3 dB under the
# peak (both are the same analog filter; they part only near Nyquist,
# where Cubase follows the analog curve and RBJ is pulled down), a shelf
# becomes Shelf IV, which is the RBJ shelf with the Q the knob sets (Q 0.5
# and up), a low/high pass the nearest Cut.
_QK = (0.0, 0.23, 0.64, 1.0, 3.04, 10.0)
_Q3 = (0.3843, 0.6759, 1.1717, 1.5980, 3.9825, 12.1392)
_QZ = (0.5, 0.5003, 0.5147, 0.5281, 0.6195, 1.5115)


def _inv(tab, v):
    """The knob value for a table value (piecewise linear, extended)."""
    if v <= tab[0]:
        return 0.0
    for i in range(len(tab) - 1):
        if tab[i] <= v <= tab[i + 1]:
            return _QK[i] + (_QK[i + 1] - _QK[i]) * (v - tab[i]) / (tab[i + 1] - tab[i])
    return _QK[-1] + (_QK[-1] - _QK[-2]) * (v - tab[-1]) / (tab[-1] - tab[-2])


def reaeq_state_bands(comp):
    """[(type, on, Hz, linear gain, bw)] from a ReaEQ chunk, or None."""
    if not comp:
        return None
    i = comp.find(struct.pack('<I', 33))
    while i >= 0:
        try:
            n = struct.unpack_from('<I', comp, i + 4)[0]
            if 0 < n <= 64 and i + 8 + 33 * n <= len(comp):
                out = []
                o = i + 8
                for _ in range(n):
                    ty, en, fr, g, bw = struct.unpack_from('<iiddd', comp, o)
                    out.append((ty, en, fr, g, bw))
                    o += 33
                if all(0 <= b[0] <= 20 and 1.0 <= b[2] <= 30000.0 for b in out):
                    return out
        except struct.error:
            pass
        i = comp.find(struct.pack('<I', 33), i + 1)
    return None


def _rbj_q3(f0, gdb, bw, rate=48000.0):
    """Q at the points 3 dB under the peak of ReaEQ's (RBJ) band."""
    import cmath
    A = 10 ** (gdb / 40.0)
    w = 2 * math.pi * f0 / rate
    al = math.sin(w) * math.sinh(math.log(2) / 2 * bw * w / math.sin(w))
    b = (1 + al * A, -2 * math.cos(w), 1 - al * A)
    a = (1 + al / A, -2 * math.cos(w), 1 - al / A)

    def mag(f):
        z = cmath.exp(-2j * math.pi * f / rate)
        return 20 * math.log10(abs((b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)))
    pk = mag(f0)
    ref = pk - 3.0 if gdb > 0 else pk + 3.0
    edges = []
    for lo, hi in ((f0 / 64.0, f0), (f0, min(f0 * 64.0, rate * 0.4999))):
        x0, x1 = lo, hi
        for _ in range(60):
            m = math.sqrt(x0 * x1)
            inside = (mag(m) > ref) if gdb > 0 else (mag(m) < ref)
            if (lo < f0) == inside:
                x1 = m
            else:
                x0 = m
        edges.append(math.sqrt(x0 * x1))
    return f0 / max(edges[1] - edges[0], 1e-9)


def from_reaeq(bands):
    """Cubase channel-EQ bands for ReaEQ bands, and notes on what is not
    the same filter. None when they do not fit Cubase's four bands."""
    use = [b for b in bands if b[1] and not (b[0] in (8, 0, 1) and abs(20 * math.log10(max(b[3], 1e-9))) < 0.01)]
    lo = [b for b in use if b[0] in (0, 4)]
    hi = [b for b in use if b[0] in (1, 3)]
    pk = [b for b in use if b[0] == 8]
    other = [b for b in use if b[0] not in (0, 1, 3, 4, 8)]
    if other or len(lo) > 1 or len(hi) > 1 or len(pk) + len(lo) + len(hi) > 4:
        return None, ["ReaEQ bands of kinds or number Cubase's channel EQ cannot hold"]
    notes = []
    out = []
    free = [1, 2]
    if not lo:
        free.append(0)
    if not hi:
        free.append(3)
    for ty, en, fr, g, bw in lo + hi:
        gdb = 20 * math.log10(max(g, 1e-9))
        idx = 0 if ty in (0, 4) else 3
        if ty in (0, 1):
            A = 10 ** (gdb / 40.0)
            S = 1.0 / max(bw, 0.915) ** 2
            q = 1.0 / math.sqrt(max((A + 1 / A) * (1 / S - 1) + 2, 1e-9))
            if q < 0.5 - 1e-6:
                notes.append('a ReaEQ shelf this gentle (Q %.2f) is steeper in Cubase (Q 0.5 at the least)' % q)
            out.append((idx, 7, gdb, fr, _inv(_QZ, max(q, 0.5))))
        else:
            qv = 1.0 / (2 * math.sinh(math.log(2) / 2 * bw))
            # Cut I's Q follows its gain: the gain that gives this Q (if
            # it is within Cut I's reach), else Cut II
            gq = math.log(max(qv, 1e-6) / 0.6173) / 0.00723
            if -24.0 <= gq <= 24.0:
                out.append((idx, 2, gq, fr, 1.0))
            else:
                out.append((idx, 2 if qv > 0.6 else 3, 24.0 if qv > 0.6 else 0.0, fr, 1.0))
            if min(abs(qv - 0.675), abs(qv - 0.5)) > 0.05:
                notes.append("a ReaEQ %s pass with Q %.2f becomes Cubase's Cut with Q %.3f"
                             % ('high' if ty == 4 else 'low', qv, 0.675 if abs(qv - 0.675) < abs(qv - 0.5) else 0.5))
    for ty, en, fr, g, bw in sorted(pk, key=lambda b: b[2]):
        gdb = 20 * math.log10(max(g, 1e-9))
        idx = free.pop(0)
        q = _inv(_Q3, _rbj_q3(fr, gdb, bw))
        out.append((idx, 1 if idx in (1, 2) else 4, gdb, fr, q))
        if fr > 5000.0:
            notes.append('a ReaEQ band at %.0f Hz is shaped a little differently towards 20 kHz in Cubase' % fr)
    return sorted(out), notes


# ---- the two EQs as filters, for fitting one to the other -------------
def _biquad_db(b, a, freqs, rate):
    import cmath
    out = []
    for f in freqs:
        z = cmath.exp(-2j * math.pi * f / rate)
        out.append(20 * math.log10(max(abs((b[0] + b[1] * z + b[2] * z * z)
                                           / (a[0] + a[1] * z + a[2] * z * z)), 1e-12)))
    return out


def _interp(tab, q):
    if q <= _QK[0]:
        return tab[0]
    for i in range(len(_QK) - 1):
        if q <= _QK[i + 1]:
            return tab[i] + (tab[i + 1] - tab[i]) * (q - _QK[i]) / (_QK[i + 1] - _QK[i])
    return tab[-1] + (tab[-1] - tab[-2]) * (q - _QK[-1]) / (_QK[-1] - _QK[-2])


def _orfanidis(fr, gdb, Q3, rate):
    G = 10 ** (gdb / 20.0)
    if abs(gdb) < 1e-9:
        return (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)
    GB = math.sqrt(G) if abs(gdb) < 6.0206 else (G / math.sqrt(2) if gdb > 0 else G * math.sqrt(2))
    pi = math.pi
    w0 = 2 * pi * min(fr, rate * 0.49) / rate
    dw = w0 / max(Q3, 0.05)
    F = abs(G * G - GB * GB)
    G00 = abs(G * G - 1)
    F00 = abs(GB * GB - 1)
    num = (w0 * w0 - pi * pi) ** 2 + G * G * F00 * pi * pi * dw * dw / F
    den = (w0 * w0 - pi * pi) ** 2 + F00 * pi * pi * dw * dw / F
    G1 = math.sqrt(num / den)
    G01 = abs(G * G - G1)
    G11 = abs(G * G - G1 * G1)
    F01 = abs(GB * GB - G1)
    F11 = abs(GB * GB - G1 * G1)
    W2 = math.sqrt(G11 / G00) * math.tan(w0 / 2) ** 2
    DW = (1 + math.sqrt(F00 / F11) * W2) * math.tan(dw / 2)
    C = F11 * DW * DW - 2 * W2 * (F01 - math.sqrt(F00 * F11))
    D = 2 * W2 * (G01 - math.sqrt(G00 * G11))
    A = math.sqrt(max((C + D) / F, 0.0))
    B = math.sqrt(max((G * G * C + GB * GB * D) / F, 0.0))
    n = 1 + W2 + A
    return (G1 + W2 + B, -2 * (G1 - W2), G1 - B + W2), (n, -2 * (1 - W2), 1 + W2 - A)


def _shelf(fr, gdb, Qp, Qz, hi, rate):
    A = 10 ** (gdb / 40.0)
    sA = math.sqrt(A)
    K = math.tan(math.pi * min(fr, rate * 0.49) / rate)
    if hi:
        return ((A * (A + sA / Qz * K + K * K), A * (-2 * A + 2 * K * K), A * (A - sA / Qz * K + K * K)),
                (1 + sA / Qp * K + A * K * K, -2 + 2 * A * K * K, 1 - sA / Qp * K + A * K * K))
    return ((A * (1 + sA / Qz * K + A * K * K), A * (-2 + 2 * A * K * K), A * (1 - sA / Qz * K + A * K * K)),
            (A + sA / Qp * K + K * K, -2 * A + 2 * K * K, A - sA / Qp * K + K * K))


def _pass(fr, Q, hi, rate):
    w = 2 * math.pi * min(fr, rate * 0.49) / rate
    c = math.cos(w)
    al = math.sin(w) / (2 * Q)
    if hi:
        return ((1 - c) / 2, 1 - c, (1 - c) / 2), (1 + al, -2 * c, 1 - al)
    return ((1 + c) / 2, -(1 + c), (1 + c) / 2), (1 + al, -2 * c, 1 - al)


def cubase_band_db(band, freqs, rate=48000.0):
    """Cubase's response for one channel-EQ band (as the Channel EQ JSFX)."""
    idx, ty, gdb, fr, q = band
    edge = idx in (0, 3)
    hi = idx == 3
    qz = _interp(_QZ, q)
    if not edge:
        ba = _orfanidis(fr, gdb, _interp(_Q3, q) * (1.0 if ty >= 1 else 0.5776), rate)
    elif ty == 0:
        ba = _orfanidis(fr, gdb, _interp(_Q3, q) * 0.5776, rate)
    elif ty == 1:
        # Shelf I: its Q knob widens it. Measured at Q 1 and up (Qp/Qz below)
        # and at Q 0 (Crown King Hot's SilkyLead, a Cubase export: the
        # corner lower and both Qs higher, within 1.1 dB); in between the
        # two blend (logarithmically)
        t = max(0.0, min(1.0, q))
        q1 = (0.3327, 0.4882, 1.0) if hi else (0.3714, 0.5497, 1.0)
        q0 = (0.5107, 0.6781, 0.441) if hi else (0.4478, 0.7341, 0.240)
        mix = [math.exp(math.log(a) * (1 - t) + math.log(b) * t) for a, b in zip(q0, q1)]
        ba = _shelf(fr * mix[2], gdb, mix[0], mix[1], hi, rate)
    elif ty == 2:
        ba = _pass(fr, 0.6173 * math.exp(0.00723 * gdb), hi, rate)
    elif ty == 3:
        qc = 0.5 if q <= 1 else (0.5 * (0.5311 / 0.5) ** ((q - 1) / 2.04) if q <= 3.04
                                 else 0.5311 * (5.0076 / 0.5311) ** ((q - 3.04) / 6.96))
        ba = _pass(fr, qc, hi, rate)
    elif ty == 4:
        ba = _orfanidis(fr, gdb, _interp(_Q3, q), rate)
    elif ty == 5:
        ba = _shelf(fr, gdb, 0.5, qz, hi, rate) if gdb >= 0 else _shelf(fr, gdb, qz, 0.5, hi, rate)
    elif ty == 6:
        ba = _shelf(fr, gdb, qz, 0.5, hi, rate) if gdb >= 0 else _shelf(fr, gdb, 0.5, qz, hi, rate)
    else:
        ba = _shelf(fr, gdb, qz, qz, hi, rate)
    return _biquad_db(ba[0], ba[1], freqs, rate)


def reaeq_band_db(band, freqs, rate=48000.0):
    """ReaEQ's response for one band (type, on, Hz, linear gain, bw)."""
    ty, en, fr, g, bw = band
    n = len(freqs)
    if not en:
        return [0.0] * n
    gdb = 20 * math.log10(max(g, 1e-9))
    A = 10 ** (gdb / 40.0)
    w = 2 * math.pi * min(fr, rate * 0.49) / rate
    c = math.cos(w)
    if ty == 8:
        al = math.sin(w) * math.sinh(math.log(2) / 2 * bw * w / math.sin(w))
        return _biquad_db((1 + al * A, -2 * c, 1 - al * A), (1 + al / A, -2 * c, 1 - al / A), freqs, rate)
    if ty in (0, 1):
        S = 1.0 / max(bw, 0.915) ** 2
        Q = 1.0 / math.sqrt(max((A + 1 / A) * (1 / S - 1) + 2, 1e-9))
        b, a = _shelf(fr, gdb, Q, Q, ty == 1, rate)
        return _biquad_db(b, a, freqs, rate)
    if ty in (3, 4):
        Q = 1.0 / (2 * math.sinh(math.log(2) / 2 * bw))
        b, a = _pass(fr, Q, ty == 3, rate)
        return _biquad_db(b, a, freqs, rate)
    return None


_GRID = [20.0 * (1000.0 ** (k / 119.0)) for k in range(120)]


def _total(fn, bands, rate):
    tot = [0.0] * len(_GRID)
    for b in bands:
        r = fn(b, _GRID, rate)
        if r is None:
            return None
        tot = [x + y for x, y in zip(tot, r)]
    return tot


def _nelder_mead(fun, x0, steps, iters=250):
    n = len(x0)
    pts = [list(x0)] + [[x0[j] + (steps[j] if j == i else 0.0) for j in range(n)] for i in range(n)]
    vals = [fun(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts = [pts[i] for i in order]
        vals = [vals[i] for i in order]
        c = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        xr = [c[j] + (c[j] - pts[-1][j]) for j in range(n)]
        fr_ = fun(xr)
        if fr_ < vals[0]:
            xe = [c[j] + 2 * (c[j] - pts[-1][j]) for j in range(n)]
            fe = fun(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr_ else (xr, fr_)
        elif fr_ < vals[-2]:
            pts[-1], vals[-1] = xr, fr_
        else:
            xc = [c[j] + 0.5 * (pts[-1][j] - c[j]) for j in range(n)]
            fc = fun(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0]] + [[pts[0][j] + 0.5 * (p[j] - pts[0][j]) for j in range(n)] for p in pts[1:]]
                vals = [vals[0]] + [fun(p) for p in pts[1:]]
    i = min(range(n + 1), key=lambda k: vals[k])
    return pts[i], vals[i]


def fit_reaeq(rbands, rate=48000.0):
    """Cubase channel-EQ bands that play a ReaEQ as closely as the four
    bands allow, and the largest difference left (dB, 20 Hz - 20 kHz).
    (None, None) when ReaEQ holds a kind of band Cubase has nothing for."""
    target = _total(reaeq_band_db, rbands, rate)
    if target is None:
        return None, None
    start, _notes = from_reaeq(rbands)
    if start is None:
        return None, None

    def worst(bands):
        got = _total(cubase_band_db, bands, rate)
        return max(abs(x - y) for x, y in zip(got, target))

    best = (worst(start), start)
    if best[0] < 0.05:
        return best[1], best[0]
    # alternatives: each shelf as Shelf II/III/IV; a resonant pass gets a
    # Parametric band on its corner where a middle band is free
    variants = [start]
    for k, b in enumerate(start):
        if b[1] in (5, 6, 7):
            for ty in (5, 6, 7):
                if ty != b[1]:
                    v = list(start)
                    v[k] = (b[0], ty, b[2], b[3], b[4])
                    variants.append(v)
    used = set(b[0] for b in start)
    for rb in rbands:
        if rb[1] and rb[0] in (3, 4):
            Q = 1.0 / (2 * math.sinh(math.log(2) / 2 * rb[4]))
            free = [i for i in (1, 2) if i not in used]
            if Q > 0.72 and free:
                v = list(start) + [(free[0], 1, 20 * math.log10(Q / 0.675), rb[2], 3.0)]
                variants.append(sorted(v))
    def optimise(v):
        x0, steps = [], []
        for b in v:
            x0 += [b[2], math.log(b[3]), b[4]]
            steps += [1.0, 0.05, 0.3]

        def unpack(x, v=v):
            out = []
            for k, b in enumerate(v):
                g, lf, q = x[3 * k:3 * k + 3]
                if b[1] in (2, 3) and b[0] in (0, 3):
                    # a Cut's Q knob stays at 1 (measured there); Cut I's
                    # gain sets its Q, Cut II ignores gain
                    q = 1.0
                    if b[1] == 3:
                        g = 0.0
                out.append((b[0], b[1], max(-24.0, min(24.0, g)),
                            max(20.0, min(20000.0, math.exp(lf))), max(0.0, min(12.0, q))))
            return out
        x, e = _nelder_mead(lambda x: worst(unpack(x)), x0, steps)
        return e, unpack(x)

    for v in variants:
        e, bands = optimise(v)
        if e < best[0]:
            best = (e, bands)
    # what is left: a free band where the difference is largest, as long
    # as that helps and bands are free
    while best[0] > 0.1:
        got = _total(cubase_band_db, best[1], rate)
        res = [t - g for t, g in zip(target, got)]
        k = max(range(len(res)), key=lambda i: abs(res[i]))
        used = set(b[0] for b in best[1])
        free = [i for i in (1, 2, 0, 3) if i not in used]
        if not free:
            break
        slot = free[0]
        extra = (slot, 1 if slot in (1, 2) else 4, res[k], _GRID[k], 1.0)
        e, bands = optimise(sorted(best[1] + [extra]))
        if e >= best[0] - 0.02:
            break
        best = (e, bands)
    return best[1], best[0]


# ---- Cubase's channel EQ -> ReaEQ, fitted -------------------------------
# REAPER's own EQ with the bands that come closest to Cubase's curve (the
# band models above, both measured on renders): a start per band, then each
# band's gain, frequency and width moved to the smallest worst-case
# difference over 20 Hz - 20 kHz.

def to_reaeq(bands, rate=48000.0):
    """(ReaEQ bands (type, on, Hz, linear gain, bw), largest difference
    left in dB) for Cubase channel-EQ bands."""
    target = _total(cubase_band_db, bands, rate)
    start = []
    for idx, ty, gdb, fr, q in bands:
        edge = idx in (0, 3)
        if edge and ty in (2, 3):
            Q = 0.6173 * math.exp(0.00723 * gdb) if ty == 2 else 0.5
            bw = 2 * math.asinh(1 / (2 * Q)) / math.log(2)
            start.append((3 if idx == 3 else 4, 1, fr, 1.0, bw))
        elif edge and ty not in (0, 4):
            start.append((1 if idx == 3 else 0, 1, fr, 10 ** (gdb / 20), 1.0))
        else:
            start.append((8, 1, fr, 10 ** (gdb / 20), q_to_octaves(_interp(_Q3, q))))
    if not start:
        return [], 0.0

    def unpack(x):
        out = []
        for k, b in enumerate(start):
            g, lf, bw = x[3 * k:3 * k + 3]
            # widths kept where the ReaEQ model was measured: a shelf under
            # 0.915 is not what the model says (2026-09-30: a 0.05-wide
            # high shelf fitted on paper, 4 dB off in REAPER's render)
            lo = 0.915 if b[0] in (0, 1) else (0.1 if b[0] == 8 else 0.05)
            out.append((b[0], 1, max(10.0, min(22000.0, math.exp(lf))),
                        1.0 if b[0] in (3, 4) else 10 ** (max(-60.0, min(24.0, g)) / 20),
                        max(lo, min(8.0, bw))))
        return out

    def worst(x):
        got = _total(reaeq_band_db, unpack(x), rate)
        return max(abs(a - b) for a, b in zip(got, target))

    def fit(x0, steps):
        best, err = x0, worst(x0)
        for _ in range(3):
            x, e = _nelder_mead(worst, best, steps)
            if e >= err - 1e-4:
                break
            best, err = x, e
        return best, err

    x0, steps = [], []
    for b in start:
        x0 += [20 * math.log10(b[3]), math.log(b[2]), b[4]]
        steps += [1.0, 0.05, 0.2]
    best, err = fit(x0, steps)
    # ReaEQ takes any number of bands: where a Cubase curve is shaped in a
    # way no single ReaEQ band follows (Shelf I's long slope), a band more
    # at the worst frequency, all refitted, up to three
    for _ in range(3):
        if err <= 0.3:
            break
        got = _total(reaeq_band_db, unpack(best), rate)
        k = max(range(len(_GRID)), key=lambda i: abs(got[i] - target[i]))
        start.append((8, 1, _GRID[k], 10 ** ((target[k] - got[k]) / 20), 1.5))
        cand, e = fit(best + [target[k] - got[k], math.log(_GRID[k]), 1.5], steps + [1.0, 0.05, 0.2])
        if e >= err - 0.01:
            start.pop()
            break
        best, err = cand, e
        steps = steps + [1.0, 0.05, 0.2]
    return unpack(best), err


def reaeq_data(rbands):
    """ReaEQ's data block for these bands (the layout REAPER saves)."""
    st = struct.pack('<II', 33, len(rbands))
    for ty, en, freq, lin, bw in rbands:
        st += struct.pack('<iiddd', ty, en, freq, lin, bw) + b'\x01'
    st += struct.pack('<iidd', 1, 1, 1.0, 0.0) + struct.pack('<IHH', 0, 2, 1)
    return st
