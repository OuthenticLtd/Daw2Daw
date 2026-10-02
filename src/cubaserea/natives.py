"""Built-in effects of Cubase, REAPER and Live, family by family.

Each DAW's own effects load nowhere else, so each becomes the closest of
the other DAW's own, with its settings carried over - never a print. The
hub is REAPER's own effects (the Cockos plug-ins and the JS effects every
REAPER ships): a Cubase effect becomes REAPER blocks here, REAPER blocks
become a Cubase effect here, and Live's devices meet REAPER's in
live_stock - so every pair of DAWs is covered by the two halves. The
first mappings (Compressor, StereoDelay, Gate, Frequency, ...) live in
stock.py / freq_eq.py; this module carries the rest, in four tables:

  CUBASE_TO_REAPER   Cubase effect name -> fn(records, project)
                     -> ([('vst', name, data) | ('js', path, sliders)], how)
  REAPER_TO_CUBASE   'ReaXcomp' / 'JS:<path>' -> fn(data or sliders, tempo)
                     -> (Cubase effect name, {record: value}, how)
  (live_stock.REAPER_MAP / LIVE_MAP hold REAPER <-> Live)

Cubase states are written over the defaults Cubase 15 saves for an effect
(cubase_defaults.json), so records not named keep Cubase's own default.
Units come from each effect's records (dB, ms, %, Hz) and REAPER's own
plug-in data layouts (stock.py, probed with tools/reaper_probe.py).
"""
import math
import struct

from . import stock

db2lin = stock.db2lin
_f32 = stock._f32


def _db(lin):
    return -150.0 if lin is None or lin <= 1e-8 else 20.0 * math.log10(lin)


def _g(rec):
    return lambda k, d=0.0: rec[k][1] if k in rec else d


# ----------------------------------------------------------- ReaXcomp
# measured (tools/reaper_probe.py, REAPER 7): u32 56, u32 band count, then
# per band f64 top frequency (Hz), gain (linear), threshold (linear),
# ratio, knee (dB), i32 attack, release, RMS (ms), flags (1 make-up gain,
# 2 auto release, 4 feedback detector, 8 band off, 16 always), and a tail
XC_MAKEUP, XC_AUTOREL, XC_FEEDBACK, XC_OFF = 1, 2, 4, 8


def reaxcomp(bands, wet=1.0):
    """A ReaXcomp data block: bands of dicts top_hz, gain_db, threshold_db,
    ratio, knee_db, attack_ms, release_ms, rms_ms, makeup, auto_release,
    active."""
    t = stock._data('ReaXcomp')
    size, n0 = struct.unpack_from('<II', t, 0)
    tail = bytes(t[8 + size * n0:])
    out = bytearray(struct.pack('<II', size, len(bands)))
    for b in bands:
        flags = 16
        if b.get('makeup', False):
            flags |= XC_MAKEUP
        if b.get('auto_release', False):
            flags |= XC_AUTOREL
        if not b.get('active', True):
            flags |= XC_OFF
        out += struct.pack('<5d4i', float(b.get('top_hz', 24000.0)), db2lin(b.get('gain_db', 0.0)),
                           db2lin(b.get('threshold_db', 0.0)), float(b.get('ratio', 1.0)),
                           float(b.get('knee_db', 0.0)), int(round(b.get('attack_ms', 15.0))),
                           int(round(b.get('release_ms', 150.0))), int(round(b.get('rms_ms', 5.0))), flags)
    out += tail
    return out


def reaxcomp_bands(data):
    """ReaXcomp's bands as reaxcomp() takes them, or None."""
    if not data or len(data) < 8:
        return None
    size, n = struct.unpack_from('<II', data, 0)
    if size < 52 or n > 16 or 8 + size * n > len(data):
        return None
    out = []
    for k in range(n):
        top, g, thr, ratio, knee, att, rel, rms, flags = struct.unpack_from('<5d4i', data, 8 + size * k)
        out.append(dict(top_hz=top, gain_db=_db(g), threshold_db=_db(thr), ratio=ratio, knee_db=knee,
                        attack_ms=att, release_ms=rel, rms_ms=rms, makeup=bool(flags & XC_MAKEUP),
                        auto_release=bool(flags & XC_AUTOREL), active=not (flags & XC_OFF)))
    return out


# REAPER's JS effects used as targets, with their sliders in order
JS_EXPANDER = 'sstillwell/expander'      # threshold dB, ratio, gain dB, detector,
                                         # detection (2 peak, 3 RMS), attack, release ms


def js_expander(threshold_db, ratio, gain_db=0.0, rms=False, attack_ms=30.0, release_ms=2.0):
    return ('js', JS_EXPANDER, [max(-120.0, min(0.0, threshold_db)), max(1.0, min(20.0, ratio)),
                                max(-20.0, min(20.0, gain_db)), 0.0, 3.0 if rms else 2.0,
                                max(0.0, min(200.0, attack_ms)), max(0.0, min(100.0, release_ms))])


def _comp(threshold_db, ratio, attack_ms, release_ms, makeup_db=0.0, knee_db=0.0, mix=1.0,
          rms_ms=0.0, lowpass=20000.0, hipass=0.0, auto_release=False):
    """ReaComp with a dry/wet blend (Cubase's Mix / Live's Dry/Wet are a
    crossfade: wet x makeup, dry the rest) and detector filters."""
    d = stock.reacomp(threshold_db=threshold_db, ratio=max(1.0, ratio), attack_ms=attack_ms,
                      release_ms=release_ms, knee_db=knee_db, rms_ms=rms_ms,
                      makeup_db=min(6.0, makeup_db + _db(max(mix, 1e-6))),
                      auto_release=auto_release, limit=ratio >= 100.0,
                      dry_db=None if mix >= 0.9999 else _db(1.0 - mix))
    struct.pack_into('<ff', d, 8 + 4 * 6, min(1.0, lowpass / 20000.0), max(0.0, hipass / 20000.0))
    out = [('vst', 'ReaComp', d)]
    extra = makeup_db + _db(max(mix, 1e-6)) - 6.0
    if extra > 1e-6:
        out.append(('js', 'utility/volume', [stock.js_db(db2lin(extra)), 150.0]))
    return out


def _limit(threshold_db, ceiling_db):
    return [('vst', 'ReaLimit', stock.realimit(threshold_db, ceiling_db))]


# ------------------------------------------------- Cubase -> REAPER
def c_expander(r, p):
    g = _g(r)
    return [js_expander(g('threshold', -20.0), g('ratio', 2.0), 0.0, g('rms') > 50.0,
                        g('attack', 5.0), min(100.0, g('release', 150.0)))], \
        'close (REAPER Downward Expander)'


def c_deesser(r, p):
    """DeEsser: the band between Lo and Hi drives the gain down - ReaComp
    whose detector hears only that band (its own filters), fast, reducing
    by up to Reduction."""
    g = _g(r)
    thr = g('threshold', -25.0) if g('autothreshold') < 0.5 else -30.0
    red = max(0.5, g('reduction', 4.0))
    return _comp(thr, 1.0 + red, 0.5, g('release', 150.0), lowpass=g('highfreq', 15000.0),
                 hipass=g('lowfreq', 4500.0)), \
        'approximate (ReaComp with its detector on the sibilant band)'


def c_maximizer(r, p):
    """Maximizer: Optimize pushes the level into the ceiling (Output)."""
    g = _g(r)
    push = 0.24 * g('optimise', 25.0)
    out = g('output', 0.0)
    return _limit(out - push, min(0.0, out)), 'approximate (ReaLimit, Optimize as its drive)'


def c_raiser(r, p):
    g = _g(r)
    thr = g('threshold', -0.1)
    return _limit(thr - g('input', 0.0), min(0.0, thr)), 'close (ReaLimit)'


def c_voxcomp(r, p):
    g = _g(r)
    return _comp(g('threshold', -15.0), 3.0, 5.0, 100.0, g('output', 0.0), 6.0, g('mix', 100.0) / 100.0,
                 rms_ms=5.0), 'approximate (ReaComp)'


