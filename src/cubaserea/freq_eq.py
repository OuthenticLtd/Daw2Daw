"""Cubase's Frequency EQ <-> REAPER's ReaEQ (and from there Live's EQ Eight).

Cubase has no other EQ an insert slot can hold for any number of bands:
Frequency, eight bands, is what a ReaEQ or an EQ Eight anywhere in a chain
becomes, and what Frequency becomes elsewhere is a ReaEQ (one band each).

Measured on Cubase 15 exports of white noise through Frequency, one band
per track (tools/cubase_fx_testbed.py, 2026-10-02), fitted to standard
biquads within 0.2 dB:

  records    equalizerA{bandon,on,type,freq,gain,q}<n> for band n = 1..8
  types      bands 1 and 8: 0..4 cut 6/12/24/48/96 dB per octave (band 1
             a low cut, band 8 a high cut), 5 low shelf, 6 peak, 7 high
             shelf, 8 notch; bands 2..7: 0 low shelf, 1 peak, 2 high
             shelf, 3 notch
  peak       a cookbook (RBJ) peak whose width 3 dB below its peak (half its
             gain under 6 dB) is 1/Q octaves - at every Q and gain measured
             (Q 0.5..8, +-12, +6)
  shelf      a cookbook shelf of Q 0.50 at Q 0.5, its corner moving out as
             Q rises: Q 1 -> RBJ Q 0.511 at f x 0.955 (low) / 1.047 (high),
             Q 2 -> 0.538 at f x 0.851 / 1.176
  cuts       Butterworth cascades (6 dB first order), every section's Q
             times sqrt(Q) ^ (1 / sections): a 12 dB cut is a pass of Q
             sqrt(Q / 2) (0.505 at 0.5, 1.016 at 2), the 24/48/96 dB ones fit
             within 0.35 dB at Q 2
  notch      a peak of the band's gain, as narrow as a peak of 5.07 x Q
  Q          0.5 at least: 0.1 .. 0.4 play as 0.5 (wider bands do not exist)
"""
import math

from . import chan_eq

RATE = 48000.0
UID = '01F6CCC94CAE4668B7C6EC85E681E419'
CUT_ORDER = (1, 2, 4, 8, 16)            # types 0..4
_SHELF_Q = ((0.5, 0.502, 1.0), (1.0, 0.511, 0.955), (2.0, 0.538, 0.851))


# ------------------------------------------------------------ models
def _rbj_peak(f0, gdb, Q, rate=RATE):
    A = 10 ** (gdb / 40.0)
    w = 2 * math.pi * min(f0, rate * 0.49) / rate
    al = math.sin(w) / (2 * Q)
    c = math.cos(w)
    return (1 + al * A, -2 * c, 1 - al * A), (1 + al / A, -2 * c, 1 - al / A)


def _rbj_shelf(f0, gdb, Q, hi, rate=RATE):
    A = 10 ** (gdb / 40.0)
    w = 2 * math.pi * min(f0, rate * 0.49) / rate
    al = math.sin(w) / (2 * Q)
    c = math.cos(w)
    sA = 2 * math.sqrt(A) * al
    if hi:
        return ((A * ((A + 1) + (A - 1) * c + sA), -2 * A * ((A - 1) + (A + 1) * c),
                 A * ((A + 1) + (A - 1) * c - sA)),
                ((A + 1) - (A - 1) * c + sA, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sA))
    return ((A * ((A + 1) - (A - 1) * c + sA), 2 * A * ((A - 1) - (A + 1) * c),
             A * ((A + 1) - (A - 1) * c - sA)),
            ((A + 1) + (A - 1) * c + sA, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - sA))


def _first_order(f0, hi, rate=RATE):
    K = math.tan(math.pi * min(f0, rate * 0.49) / rate)
    if hi:      # high pass
        return (1 / (1 + K), -1 / (1 + K), 0.0), (1.0, (K - 1) / (K + 1), 0.0)
    return (K / (1 + K), K / (1 + K), 0.0), (1.0, (K - 1) / (K + 1), 0.0)


def _peak_oct(Q, gdb):
    """Octaves between the points 3 dB under the top of an RBJ peak."""
    A = 10 ** (abs(gdb) / 40.0)
    # 3 dB under the top from 6 dB up, half the gain (in dB) below that -
    # where the two meet; Cubase's channel EQ draws its width the same way
    # (chan_eq._orfanidis), and the +6 and +12 dB measurements fit it
    lvl = 10 ** ((abs(gdb) - 3.0) / 20.0) if abs(gdb) >= 6.0206 else 10 ** (abs(gdb) / 40.0)
    # analog prototype: |H(jx)|^2 = ((1-x^2)^2 + (xA/Q)^2) / ((1-x^2)^2 + (x/(AQ))^2)
    def mag(x):
        u = (1 - x * x) ** 2
        return math.sqrt((u + (x * A / Q) ** 2) / (u + (x / (A * Q)) ** 2))
    lo, hi = 1e-4, 1.0
    for _ in range(60):
        m = math.sqrt(lo * hi)
        if mag(m) < lvl:
            lo = m
        else:
            hi = m
    return 2 * math.log2(1.0 / hi)


