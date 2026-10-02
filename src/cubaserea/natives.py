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


def c_multiband(r, p, expander=False):
    """MultibandCompressor / MultibandExpander: four bands, each up to its
    Freq crossover - ReaXcomp's bands exactly so. An expander's ratio
    (1:x below threshold) is ReaXcomp's ratio under 1."""
    g = _g(r)
    bands = []
    for k in range(1, 5):
        ratio = g('ratio%d' % k, 1.0)
        bands.append(dict(top_hz=g('freq%d' % k, 24000.0) if k < 4 else 24000.0,
                          gain_db=g('makeup%d' % k, 0.0), threshold_db=g('threshold%d' % k, -15.0),
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
                    'makeup%d' % k: b['gain_db'], 'byp%d' % k: 0.0 if b['active'] else 1.0,
                    ('autorelease%d' if expander else 'auto%d') % k: 1.0 if b['auto_release'] else 0.0})
        if k < 4:
            rec['freq%d' % k] = b['top_hz']
    return name, rec, 'close (Cubase %s, band for band)' % name


REAPER_TO_CUBASE = {
    'JS:' + JS_EXPANDER: r_expander,
    'ReaXcomp': r_reaxcomp,
}


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
    name, rec, how = got
    st = builtins.table_state(name, rec)
    uid = builtins.uid_of_name(name)
    return (uid, st, name, how) if st and uid else None