def c_blackvalve(r, p):
    g = _g(r)
    # Peak Reduction is the amount (0..100): the threshold comes down with it
    return _comp(-0.4 * g('peakreduction', 0.0) + g('input', 0.0) * -1.0, 4.0, 10.0, 300.0,
                 g('output', 0.0), 6.0, g('mix', 100.0) / 100.0, rms_ms=10.0, auto_release=True), \
        'approximate (ReaComp; the tube colour is not carried)'


def c_tubecomp(r, p):
    g = _g(r)
    return _comp(-g('inputgain', 0.0) - 12.0, 8.0 if g('highratio') > 0.5 else 3.0, g('attack', 1.0),
                 g('release', 500.0), g('outputgain', 0.0), 6.0, g('mix', 50.0) / 100.0,
                 auto_release=g('autorelease') > 0.5), \
        'approximate (ReaComp; the tube drive is not carried)'


def c_vintage(r, p):
    g = _g(r)
    ratio = {0: 2.0, 1: 4.0, 2: 8.0, 3: 12.0, 4: 20.0}.get(int(round(g('ratio', 0.0))), 4.0)
    return _comp(-g('inputgain', 0.0) - 12.0, ratio, g('attack', 1.0), g('release', 500.0),
                 g('outputgain', 0.0), 0.0, g('mix', 100.0) / 100.0,
                 auto_release=g('autorelease') > 0.5), 'approximate (ReaComp)'


def c_vstdynamics(r, p):
    """VSTDynamics: its gate, compressor and limiter sections, each that is
    on, in its order (Routing 0 gate-comp-limit, 1 comp-gate-limit)."""
    g = _g(r)
    gate = [('vst', 'ReaGate', stock.reagate(
        threshold_db=g('gthreshold', -20.0), attack_ms=g('gattack', 1.0),
        release_ms=g('grelease', 150.0), hold_ms=g('ghold', 1.0),
        closed_db=None if g('grange', -100.0) <= -100 else g('grange')))] if g('gon') > 0.5 else []
    comp = _comp(g('cthreshold', -20.0), g('cratio', 2.0), g('cattack', 1.0), g('crelease', 500.0),
                 g('cmakeup', 0.0), auto_release=g('cautorelease') > 0.5) if g('con') > 0.5 else []
    lim = _limit(g('loutput', 0.0), min(0.0, g('loutput', 0.0))) if g('lon') > 0.5 else []
    order = gate + comp if int(round(g('routing', 1.0))) == 0 else comp + gate
    out = order + lim
    return (out, 'close (ReaGate / ReaComp / ReaLimit, its sections)') if out \
        else ([('js', 'utility/volume', [0.0, 150.0])], 'exact (every section off)')


# Cubase's Multiband Compressor makes up each band's gain by itself:
# (1 - 1/ratio) * f(threshold) dB, f from renders of quiet noise that never
# reaches the threshold (ratios 1.5..8 at -30 dB within 0.4 dB); the band's
# Gain adds to it, negative too
MBC_MAKEUP = ((0.0, 0.0), (-10.0, 1.12), (-20.0, 4.585), (-30.0, 10.8), (-40.0, 20.36))


def mbc_makeup_db(threshold_db, ratio):
    t = min(0.0, threshold_db)
    pts = MBC_MAKEUP
    for (t0, f0), (t1, f1) in zip(pts, pts[1:]):
        if t >= t1:
            f = f0 + (f1 - f0) * (t - t0) / (t1 - t0)
            break
    else:
        (t0, f0), (t1, f1) = pts[-2], pts[-1]
        f = f1 + (f1 - f0) * (t - t1) / (t1 - t0)
    return (1.0 - 1.0 / max(1.0, ratio)) * f


def c_multiband(r, p, expander=False):
    """MultibandCompressor / MultibandExpander: four bands, each up to its
    Freq crossover - ReaXcomp's bands exactly so. An expander's ratio
    (1:x below threshold) is ReaXcomp's ratio under 1."""
    g = _g(r)
    bands = []
    for k in range(1, 5):
        ratio = g('ratio%d' % k, 1.0)
        bands.append(dict(top_hz=g('freq%d' % k, 24000.0) if k < 4 else 24000.0,
                          gain_db=g('makeup%d' % k, 0.0) + (0.0 if expander else mbc_makeup_db(
                              g('threshold%d' % k, -15.0), ratio)),
                          threshold_db=g('threshold%d' % k, -15.0),
                          ratio=(1.0 / max(1.0, ratio)) if expander else max(1.0, ratio),
                          knee_db=0.0, attack_ms=g('attack%d' % k, 1.0), release_ms=g('release%d' % k, 500.0),
                          rms_ms=0.0, makeup=False,
                          auto_release=g('auto%d' % k if not expander else 'autorelease%d' % k) > 0.5,
                          active=g('byp%d' % k) < 0.5))
    out = [('vst', 'ReaXcomp', reaxcomp(bands))]
    if abs(g('output')) > 1e-6:
        out.append(('js', 'utility/volume', [stock.js_db(db2lin(g('output'))), 150.0]))
    return out, 'close (ReaXcomp, band for band)'


CUBASE_TO_REAPER = {
    'Expander': c_expander, 'DeEsser': c_deesser, 'Maximizer': c_maximizer, 'Raiser': c_raiser,
    'VoxComp': c_voxcomp, 'Black Valve': c_blackvalve, 'Tube Compressor': c_tubecomp,
    'VintageCompressor': c_vintage, 'VSTDynamics': c_vstdynamics,
    'MultibandCompressor': c_multiband,
    'MultibandExpander': lambda r, p: c_multiband(r, p, expander=True),
}


# ---------------------------------------------------------------- EQs
# Measured on Cubase 15 renders of noise through each EQ (tools/
# cubase_fx_testbed.py, test/eqcal, 2026-10-02) and fitted with ReaEQ bands
# (tools/fit_reaeq.py). A band measured at one setting scales with it:
# its gain in dB in proportion, its frequency with the knob.
def _reaeq(bands):
    from . import chan_eq
    return ('vst', 'ReaEQ', chan_eq.reaeq_data([b for b in bands if b]))


def _bell(hz, gdb, bw):
    return (8, 1, float(hz), db2lin(gdb), float(bw))


def c_studioeq(r, p):
    """StudioEQ: its low and high bands are the channel EQ's shelves and
    cut (types 0/1/2/3 render as the channel EQ's 5/6/7/3, within 0.18 dB),
    its two middle bands Frequency's bells (within 0.09 dB) - carried as
    ReaEQ bands fitted to those curves."""
    from . import chan_eq, freq_eq
    g = _g(r)
    out_bands, worst = [], 0.0
    edge = []
    if g('on1l', 1.0) > 0.5:
        edge.append((0, {0: 5, 1: 6, 2: 7, 3: 3}.get(int(round(g('lftype'))), 5),
                     g('gainlfl'), g('freqlfl', 100.0), g('qlfl', 1.0)))
    if g('on4l', 1.0) > 0.5:
        edge.append((3, {0: 5, 1: 6, 2: 7, 3: 3}.get(int(round(g('hftype'))), 5),
                     g('gainhfl'), g('freqhfl', 10000.0), g('qhfl', 1.0)))
    for b in edge:
        if b[1] != 3 and abs(b[2]) < 1e-6:
            continue
        rb, err = chan_eq.to_reaeq([b])
        out_bands += rb
        worst = max(worst, err)
    for on, f, gg, q in (('on2l', 'freqp1l', 'gainp1l', 'qp1l'), ('on3l', 'freqp2l', 'gainp2l', 'qp2l')):
        if g(on, 1.0) > 0.5 and abs(g(gg)) > 1e-6:
            hz = g(f, 1000.0)
            Q = freq_eq.rbj_q_of(max(0.05, g(q, 1.0)), g(gg))
            out_bands.append(_bell(hz, g(gg), freq_eq._reaeq_bw_of_rbj_q(Q, hz)))
    ent = [_reaeq(out_bands)] if out_bands else []
    if abs(g('outputl')) > 1e-6:
        ent.append(('js', 'utility/volume', [stock.js_db(db2lin(g('outputl'))), 150.0]))
    return (ent or [('js', 'utility/volume', [0.0, 150.0])]), \
        'close (ReaEQ, within %.1f dB of StudioEQ)' % worst