def rbj_q_of(q, gdb):
    """The RBJ Q of a Frequency peak at Q q and gain gdb."""
    return _rbj_q_cached(round(q, 4), round(gdb, 3))


import functools  # noqa: E402


@functools.lru_cache(maxsize=4096)
def _rbj_q_cached(q, gdb):
    target = 1.0 / max(q, 0.05)
    lo, hi = 0.02, 100.0
    for _ in range(60):
        m = math.sqrt(lo * hi)
        if _peak_oct(m, gdb) > target:
            lo = m
        else:
            hi = m
    return math.sqrt(lo * hi)


def _shelf_params(q):
    q = max(0.5, min(4.0, q))
    t = _SHELF_Q
    for (q0, r0, k0), (q1, r1, k1) in zip(t, t[1:]):
        if q <= q1:
            u = math.log(q / q0) / math.log(q1 / q0)
            return r0 + (r1 - r0) * u, math.exp(math.log(k0) + (math.log(k1) - math.log(k0)) * u)
    (q0, r0, k0), (q1, r1, k1) = t[-2], t[-1]
    u = math.log(q / q0) / math.log(q1 / q0)
    return r0 + (r1 - r0) * u, math.exp(math.log(k0) + (math.log(k1) - math.log(k0)) * u)


def kind_of(n, ty):
    """('cut', order) / ('ls',) / ('hs',) / ('peak',) / ('notch',) for
    band n (1..8) of type ty."""
    ty = int(round(ty))
    if n in (1, 8):
        if 0 <= ty <= 4:
            return ('cut', CUT_ORDER[ty])
        return {5: ('ls',), 6: ('peak',), 7: ('hs',), 8: ('notch',)}.get(ty, ('peak',))
    return {0: ('ls',), 1: ('peak',), 2: ('hs',), 3: ('notch',)}.get(ty, ('peak',))


QMIN = 0.5      # Q 0.1 .. 0.4 render exactly as 0.5 (bands and shelves)


def band_db(band, freqs, rate=RATE):
    """Frequency's response for one band (n, type, Hz, gain dB, Q)."""
    n, ty, hz, gdb, q = band
    k = kind_of(n, ty)
    if k[0] != 'cut':
        q = max(QMIN, q)
    if k[0] == 'peak':
        ba = _rbj_peak(hz, gdb, rbj_q_of(q, gdb), rate)
        return chan_eq._biquad_db(ba[0], ba[1], freqs, rate)
    if k[0] == 'notch':
        ba = _rbj_peak(hz, gdb, rbj_q_of(5.07 * q, gdb), rate)
        return chan_eq._biquad_db(ba[0], ba[1], freqs, rate)
    if k[0] in ('ls', 'hs'):
        rq, f = _shelf_params(q)
        f0 = hz * f if k[0] == 'ls' else hz / f
        ba = _rbj_shelf(f0, gdb, rq, k[0] == 'hs', rate)
        return chan_eq._biquad_db(ba[0], ba[1], freqs, rate)
    order, hi_pass = k[1], n == 1
    out = [0.0] * len(freqs)
    if order == 1:
        b, a = _first_order(hz, hi_pass, rate)
        return chan_eq._biquad_db(b, a, freqs, rate)
    secs = order // 2
    for s in range(secs):
        Q = 1.0 / (2 * math.cos(math.pi * (2 * s + 1) / (4 * secs)))
        Q *= math.sqrt(max(q, 0.05)) ** (1.0 / secs)
        b, a = chan_eq._pass(hz, Q, not hi_pass, rate)
        out = [x + y for x, y in zip(out, chan_eq._biquad_db(b, a, freqs, rate))]
    return out


# --------------------------------------------------- ReaEQ <-> Frequency
GRID = [20.0 * (1000.0 ** (k / 79.0)) for k in range(80)]