def c_djeq(r, p):
    """DJ-Eq: a low shelf at 250 Hz, a bell at 1.5 kHz and a high shelf at
    5 kHz (exact to 0.01 dB at +6), each with a kill: the low kill three
    high passes, the high kill three low passes, the mid kill two deep
    bells (within 1.3 dB of its -48 dB notch)."""
    g = _g(r)
    bands = []
    if g('lowCutOn') > 0.5:
        bands += [(4, 1, 173.7, 1.0, 2.449), (4, 1, 355.4, 1.0, 2.393), (4, 1, 30.3, 1.0, 1.834)]
    elif abs(g('lowgain')) > 1e-6:
        bands.append((0, 1, 249.7, db2lin(g('lowgain')), 1.395))
    if g('midCutOn') > 0.5:
        bands += [_bell(1019.0, -32.22, 3.081), _bell(1051.6, -16.13, 3.831)]
    elif abs(g('midgain')) > 1e-6:
        bands.append(_bell(1501.2, g('midgain'), 1.003))
    if g('highCutOn') > 0.5:
        bands += [(3, 1, 8463.6, 1.0, 1.929), (3, 1, 23000.0, 1.0, 0.219), (3, 1, 3039.8, 1.0, 2.128)]
    elif abs(g('highgain')) > 1e-6:
        bands.append((1, 1, 4999.1, db2lin(g('highgain')), 1.394))
    return ([_reaeq(bands)] if bands else [('js', 'utility/volume', [0.0, 150.0])]), \
        'close (ReaEQ, fitted to DJ-Eq renders)'


GEQ10_HZ = (30.6, 63, 125, 250, 501.2, 1000, 2000, 4000, 8000, 16288)    # measured: 1, 5, 10
GEQ30_HZ = (25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000,
            1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000)


def c_geq(r, p, hz, bw, full):
    # full: the fitted bell's gain at the top of the slider, per band
    """GEQ-10 / GEQ-30: a bell per slider (0.5 is flat, 1 the top of its
    range, Range scaling it), the shape fitted at full boost: GEQ-10 bands
    an octave wide (bw 0.5 .. 0.75, peak 12.2 .. 13.4 dB), GEQ-30 bands a
    third (0.24, 13.5 dB) - within 1.3 dB."""
    g = _g(r)
    rng = g('gainrange', 1.0)     # renders: Range 0.5 is half the gain
    bands = []
    for k, f in enumerate(hz, 1):
        s = g('slider%d' % k, 0.5)
        if abs(s - 0.5) < 1e-4:
            continue
        gdb = (s - 0.5) * 2.0 * full(k) * rng
        if g('invert') > 0.5:
            gdb = -gdb
        bands.append(_bell(f, gdb, bw(k)))
    ent = [_reaeq(bands)] if bands else []
    o = g('output', 0.5)
    if abs(o - 0.5) > 1e-4:
        ent.append(('js', 'utility/volume', [stock.js_db(db2lin((o - 0.5) * 24.0)), 150.0]))
    return (ent or [('js', 'utility/volume', [0.0, 150.0])]), \
        'close (ReaEQ, a band per slider, fitted to the graphic EQ\'s renders)'


def c_eqm5(r, p):
    """EQ-M5: a low bell (boost x 0.64 dB per step), a mid cut (x 1.07) and
    a high bell (x 0.63) - fitted within 0.04 dB at 5."""
    g = _g(r)
    bands = []
    if g('boostlow') > 1e-6:
        bands.append(_bell(g('freqlow', 500.0), 0.636 * g('boostlow'), 1.337))
    if g('attenmid') > 1e-6:
        bands.append(_bell(1.04 * g('freqmid', 2000.0), -1.068 * g('attenmid'), 2.446))
    if g('boosthigh') > 1e-6:
        bands.append(_bell(0.98 * g('freqhigh', 3000.0), 0.628 * g('boosthigh'), 0.654))
    ent = [_reaeq(bands)] if bands else []
    if abs(g('output')) > 1e-6:
        ent.append(('js', 'utility/volume', [stock.js_db(db2lin(g('output'))), 150.0]))
    return (ent or [('js', 'utility/volume', [0.0, 150.0])]), 'close (ReaEQ, fitted to EQ-M5 renders)'


def c_eqp1a(r, p):
    """EQ-P1A: the low boost a shelf with its corner at 5.7 x the knob's
    frequency, the low cut a shelf at 11 x, the high boost a bell, the high
    cut a shelf at 0.24 x - each fitted at 5 (0.04 .. 0.65 dB)."""
    g = _g(r)
    bands = []
    if g('lowboost') > 1e-6:
        bands.append((0, 1, 5.70 * g('freqboostlow', 100.0), db2lin(1.606 * g('lowboost')), 1.729))
    if g('lowatten') > 1e-6:
        bands.append((0, 1, 11.1 * g('freqboostlow', 100.0), db2lin(-1.582 * g('lowatten')), 1.298))
    if g('highboost') > 1e-6:
        bands.append(_bell(1.07 * g('freqboosthigh', 5000.0), 0.75 * g('highboost'),
                           2.222 * max(0.1, g('bandwidth', 1.0))))
    if g('highatten') > 1e-6:
        bands.append((1, 1, 0.2434 * g('freqattenhigh', 8000.0), db2lin(-0.804 * g('highatten')), 1.348))
    ent = [_reaeq(bands)] if bands else []
    if abs(g('output')) > 1e-6:
        ent.append(('js', 'utility/volume', [stock.js_db(db2lin(g('output'))), 150.0]))
    return (ent or [('js', 'utility/volume', [0.0, 150.0])]), 'close (ReaEQ, fitted to EQ-P1A renders)'


CUBASE_TO_REAPER.update({
    'StudioEQ': c_studioeq, 'DJ-Eq': c_djeq, 'EQ-M5': c_eqm5, 'EQ-P1A': c_eqp1a,
    'GEQ-10': lambda r, p: c_geq(r, p, GEQ10_HZ, lambda k: 0.5 + 0.254 * min(1.0, (k - 1) / 4.0)
                                 - 0.063 * max(0.0, (k - 5) / 5.0),
                                 lambda k: 12.17 + 1.24 * min(1.0, (k - 1) / 4.0) - 0.46 * max(0.0, (k - 5) / 5.0)),
    'GEQ-30': lambda r, p: c_geq(r, p, GEQ30_HZ, lambda k: 0.24, lambda k: 13.49),
})


# ------------------------------------------------- REAPER -> Cubase
def r_expander(sl, tempo):
    v = list(sl) + [0.0] * 7
    return 'Expander', {'threshold': v[0], 'ratio': max(1.0, v[1]), 'attack': v[5],
                        'release': max(1.0, v[6]), 'rms': 80.0 if int(v[4]) & 1 else 0.0,
                        'softknee': 0.0, 'bypass': 0.0}, 'close (Cubase Expander)'


def r_reaxcomp(data, tempo):
    bands = reaxcomp_bands(data)
    if not bands:
        return None
    act = [b['ratio'] for b in bands if b['active']]
    expander = bool(act) and any(r < 0.999 for r in act) and all(r <= 1.001 for r in act)
    name = 'MultibandExpander' if expander else 'MultibandCompressor'
    rec = {'output': 0.0, 'bypass': 0.0}
    for k, b in enumerate(bands[:4], 1):
        rec.update({'threshold%d' % k: b['threshold_db'],
                    'ratio%d' % k: (1.0 / max(b['ratio'], 1e-3)) if expander else max(1.0, b['ratio']),
                    'attack%d' % k: max(0.1, b['attack_ms']), 'release%d' % k: max(10.0, b['release_ms']),
                    'makeup%d' % k: max(-24.0, b['gain_db'] - (0.0 if expander else mbc_makeup_db(
                        b['threshold_db'], b['ratio']))),
                    'byp%d' % k: 0.0 if b['active'] else 1.0,
                    ('autorelease%d' if expander else 'auto%d') % k: 1.0 if b['auto_release'] else 0.0})
        if k < 4:
            rec['freq%d' % k] = b['top_hz']
    return name, rec, 'close (Cubase %s, band for band)' % name


REAPER_TO_CUBASE = {
    'JS:' + JS_EXPANDER: r_expander,
    'ReaXcomp': r_reaxcomp,
}


# --------------------------------------------- REAPER's JS EQs as ReaEQ
# Stillwell's RBJ EQs are cookbook biquads (their source): a peak of Q 0.8
# at fixed frequencies, high/low passes of Q 1/sqrt 2 (hpflpf) or 1 (the
# 7-band's high pass) - ReaEQ bands exactly. LOSER's 3/4-band EQs split
# with one-pole crossovers: the nearest shelves.
RBJ4_F = ((40, 80, 160, 315, 500), (125, 250, 500, 1000, 2000), (315, 630, 1200, 2500, 5000),
          (1600, 3200, 6400, 9000, 12000))
RBJ7_F = (100, 200, 400, 800, 2500, 6000, 12000)


def _pass_bw(Q, hz):
    from . import freq_eq
    return freq_eq._reaeq_bw_of_rbj_q(Q, hz)


def js_eq_bands(path, sl):
    """(ReaEQ bands, output gain dB) for one of REAPER's JS EQs, or None."""
    from . import freq_eq
    v = list(sl) + [0.0] * 16
    if path == 'sstillwell/hpflpf':
        b = []
        if v[0] > 0:
            b.append((4, 1, v[0], 1.0, _pass_bw(1 / math.sqrt(2), v[0])))
        if v[1] < 22000:
            b.append((3, 1, v[1], 1.0, _pass_bw(1 / math.sqrt(2), v[1])))
        return b, v[2]
    if path == 'sstillwell/rbj4eq':
        b = []
        for k in range(4):
            hz = RBJ4_F[k][max(0, min(4, int(round(v[2 * k]))))]
            if abs(v[2 * k + 1]) > 1e-6:
                b.append((8, 1, float(hz), db2lin(v[2 * k + 1]), freq_eq._reaeq_bw_of_rbj_q(0.8, hz)))
        return b, 0.0
    if path == 'sstillwell/rbj7eq':
        b = [(4, 1, max(10.0, v[0]), 1.0, _pass_bw(1.0, max(10.0, v[0])))] if v[0] > 10.5 else []
        for k, hz in enumerate(RBJ7_F):
            if abs(v[1 + k]) > 1e-6:
                b.append((8, 1, float(hz), db2lin(v[1 + k]), freq_eq._reaeq_bw_of_rbj_q(0.8, hz)))
        return b, 0.0
    if path in ('loser/3BandEQ', 'loser/4BandEQ'):
        if path == 'loser/3BandEQ':
            lo, f1, mid, f2, hi, out = v[:6]
            mids = [(mid, f1, f2)]
        else:
            lo, f1, lm, f2, hm, f3, hi, out = v[:8]
            mids = [(lm, f1, f2), (hm, f2, f3)]
            mid = lm
        b = []
        # the low and high bands relative to the middle, a broad shelf at
        # each crossover; the middle level as output gain
        if abs(lo - mid) > 1e-6:
            b.append((0, 1, max(20.0, f1), db2lin(lo - mid), 2.5))
        if path == 'loser/4BandEQ' and abs(hm - lm) > 1e-6:
            b.append((1, 1, max(20.0, f2), db2lin(hm - lm), 2.5))
        top = hm if path == 'loser/4BandEQ' else mid
        if abs(hi - top) > 1e-6:
            b.append((1, 1, max(20.0, f2 if path == 'loser/3BandEQ' else f3), db2lin(hi - top), 2.5))
        return b, out + mid
    return None


def r_js_eq(path):
    def fn(sl, tempo):
        from . import freq_eq
        got = js_eq_bands(path, sl)
        if not got:
            return None
        bands, out = got
        recs, worst = freq_eq.records_for(bands)
        if not recs:
            return None
        rec = dict(recs[0])
        rec['equalizerAoutput'] = out
        return 'Frequency', rec, "close (Cubase Frequency, within %.1f dB of the JS EQ)" % worst
    return fn


for _p in ('sstillwell/hpflpf', 'sstillwell/rbj4eq', 'sstillwell/rbj7eq', 'loser/3BandEQ', 'loser/4BandEQ'):
    REAPER_TO_CUBASE['JS:' + _p] = r_js_eq(_p)


# -------------------------------------------------------------- delays
# Cubase's delays mix as StereoDelay does (measured there): wet
# min(1, 2 mix), dry min(1, 2 (1 - mix)); a synced time is one of
# stock.SYNC_NOTES (quarter notes) at the song's tempo.
def _tempo(p):
    return p.tempo[0][1] if p is not None and getattr(p, 'tempo', None) else 120.0


def _ms(g, p, ms_key, sync_key, note_key, default=250.0):
    ms = g(ms_key, default)
    if g(sync_key) > 0.5:
        ms = stock.SYNC_NOTES[max(0, min(17, int(round(g(note_key)))))] * 60000.0 / max(_tempo(p), 1.0)
    return ms


def _wetdry(mix_pct):
    m = max(0.0, min(1.0, mix_pct / 100.0))
    return min(1.0, 2 * m), min(1.0, 2 * (1 - m))


def _delay(taps, wet, dry):
    return [('vst', 'ReaDelay', stock.readelay(taps, wet_db=_db(wet) if wet > 0 else -150.0,
                                               dry_db=_db(dry) if dry > 1e-6 else None))]


def c_monodelay(r, p):
    g = _g(r)
    wet, dry = _wetdry(g('mix', 50.0))
    tap = dict(ms=_ms(g, p, 'delay', 'temposync', 'syncnote', 250.0), feedback=g('feedback', 50.0) / 100.0,
               hipass=g('filterL', 50.0) if g('filterLOn') > 0.5 else 0.0,
               lowpass=g('filterH', 15000.0) if g('filterHOn') > 0.5 else 20000.0)
    return _delay([tap], wet, dry), 'close (ReaDelay)'


def c_studiodelay(r, p):
    g = _g(r)
    wet, dry = _wetdry(g('mix', 50.0))
    o = db2lin(g('outgain'))
    tap = dict(ms=_ms(g, p, 'delaytime0', 'temposync', 'syncnote', 500.0), feedback=g('feedback', 50.0) / 100.0,
               hipass=g('lowcut', 20.0), lowpass=g('highcut', 20000.0),
               width=max(-1.0, min(1.0, g('spatial', 50.0) / 50.0 - 1.0)))
    return _delay([tap], wet * o, dry * o), 'close (ReaDelay; its macro effects are not carried)'


def c_modmachine(r, p):
    g = _g(r)
    wet, dry = _wetdry(g('delaymix', 50.0))
    tap = dict(ms=_ms(g, p, 'delaytime', 'temposync', 'syncnote', 500.0),
               feedback=g('delayfeedback', 50.0) / 100.0)
    return _delay([tap], wet, dry), 'approximate (ReaDelay; the modulation, drive and filter are not carried)'


def c_multitap(r, p):
    """MultiTap Delay: its taps (time, pan, level, feedback each)."""
    g = _g(r)
    base = g('delaytime0', 1000.0)
    taps = []
    for k in range(0, 9):
        t = g('delaytime%d' % k)
        if t <= 0:
            continue
        taps.append(dict(ms=t, pan=max(-1.0, min(1.0, g('panvalue%d' % k) / 100.0)),
                         volume=g('levelvalue%d' % k, 100.0) / 100.0,
                         feedback=g('feedbackvalue%d' % k) / 100.0))
    if not taps:
        taps = [dict(ms=base)]
    wet, dry = _wetdry(g('mix', 50.0))
    return _delay(taps, wet, dry), 'approximate (ReaDelay, its taps; the tap effects are not carried)'


CUBASE_TO_REAPER.update({'MonoDelay': c_monodelay, 'StudioDelay': c_studiodelay,
                         'ModMachine': c_modmachine, 'MultiTap Delay': c_multitap})