def _err(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


def _reaeq_bw_of_rbj_q(Q, hz, rate=RATE):
    w = 2 * math.pi * min(hz, rate * 0.49) / rate
    return math.asinh(1.0 / (2 * Q)) * 2 / math.log(2) * math.sin(w) / w


def from_reaeq_band(rb, n):
    """Frequency (type, Hz, gain, Q) on band n for one ReaEQ band, and how
    far apart the two curves are (dB). None when Frequency has no such kind."""
    ty, en, hz, lin, bw = rb
    gdb = 20 * math.log10(max(lin, 1e-9))
    want = chan_eq.reaeq_band_db(rb, GRID)
    if ty == 8:
        # the RBJ Q ReaEQ plays, as Frequency's 3-dB-under-the-peak width
        w = 2 * math.pi * min(hz, RATE * 0.49) / RATE
        Q = 1.0 / (2 * math.sinh(math.log(2) / 2 * bw * w / math.sin(w)))
        q = max(QMIN, 1.0 / _peak_oct(Q, gdb)) if abs(gdb) > 1e-6 else 1.0
        t = 6 if n in (1, 8) else 1
        got = (t, hz, gdb, q)
    elif ty in (0, 1):
        best = None
        # past Q 2 Frequency's shelves ring (Q 4: +1.3 dB off a plain shelf)
        for qq in (0.5, 0.6, 0.7, 0.8, 1.0, 1.2, 1.5, 2.0):
            for k in range(-12, 13):
                f = hz * 2 ** (k / 24.0)
                b = (n, (5 if ty == 0 else 7) if n in (1, 8) else (0 if ty == 0 else 2), f, gdb, qq)
                e = _err(band_db(b, GRID), want)
                if best is None or e < best[0]:
                    best = (e, b)
        return best[1][1:], best[0]
    elif ty in (3, 4) and n in (1, 8):
        w = 2 * math.pi * min(hz, RATE * 0.49) / RATE
        Q = 1.0 / (2 * math.sinh(math.log(2) / 2 * bw * w / math.sin(w)))
        got = (1, hz, 0.0, max(0.05, 2 * Q * Q))
    else:
        return None, None
    b = (n,) + got
    return got, _err(band_db(b, GRID), want)


def _rec_bands(rec):
    return [(n, rec['equalizerAtype%d' % n], rec['equalizerAfreq%d' % n],
             rec['equalizerAgain%d' % n], rec['equalizerAq%d' % n])
            for n in range(1, 9) if rec.get('equalizerAbandon%d' % n)]


def _curve(bands):
    tot = [0.0] * len(GRID)
    for b in bands:
        tot = [x + y for x, y in zip(tot, band_db(b, GRID))]
    return tot


def refine(rec, want, worst, spare=True):
    """Tune the bands of one Frequency together toward `want` (dB on GRID)
    when one band each leaves more than 0.25 dB: frequencies, gains and Qs
    move jointly, and a free middle band may join as a bell (Frequency's
    shelves are steeper than ReaEQ's gentle ones, and no bell is wider
    than Q 0.5). Returns the record and the largest difference left."""
    if worst <= 0.25:
        return rec, worst
    bands = _rec_bands(rec)
    used = {b[0] for b in bands}
    free = [n for n in (2, 3, 4, 5, 6, 7) if n not in used]
    if spare and free:
        # where the curve is furthest off: a bell there of the difference
        cur = _curve(bands)
        k = max(range(len(GRID)), key=lambda i: abs(want[i] - cur[i]))
        bands.append((free[0], 1.0, GRID[k], want[k] - cur[k], 0.7))
    shapes = [b[1] for b in bands]
    ns = [b[0] for b in bands]
    cont = [kind_of(n, t)[0] != 'cut' for n, t in zip(ns, shapes)]

    def unpack(x):
        out, i = [], 0
        for n, t, b, c in zip(ns, shapes, bands, cont):
            hz = math.exp(x[i]); i += 1
            g = x[i] if c else 0.0; i += 1 if c else 0
            q = max(QMIN, min(2.0, math.exp(x[i]))) if c and kind_of(n, t)[0] in ('ls', 'hs')                 else max(QMIN if c else 0.05, math.exp(x[i])); i += 1
            out.append((n, t, max(20.0, min(20000.0, hz)), max(-24.0, min(24.0, g)), q))
        return out
    x0, steps = [], []
    for b, c in zip(bands, cont):
        x0.append(math.log(b[2])); steps.append(0.15)
        if c:
            x0.append(b[3]); steps.append(1.0)
        x0.append(math.log(max(b[4], 0.05))); steps.append(0.2)

    def cost(x):
        return _err(_curve(unpack(x)), want)
    best, val = chan_eq._nelder_mead(cost, x0, steps, iters=120 + 40 * len(bands))
    if val >= worst:
        return rec, worst
    rec = dict(rec)
    for n, t, hz, g, q in unpack(best):
        rec.update({'equalizerAbandon%d' % n: 1.0, 'equalizerAon%d' % n: 1.0,
                    'equalizerAtype%d' % n: float(t), 'equalizerAfreq%d' % n: float(hz),
                    'equalizerAgain%d' % n: float(g), 'equalizerAq%d' % n: float(q)})
    return rec, val


def records_for(rbands, log=None, name='ReaEQ'):
    """Frequency instances (a dict of records each) playing these ReaEQ
    bands, and the largest curve difference. A high pass takes band 1, a
    low pass band 8 (the bands that cut); the rest fill 2..7, then 1 and 8;
    past eight bands a second Frequency follows."""
    rest = [b for b in rbands if b[1]]
    out, worst = [], 0.0
    while rest:
        rec = {'equalizerAbandon%d' % k: 0.0 for k in range(1, 9)}
        rec.update({'dynamic%d' % k: 0.0 for k in range(1, 9)})
        rec.update({'linearphase%d' % k: 0.0 for k in range(1, 9)})
        rec.update({'equalizerAoutput': 0.0, 'equalizerAbypass': 0.0, 'bypass': 0.0})
        free = [2, 3, 4, 5, 6, 7, 1, 8]
        left = []
        for b in rest:
            if b[0] == 4 and 1 in free:
                n = 1
            elif b[0] == 3 and 8 in free:
                n = 8
            elif b[0] in (3, 4):
                left.append(b)
                continue
            else:
                n = next((k for k in free if k not in (1, 8)), None) or \
                    next((k for k in free), None)
                if n is None:
                    left.append(b)
                    continue
            got, err = from_reaeq_band(b, n)
            if got is None:
                if log is not None:
                    log.append('a %s band of type %d has no Frequency counterpart; left out'
                               % (name, b[0]))
                continue
            free.remove(n)
            ty, hz, gdb, q = got
            rec.update({'equalizerAbandon%d' % n: 1.0, 'equalizerAon%d' % n: 1.0,
                        'equalizerAtype%d' % n: float(ty), 'equalizerAfreq%d' % n: float(hz),
                        'equalizerAgain%d' % n: float(gdb), 'equalizerAq%d' % n: float(q)})
            worst = max(worst, err)
        if not left:
            # all of it in this one: tune it against the whole ReaEQ curve
            curves = [chan_eq.reaeq_band_db(x, GRID) for x in rbands if x[1]]
            if curves and all(c is not None for c in curves):
                want = [sum(v) for v in zip(*curves)]
                rec, worst = refine(rec, want, _err(_curve(_rec_bands(rec)), want))
        out.append(rec)
        if len(left) == len(rest):
            break
        rest = left
    return out, worst


def reaeq_of(recs, log=None):
    """ReaEQ bands for a Frequency's records, and the largest curve
    difference: a band per Frequency band (a cut steeper than 12 dB as
    that many 12 dB passes)."""
    g = lambda k, d=0.0: recs[k][1] if k in recs else d
    out, worst = [], 0.0
    for n in range(1, 9):
        if g('equalizerAbandon%d' % n) < 0.5:
            continue
        ty, hz, gdb, q = (g('equalizerAtype%d' % n), g('equalizerAfreq%d' % n, 1000.0),
                          g('equalizerAgain%d' % n), g('equalizerAq%d' % n, 1.0))
        on = 1 if g('equalizerAon%d' % n, 1.0) >= 0.5 else 0
        k = kind_of(n, ty)
        band = (n, ty, hz, gdb, q)
        want = band_db(band, GRID)
        if k[0] in ('peak', 'notch'):
            Q = rbj_q_of(q * (5.07 if k[0] == 'notch' else 1.0), gdb)
            rb = [(8, on, hz, 10 ** (gdb / 20.0), _reaeq_bw_of_rbj_q(Q, hz))]
        elif k[0] in ('ls', 'hs'):
            best = None
            for bw in (0.6, 0.8, 0.915, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0):
                for kk in range(-12, 13):
                    f = hz * 2 ** (kk / 24.0)
                    c = (0 if k[0] == 'ls' else 1, on, f, 10 ** (gdb / 20.0), bw)
                    e = _err(chan_eq.reaeq_band_db(c, GRID), want)
                    if best is None or e < best[0]:
                        best = (e, c)
            rb = [best[1]]
        else:
            order = k[1]
            ty_r = 4 if n == 1 else 3
            secs = max(1, order // 2)
            rb = []
            for s in range(secs):
                Q = 1.0 / (2 * math.cos(math.pi * (2 * s + 1) / (4 * secs)))
                if order == 1:
                    Q = 0.5          # the nearest a 12 dB pass comes to 6 dB
                else:
                    Q *= math.sqrt(max(q, 0.05)) ** (1.0 / secs)
                rb.append((ty_r, on, hz, 1.0, _reaeq_bw_of_rbj_q(Q, hz)))
        got = chan_eq._total(chan_eq.reaeq_band_db, [b for b in rb], RATE) if rb else None
        if got is not None:
            want_g = band_db(band, chan_eq._GRID)
            worst = max(worst, _err(got, want_g)) if on else worst
        out += rb
    if g('dynamic1') or any(g('dynamic%d' % n) for n in range(2, 9)):
        if log is not None:
            log.append("Frequency's dynamic bands play static (their threshold is not carried)")
    return out, worst