# REAPER's JS delays as a ReaDelay block (then Cubase via stock.to_cubase,
# Live via live_stock.delay): times in ms or a fraction of a whole note,
# feedback and levels in dB (their sliders)
def js_delay_data(path, sl, tempo=120.0):
    v = list(sl) + [0.0] * 8
    whole = 240000.0 / max(tempo, 1.0)
    if path == 'delay/delay':
        ms, fb, wet, dry = v[0], v[1], v[2] + v[3], v[4]
        taps = [dict(ms=ms, feedback=db2lin(fb) if fb > -119 else 0.0)]
    elif path == 'delay/delay_tone':
        ms, fb, mix = v[0], v[1], v[6]
        taps = [dict(ms=ms, feedback=db2lin(fb) if fb > -119 else 0.0)]
        wet, dry = _db(max(mix, 1e-6)), _db(max(1.0 - mix, 1e-6))
    elif path == 'sstillwell/delay_tempo':
        ms = v[0] if v[0] > 0 else v[6] * whole
        taps = [dict(ms=ms, feedback=db2lin(v[1]) if v[1] > -119 else 0.0)]
        wet, dry = v[2] + v[3], v[4]
    elif path == 'sstillwell/delay_pong':
        ms = v[0] if v[0] > 0 else v[6] * whole
        w = v[5] / 100.0
        fb = db2lin(v[1]) if v[1] > -119 else 0.0
        # ping-pong: repeats alternating sides, each down by the feedback
        taps, lvl = [], 1.0
        for k in range(1, 9):
            taps.append(dict(ms=k * ms, pan=w if k % 2 == 0 else -w, width=0.0, volume=lvl))
            lvl *= fb
            if lvl < 1e-3:
                break
        wet, dry = v[2] + v[3], v[4]
    else:
        return None
    return stock.readelay(taps, wet_db=wet, dry_db=dry if dry > -119 else None)


def _js_delay_to_cubase(path):
    def fn(sl, tempo):
        data = js_delay_data(path, sl, tempo)
        return stock.to_cubase('ReaDelay', data, tempo) if data else None
    return fn


for _p in ('delay/delay', 'delay/delay_tone', 'sstillwell/delay_tempo', 'sstillwell/delay_pong'):
    REAPER_TO_CUBASE['JS:' + _p] = _js_delay_to_cubase(_p)


# ---------------------------------------------------------- modulation
# REAPER's JS targets (sliders in order, from their sources):
JS_CHORUS = 'sstillwell/chorus'      # length ms, voices, rate Hz, depth 0..1, wet dB, dry dB
JS_FLANGER = 'guitar/flanger'        # length ms, feedback dB, wet dB, dry dB, rate Hz
JS_PHASER = 'guitar/phaser'          # rate Hz, range min Hz, range max Hz, feedback dB, wet dB
JS_TREMOLO = 'guitar/tremolo'        # rate Hz, amount dB, stereo separation 0..1
JS_PANNER = 'loser/ppp'              # rate Hz, width %
JS_WAH = 'guitar/wah'                # position 0..1, resonance top, bottom, distortion


def _rate(g, p, rate_key, sync_key, note_key, default=1.0):
    """An LFO rate in Hz; a synced one is a note value (SYNC_NOTES, in
    quarters) at the song's tempo."""
    if g(sync_key) > 0.5:
        q = stock.SYNC_NOTES[max(0, min(17, int(round(g(note_key)))))]
        return _tempo(p) / 60.0 / max(q, 1e-3)
    return g(rate_key, default)


def _mix_db(mix_pct):
    wet, dry = _wetdry(mix_pct)
    return _db(wet) if wet > 0 else -100.0, _db(dry) if dry > 0 else -100.0


# Cubase's Chorus / Flanger at a Mix play this much quieter than REAPER's
# Stillwell Chorus / Flanger at the wet and dry that Mix crossfades to
# (renders of the drum loop, its levels on Cubase's Volume measured):
# their voices sum otherwise, so the level is carried across as this
CHORUS_LVL_DB = 1.88
FLANGER_LVL_DB = 1.45


def c_chorus(r, p, unit=''):
    g = _g(r)
    wet, dry = _mix_db(g('mix' + unit, 50.0))
    return ('js', JS_CHORUS, [max(1.0, min(250.0, g('delay' + unit, 20.0))), 2.0,
                              max(0.1, min(16.0, _rate(g, p, 'rate' + unit, 'temposync' + unit,
                                                       'syncnote' + unit, 1.0))),
                              max(0.0, min(1.0, g('width' + unit, 10.0) / 100.0)),
                              max(-100.0, wet), max(-100.0, dry)])


def c_studiochorus(r, p):
    return [c_chorus(r, p, '1'), c_chorus(r, p, '2')], 'approximate (Stillwell Chorus, one per unit)'


def c_vibrato(r, p):
    g = _g(r)
    return [('js', JS_CHORUS, [8.0, 1.0, max(0.1, min(16.0, _rate(g, p, 'rate', 'tempoSync', 'syncNote', 1.0))),
                               max(0.0, min(1.0, g('depth', 75.0) / 100.0)), 0.0, -100.0])], \
        'approximate (Stillwell Chorus, all wet: a vibrato)'


def c_cloner(r, p):
    g = _g(r)
    wet, dry = _mix_db(g('mix', 50.0))
    return [('js', JS_CHORUS, [max(1.0, g('delay', 50.0) / 2.0), max(1.0, min(8.0, g('nvoices', 4.0))), 0.3,
                               max(0.0, min(1.0, g('detune', 50.0) / 100.0)), wet, dry])], \
        'approximate (Stillwell Chorus, its voices)'


def c_flanger(r, p):
    g = _g(r)
    wet, dry = _mix_db(g('mix', 50.0))
    fb = g('feedback', 50.0) / 100.0
    return [('js', JS_FLANGER, [max(0.0, min(200.0, g('delay', 2.0))),
                                _db(fb) if fb > 1e-3 else -120.0, wet, dry,
                                max(0.001, _rate(g, p, 'rate', 'temposync', 'syncnote', 1.0))])] \
        + _out(-FLANGER_LVL_DB), 'approximate (REAPER Flanger)'


def c_phaser(r, p):
    g = _g(r)
    m = max(0.0, min(0.999, g('mix', 50.0) / 100.0))
    k = m / (1.0 - m)                 # the JS's wet over its dry
    w = g('width', 50.0) / 100.0
    lo, hi = 300.0 * (1.0 - 0.8 * w), 300.0 + 3000.0 * w
    fb = g('feedback', 50.0) / 100.0
    return [('js', JS_PHASER, [max(0.0, min(10.0, _rate(g, p, 'rate', 'temposync', 'syncnote', 1.0))),
                               max(40.0, lo), min(20000.0, hi), max(-120.0, min(-1.0, _db(max(fb, 1e-6)))),
                               max(-120.0, min(12.0, 6.0 * math.log2(max(k, 1e-6))))])] \
        + _out(_db(1.0 - m)), 'approximate (REAPER Phaser)'


def c_tremolo(r, p):
    g = _g(r)
    # the JS's Amount is the gain's swing, 2^(dB/6) (its source: gain runs
    # from 1 - a to 1): Cubase's depth as that swing
    depth = max(0.001, min(1.0, g('depth', 75.0) / 100.0))
    return [('js', JS_TREMOLO, [max(0.0, min(100.0, _rate(g, p, 'rate', 'tempoSync', 'syncNote', 8.0))),
                                max(-60.0, 6.0 * math.log2(depth)), max(0.0, min(1.0, g('spatial', 0.0) / 100.0))])], \
        'close (REAPER Tremolo)'


def c_autopan(r, p):
    g = _g(r)
    return [('js', JS_PANNER, [max(0.0, min(20.0, _rate(g, p, 'rate', 'temposync', 'syncnote', 1.0))),
                               max(0.0, min(100.0, g('width', 75.0)))])], 'close (REAPER Ping Pong Pan)'


def c_wahwah(r, p):
    g = _g(r)
    x = max(0.0, min(1.0, g('pedal', 50.0) / 100.0))
    q = max(1.0, g('qlow', 50.0) * (1 - x) + g('qhigh', 50.0) * x)
    # the frequency the pedal sets over its own sweep, as a position on a
    # 200 .. 2000 Hz one (the sweep every target here shares)
    lo, hi = max(20.0, g('freqlow', 200.0)), max(21.0, g('freqhigh', 2000.0))
    hz = lo * (hi / lo) ** x
    pos = max(0.0, min(1.0, math.log(hz / 200.0) / math.log(10.0)))
    return [('js', JS_WAH, [pos, min(1.0, q / 100.0),
                            min(1.0, q / 100.0), 0.0])], 'approximate (REAPER Wah-Wah)'


CUBASE_TO_REAPER.update({
    'Chorus': lambda r, p: ([c_chorus(r, p)] + _out(-CHORUS_LVL_DB), 'approximate (Stillwell Chorus)'),
    'StudioChorus': c_studiochorus, 'Vibrato': c_vibrato, 'Cloner': c_cloner,
    'Flanger': c_flanger, 'Phaser': c_phaser, 'Tremolo': c_tremolo, 'AutoPan': c_autopan,
    'WahWah': c_wahwah,
})


def _crossfade(w, d):
    """(Cubase Mix %, level dB after it) that play wet w + dry d (linear):
    Cubase's crossfade gives wet min(1, 2 m), dry min(1, 2 (1 - m)), so the
    ratio w / d picks m and the larger of the two is the level (renders:
    a JS flanger at -6/-6 against Cubase's Flanger at 50 %, -6 dB)."""
    if w + d <= 1e-9:
        return 50.0, -150.0
    if w <= d:
        m, lvl = 0.5 * w / d, d
    else:
        m, lvl = 1.0 - 0.5 * d / w, w
    return 100.0 * m, _db(lvl)


def _crossfade_pct(wet_db, dry_db):
    """Cubase's Mix (0..100) for a wet/dry pair: the inverse of _wetdry
    (wet min(1, 2 m), dry min(1, 2 (1 - m))) - exact for a pair that
    crossfade makes, the nearest otherwise."""
    w, d = db2lin(wet_db) if wet_db > -99 else 0.0, db2lin(dry_db) if dry_db > -99 else 0.0
    if w + d <= 1e-9:
        return 50.0
    m = w / 2.0 if w < 0.999 else 1.0 - d / 2.0
    return 100.0 * max(0.0, min(1.0, m))


def r_chorus(sl, tempo):
    v = list(sl) + [0.0] * 6
    if v[1] <= 1 and v[5] <= -99:
        return 'Vibrato', {'rate': v[2], 'tempoSync': 0.0, 'depth': 100.0 * v[3], 'bypass': 0.0}, \
            'close (Cubase Vibrato)'
    m, lvl = _crossfade(db2lin(v[4]) if v[4] > -99 else 0.0, db2lin(v[5]) if v[5] > -99 else 0.0)
    return 'Chorus', {'rate': v[2], 'temposync': 0.0, 'width': 100.0 * v[3], 'delay': v[0],
                      'mix': m, 'bypass': 0.0}, 'approximate (Cubase Chorus)', lvl + CHORUS_LVL_DB


def r_flanger(sl, tempo):
    v = list(sl) + [0.0] * 6
    m, lvl = _crossfade(db2lin(v[2]) if v[2] > -119 else 0.0, db2lin(v[3]) if v[3] > -119 else 0.0)
    return 'Flanger', {'rate': v[4], 'temposync': 0.0, 'delay': max(0.1, v[0]),
                       'feedback': 100.0 * db2lin(v[1]) if v[1] > -119 else 0.0,
                       'mix': m, 'bypass': 0.0}, 'approximate (Cubase Flanger)', lvl + FLANGER_LVL_DB


def r_phaser(sl, tempo):
    v = list(sl) + [0.0] * 6
    k = 2.0 ** (v[4] / 6.0)           # the JS's wet, its dry being 1
    return 'Phaser', {'rate': v[0], 'temposync': 0.0, 'width': max(0.0, min(100.0, (v[2] - 300.0) / 30.0)),
                      'feedback': 100.0 * 2.0 ** (v[3] / 6.0), 'mix': 100.0 * k / (1.0 + k),
                      'bypass': 0.0}, 'approximate (Cubase Phaser, Volume after it)', _db(1.0 + k)


def r_tremolo(sl, tempo):
    v = list(sl) + [0.0] * 4
    return 'Tremolo', {'rate': v[0], 'tempoSync': 0.0, 'depth': 100.0 * min(1.0, 2.0 ** (v[1] / 6.0)),
                       'spatial': 100.0 * v[2], 'bypass': 0.0}, 'close (Cubase Tremolo)'


def r_panner(sl, tempo):
    v = list(sl) + [0.0] * 2
    return 'AutoPan', {'rate': v[0], 'temposync': 0.0, 'width': v[1], 'bypass': 0.0}, 'close (Cubase AutoPan)'


def r_wah(sl, tempo):
    v = list(sl) + [0.0] * 4
    return 'WahWah', {'pedal': 100.0 * v[0], 'pedalEnable': 1.0, 'qlow': 100.0 * v[2], 'qhigh': 100.0 * v[1],
                      'freqlow': 200.0, 'freqhigh': 2000.0, 'bypass': 0.0}, 'approximate (Cubase WahWah)'


for _p, _f in ((JS_CHORUS, r_chorus), ('guitar/chorus', r_chorus), (JS_FLANGER, r_flanger),
               (JS_PHASER, r_phaser), (JS_TREMOLO, r_tremolo), (JS_PANNER, r_panner), (JS_WAH, r_wah)):
    REAPER_TO_CUBASE['JS:' + _p] = _f


# ----------------------------------------------------------- distortion
# A distortion's sound is its curve, which no other DAW's own effect has:
# each maps to the closest kind (drive, saturation, soft clip, bit
# reduction) with its amount, tone and level - approximate by nature.
JS_DIST = 'guitar/distortion'        # gain dB 0..50, hardness 1..10, max volume dB
JS_SAT = 'loser/Saturation'          # amount %
JS_CLIP = 'schwa/soft_clipper'       # boost dB 0..9, output brickwall dB
JS_BITS = 'utility/dither_psycho'    # bits, noise shaping, dither, width


def _out(db):
    return [('js', 'utility/volume', [stock.js_db(db2lin(db)), 150.0])] if abs(db) > 1e-6 else []


# Level of each distortion over its input (renders of the drum loop):
# Cubase's Distortion by its Boost (0..1, as saved), REAPER's Distortion
# (hardness 6, max -6) by its Gain - the JS's own math, its render nulled
DIST_CUBASE = ((0.0, 2.92), (0.1, -0.26), (0.25, 3.90), (0.5, 7.64), (1.0, 9.99))
DIST_JS = ((0.0, 0.0), (5.0, 5.02), (10.0, 10.03), (15.0, 14.41), (20.0, 16.38), (25.0, 17.45),
           (30.0, 18.22), (40.0, 19.88), (50.0, 21.89))


def _interp(pts, x):
    x = max(pts[0][0], min(pts[-1][0], x))
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def c_distortion(r, p):
    g = _g(r)
    b = max(0.0, min(1.0, g('boost', 0.0)))
    gain = 50.0 * b
    lvl = _interp(DIST_CUBASE, b) + g('output') - _interp(DIST_JS, gain)
    return [('js', JS_DIST, [gain, 6.0, -6.0, 2.0])] + _out(lvl), \
        'approximate (REAPER Distortion, level matched)'


def c_distroyer(r, p):
    g = _g(r)
    return [('js', JS_DIST, [max(0.0, min(50.0, 4.0 * g('drive', 5.0) + g('boost', 3.0))), 6.0, -6.0, 2.0])] \
        + _out(g('output')), 'approximate (REAPER Distortion)'


def _sat_rise_db(amount):
    foo = max(1e-6, amount) / 200.0 * math.pi
    return _db(foo / math.sin(foo))


def _sat_amount_for(rise_db):
    lo, hi = 0.0, 100.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if _sat_rise_db(mid) < rise_db:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def c_magneto(r, p):
    g = _g(r)
    return [('js', JS_SAT, [_sat_amount_for(0.042 * g('drive', 20.0))])] + _out(g('output')), \
        'approximate (REAPER Saturation)'


def c_softclipper(r, p):
    g = _g(r)
    return [('js', JS_CLIP, [max(0.0, min(9.0, g('input', 0.0))), max(-3.0, min(1.0, g('output', 0.0)))])], \
        'close (REAPER Soft Clipper)'


def c_ampsim(r, p):
    g = _g(r)
    return [('js', JS_DIST, [max(0.0, min(50.0, 5.0 * g('drive', 5.0))), 4.0, -6.0, 2.0])] \
        + _out(4.0 * (g('volume', 5.0) - 5.0)), 'approximate (REAPER Distortion; the amp and cabinet ' \
        'models are not carried)'


CUBASE_TO_REAPER.update({'Distortion': c_distortion, 'Distroyer': c_distroyer, 'Magneto II': c_magneto,
                         'SoftClipper': c_softclipper, 'AmpSimulator': c_ampsim})


def r_dist(sl, tempo):
    v = list(sl) + [0.0] * 4
    b = max(0.0, min(1.0, v[0] / 50.0))
    out = _interp(DIST_JS, v[0]) - _interp(DIST_CUBASE, b)
    return 'Distortion', {'boost': b, 'output': max(-24.0, min(24.0, out)), 'mix': 100.0,
                          'bypass': 0.0}, 'approximate (Cubase Distortion, level matched)'


def r_sat(sl, tempo):
    v = list(sl) + [0.0]
    return 'Magneto II', {'drive': max(0.0, min(100.0, _sat_rise_db(v[0]) / 0.042)), 'saturationOn': 1.0,
                          'output': 0.0,
                          'bypass': 0.0}, 'approximate (Cubase Magneto II)'


def r_clip(sl, tempo):
    v = list(sl) + [0.0] * 2
    return 'SoftClipper', {'input': v[0], 'output': v[1], 'mix': 100.0, 'bypass': 0.0}, \
        'close (Cubase SoftClipper)'


for _p, _f in ((JS_DIST, r_dist), (JS_SAT, r_sat), (JS_CLIP, r_clip)):
    REAPER_TO_CUBASE['JS:' + _p] = _f


# ------------------------------------------- reverb, pitch, space, tools
def c_shimmer(r, p):
    g = _g(r)
    wet, dry = _wetdry(g('mix', 50.0))
    fb = max(0.0, min(1.0, g('feedback', 50.0) / 100.0))
    data = stock.reaverbate(wet_db=_db(wet) + g('outgain', 0.0) if wet > 0 else -150.0,
                            dry_db=_db(dry) if dry > 0 else None, room=0.6 + 0.35 * fb, damping=0.3,
                            lowpass=g('highcut', 10000.0), hipass=g('lowcut', 100.0))
    return [('vst', 'ReaVerbate', data)], 'approximate (ReaVerbate; the shimmer\'s pitch is not carried)'


def c_pitchshifter(r, p):
    g = _g(r)
    m = max(0.0, min(1.0, g('mix', 100.0) / 100.0))
    data = stock.reapitch([(max(-24.0, min(24.0, g('pitchshift', 0.0))), m * db2lin(g('outgain', 0.0)))],
                          dry=(1.0 - m) * db2lin(g('outgain', 0.0)))
    return [('vst', 'ReaPitch', data)], 'close (ReaPitch)'


def c_imager(r, p):
    """Imager: a width per band - one width for all (their mean), the
    bands' own levels and pans not carried past the first."""
    g = _g(r)
    n = int(g('imgnoBands', 3.0)) + 1
    ws = [g('imgwidth%d' % k, 100.0) for k in range(1, n + 1) if g('imgbandon%d' % k, 1.0) > 0.5]
    w = sum(ws) / len(ws) if ws else 100.0
    return [stock.stereo_width(w)], 'approximate (REAPER stereo width, the bands\' mean %d %%)' % round(w)


CUBASE_TO_REAPER.update({'Shimmer': c_shimmer, 'PitchShifter': c_pitchshifter, 'Imager': c_imager})


def reapitch_voices(data):
    """[(semitones, linear volume)], dry of a ReaPitch block."""
    if not data or len(data) < 32:
        return [], 1.0
    n, size = struct.unpack_from('<II', data, 8)
    dry = _f32(data, 28)
    out = []
    for k in range(n):
        o = 32 + k * size
        if o + 44 > len(data):
            break
        t = [_f32(data, o + 4 * j) for j in range(11)]
        if t[1] >= 0.5:
            out.append((-24.0 + 48.0 * t[2], t[9]))
    return out, dry


def r_reapitch(data, tempo):
    """ReaPitch -> Cubase: octaves below are Octaver (stock.to_cubase);
    any other one voice PitchShifter."""
    got = stock.to_cubase('ReaPitch', data, tempo)
    if got:
        return got
    voices, dry = reapitch_voices(data)
    if len(voices) != 1:
        return None
    semis, vol = voices[0]
    tot = vol + dry
    return 'PitchShifter', {'pitchshift': semis, 'mix': 100.0 * vol / tot if tot > 1e-9 else 100.0,
                            'outgain': _db(tot) if tot > 1e-9 else 0.0, 'bypass': 0.0}, \
        'close (Cubase PitchShifter)'


REAPER_TO_CUBASE['ReaPitch'] = r_reapitch

# Meters and tuners make no sound: they are left out with a note
METERS_CUBASE = ('SuperVision', 'Tuner', 'TestGenerator')
METERS_LIVE = ('SpectrumAnalyzer', 'Spectrum', 'Tuner')


# ------------------------------------------------ the rest, approximate
def c_rotary(r, p):
    """Rotary: its horn's amplitude and Doppler as a tremolo and a light
    chorus at the rotor's speed (Speed 0.5 is slow, ~0.8 Hz; 1 fast, ~7 Hz)."""
    g = _g(r)
    hz = 0.8 + 6.0 * max(0.0, min(1.0, g('speed', 0.5)))
    return [('js', JS_TREMOLO, [hz, -6.0, 1.0]),
            ('js', JS_CHORUS, [3.0, 1.0, hz, 0.3, -6.0, -3.0])], \
        'approximate (REAPER Tremolo + Chorus; the speaker model is not carried)'


def c_quadrafuzz(r, p):
    g = _g(r)
    drives = [g('drive%d' % k, 0.0) for k in range(1, 5) if g('byp%d' % k) < 0.5 and g('mute%d' % k) < 0.5]
    d = sum(drives) / len(drives) if drives else 0.0
    return [('js', JS_DIST, [max(0.0, min(50.0, d / 2.0)), 6.0, -6.0, 2.0])], \
        'approximate (REAPER Distortion, the bands\' mean drive)'


def c_ampbig(r, p):
    g = _g(r)
    return [('js', JS_DIST, [max(0.0, min(50.0, 5.0 * g('drive', 5.0))), 4.0, -6.0, 2.0])] \
        + _out(4.0 * (g('volume', 5.0) - 5.0)), 'approximate (REAPER Distortion; its amp, cabinet and ' \
        'pedal models are not carried)'


def c_squasher(r, p):
    """Squasher: its bands' downward compression (threshold upper, ratio)
    as ReaXcomp's; the upward half and gate are not carried."""
    g = _g(r)
    n = int(g('compAnoBands', 2.0)) + 1
    bands = []
    for k in range(1, n + 1):
        bands.append(dict(top_hz=g('compAfreq%d' % k, 24000.0) if k < n else 24000.0,
                          gain_db=g('compAoutput%d' % k, 0.0) - 6.0, threshold_db=g('compAthresholdupper%d' % k, -30.0),
                          ratio=1.0 + g('compAratio%d' % k, 50.0) / 10.0, attack_ms=g('compAattack%d' % k, 0.5),
                          release_ms=g('compArelease%d' % k, 50.0), rms_ms=0.0,
                          active=g('compAbandon%d' % k, 1.0) > 0.5))
    return [('vst', 'ReaXcomp', reaxcomp(bands))], 'approximate (ReaXcomp; the upward compression is not carried)'


def c_envshaper_mb(r, p):
    g = _g(r)
    keys = [k for k in ('attack1', 'attack2', 'attack3', 'attack4', 'attackgain', 'attack') if k in r]
    a = sum(g(k) for k in keys) / len(keys) if keys else 0.0
    return [('js', 'loser/TransientController', [max(-100.0, min(100.0, 5.0 * a)), 0.0, 0.0])], \
        'approximate (REAPER Transient Controller, the bands\' mean attack)'


CUBASE_TO_REAPER.update({
    'Rotary': c_rotary, 'Quadrafuzz v2': c_quadrafuzz, 'VST Amp Rack': c_ampbig, 'VST Bass Amp': c_ampbig,
    'Squasher': c_squasher, 'MultibandEnvelopeShaper': c_envshaper_mb, 'UltraShaper': c_envshaper_mb,
})

def c_morphfilter(r, p):
    """MorphFilter: a low pass of type A's slope (0..3: 6, 12, 18, 24 dB,
    sections of Q 0.5 - fitted within 0.1 dB at 1 kHz) morphing into a high
    pass of type B's; carried as whichever Morph leans to, Resonance
    raising the last section's Q (80 %% renders +13 dB)."""
    g = _g(r)
    m = g('morph', 0.0)
    hp = m >= 50.0
    ty = int(round(g('typeFilterB' if hp else 'typeFilterA', 0.0)))
    hz = max(20.0, g('lofreq', 1000.0))
    order = (1, 2, 3, 4)[max(0, min(3, ty))]
    res = max(0.0, min(1.0, g('resonance', 0.0) / 100.0))
    bands = []
    secs = (order + 1) // 2
    for k in range(secs):
        Q = 0.5 if k < secs - 1 else 0.5 + 6.0 * res * res
        if order % 2 == 1 and k == 0:
            # ReaEQ has no first-order pass: a 6 dB high pass is a very wide
            # one far below (0.062 x the corner, bw 8 - within 0.2 dB of the
            # render), a 6 dB low pass a shelf 55 dB deep 15.35 x above
            # (within 0.01 dB of a first-order low pass)
            if hp:
                bands.append((4, 1, max(10.0, hz * 0.0617), 1.0, 8.0))
            else:
                bands.append((1, 1, min(23000.0, hz * 15.35), db2lin(-55.24), 1.413))
            continue
        bands.append((4 if hp else 3, 1, hz, 1.0, _pass_bw(Q, hz)))
    note = '' if m in (0.0, 100.0) else '; its morph between them is not carried'
    return [_reaeq(bands)], 'close (ReaEQ, a %d dB %s pass%s)' % (6 * order, 'high' if hp else 'low', note)


CUBASE_TO_REAPER['MorphFilter'] = c_morphfilter

# Cubase's own effects with nothing like them elsewhere: left out, said so
# ------------------------------------- Cubase's VST2-era effects
# Kept as Steinberg's VST2 wrapper ('VstW' + the bank of normalised
# parameters); the parameters' names and ranges read from Cubase 15's
# vintage-plugins.vst3 itself (tools/format_probe.py curves): every one
# linear from its low to its high value, the lists evenly stepped
LEGACY_PARAMS = {
    'Bitcrusher': (('mode', 0, 3), ('depth', 0, 24), ('divider', 1, 65), ('mix', 0, 100), ('output', 0, 100)),
    'Chopper': (('waveform', 0, 4), ('depth', 0, 100), ('sync', 0, 1), ('speed', 0, 17), ('input', 0, 100),
                ('output', 0, 100), ('mix', 0, 100), ('mono', 0, 1), ('square', 0, 1)),
    'DaTube': (('drive', 0, 100), ('mix', 0, 100), ('output', 0, 1), ('vu', 0, 100)),
}


def legacy_values(name, state):
    from . import plugin_formats
    got = plugin_formats.unvstw(state or b'')
    spec = LEGACY_PARAMS.get(name)
    if not got or 'params' not in got or not spec:
        return None
    return {k: lo + (hi - lo) * v for (k, lo, hi), v in zip(spec, got['params'])}


def _lvl_pct(pct):
    """A 0..100 level knob of these effects (their default, 100, is unity),
    as dB."""
    return _db(max(1e-6, pct / 100.0))


def c_bitcrusher(v, p):
    bits = max(1.0, min(24.0, round(v['depth'])))
    note = '' if v['divider'] <= 1.5 else '; its sample-rate division (1/%d) is not carried' % round(v['divider'])
    wet = v['mix'] / 100.0
    if wet < 0.999:
        note += '; its dry/wet mix is not carried'
    return [('js', JS_BITS, [bits, 0.0, 0.0, 2.0])] + _out(_lvl_pct(v['output'])), \
        'approximate (REAPER bit reduction%s)' % note


def c_chopper(v, p):
    if v['sync'] > 0.5:
        q = stock.SYNC_NOTES[max(0, min(17, int(round(v['speed']))))]
        rate = _tempo(p) / 60.0 / max(q, 1e-3)
    else:
        rate = 1.0 + v['speed']
    depth = max(0.001, min(1.0, v['depth'] / 100.0 * v['mix'] / 100.0))
    return [('js', JS_TREMOLO, [max(0.0, min(100.0, rate)), max(-60.0, 6.0 * math.log2(depth)),
                                0.0 if v['mono'] > 0.5 else 1.0])] + _out(_lvl_pct(v['output'])), \
        "approximate (REAPER Tremolo; the Chopper's square wave is a smooth one here)"


def c_datube(v, p):
    return [('js', JS_SAT, [max(0.0, min(100.0, v['drive']))])] + \
        _out(_db(max(1e-6, v['output']))), 'approximate (REAPER Saturation)'


LEGACY_TO_REAPER = {'Bitcrusher': c_bitcrusher, 'Chopper': c_chopper, 'DaTube': c_datube}


UNMATCHED_CUBASE = ('Vocoder', 'VocalChain', 'Pitch Correct', 'FX Modulator', 'Mix6To2', 'MixerDelay',
                    'MIDI Gate', 'ModScripter', 'Step Modulator', 'Grungelizer', 'Metalizer', 'Tranceformer',
                    'RingModulator', 'StepFilter',
                    'DualFilter', 'AutoFilter', 'ToneBooster')


# --------------------------------------------------------- the hooks
def js_sliders(fx):
    """A JS effect's slider values, from the block the REAPER reader kept."""
    for e in (getattr(fx, 'raw_group', None) or []):
        if hasattr(e, 'raw'):
            for x in e.raw:
                if isinstance(x, str) and x.strip():
                    out = []
                    for v in x.split():
                        try:
                            out.append(float(v))
                        except ValueError:
                            out.append(0.0)
                    return out
    return []


def from_cubase(fx, project=None):
    """(REAPER entries, how) for one of Cubase's own effects this module
    maps, or None."""
    from . import builtins
    name = builtins.name_of_uid(fx.uid) or fx.name
    if name in LEGACY_TO_REAPER:
        v = legacy_values(name, fx.component)
        if v is None:
            return None
        try:
            return LEGACY_TO_REAPER[name](v, project)
        except Exception:
            return None
    fn = CUBASE_TO_REAPER.get(name)
    if fn is None:
        return None
    try:
        return fn(builtins._records(fx.component or b''), project)
    except Exception:
        return None


def to_cubase(key, payload, tempo=120.0):
    """(uid, Cubase state, name, how) for a REAPER block ('ReaXcomp', data)
    or JS effect ('JS:path', sliders) that has a Cubase counterpart here."""
    from . import builtins
    fn = REAPER_TO_CUBASE.get(key)
    if fn is None:
        return None
    got = fn(payload, tempo)
    if not got:
        return None
    if len(got) == 4 and isinstance(got[1], (bytes, bytearray)):
        return got
    name, rec, how = got[:3]
    post = got[3] if len(got) > 3 else 0.0
    st = builtins.table_state(name, rec)
    uid = builtins.uid_of_name(name)
    if not (st and uid):
        return None
    if abs(post) > 0.01:
        return uid, st, name, how, [(builtins.VOLUME_UID, builtins.volume_state(part), 'Volume')
                                    for part in builtins.volume_split(db2lin(post))]
    return uid, st, name, how
