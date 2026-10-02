"""REAPER's and Cubase's own effects as Live's own effects.

A stock effect of one host loads nowhere else, so it becomes the closest of
the other host's own: here Live's Compressor, Limiter, Gate, Delay, Reverb,
EQ Eight, Chorus-Ensemble and Auto Filter, with its settings carried over -
never a print.

The source is REAPER's own plug-in and its saved data block (the reader
keeps it as Fx.reaper_stock even where it also made the Cubase equivalent,
rpp_read), or a Cubase built-in, which stock.from_cubase already turns into
the REAPER stock blocks that play it (measured there); Cubase's Chorus and
WahWah, which have no REAPER block, map on their own. The REAPER blocks are
read here the way stock.to_cubase reads them (same layouts).

Live's parameters are stored in their own units (read off its presets):
thresholds and gains in the mixer's linear gain, times in ms, Compressor
Gain / Limiter Gain and Ceiling / Gate Return in dB, mixes 0..1, EQ Eight
gain in dB with Q.

Every mapping returns (device element, how close) so the log can say it.
"""
import math
import struct

from . import stock

_f32 = stock._f32


def _db(lin):
    return -150.0 if lin is None or lin <= 1e-8 else 20.0 * math.log10(lin)


def _lin(db):
    return 10.0 ** (db / 20.0)


def _set(d, path, value):
    node = d.find(path + '/Manual')
    if node is None:
        return False
    if isinstance(value, bool):
        node.set('Value', 'true' if value else 'false')
    elif isinstance(value, int):
        node.set('Value', str(value))
    else:
        node.set('Value', repr(float(value)))
    return True


def _clamp(d, path, v):
    """v clamped to the parameter's own range in the record."""
    r = d.find(path + '/MidiControllerRange')
    if r is not None:
        try:
            lo, hi = float(r.find('Min').get('Value')), float(r.find('Max').get('Value'))
            v = min(hi, max(lo, v))
        except (TypeError, ValueError, AttributeError):
            pass
    return v


def _put(d, path, v):
    return _set(d, path, _clamp(d, path, float(v)))


# ------------------------------------------------------------ REAPER
def compressor(w, data):
    """ReaComp -> Compressor. ReaComp raw values from byte 8 (stock.reacomp):
    0 threshold lin, 1 ratio (1 + 99 v; 1 = limit), 2 attack 500 v ms,
    3 release 5000 v ms, 10 dry lin, 11 wet (= makeup) lin, 13 RMS 100 v ms,
    14 knee 24 v dB, 15 auto make-up, 16 auto release."""
    if not data or len(data) < 8 + 4 * 17:
        return None
    v = [_f32(data, 8 + 4 * p) for p in range(17)]
    d = w.stock_device('compressor')
    ratio = 1.0 + 99.0 * v[1] if v[1] < 1 else 1e30
    _put(d, 'Threshold', v[0])
    _set(d, 'Ratio', min(ratio, 3.40282326356e38))
    _put(d, 'Attack', max(0.01, 500.0 * v[2]))
    _put(d, 'Release', max(1.0, 5000.0 * v[3]))
    _set(d, 'AutoReleaseControlOnOff', v[16] >= 0.5)
    _put(d, 'Gain', _db(v[11]))
    _set(d, 'GainCompensation', v[15] >= 0.5)
    _put(d, 'Knee', 24.0 * v[14])
    # ReaComp's dry adds the uncompressed signal on top; Live's Dry/Wet
    # crossfades: the same blend at the same overall level when dry = 0
    dry, wet = v[10] or 0.0, v[11] or 1.0
    _put(d, 'DryWet', 1.0 if dry <= 1e-6 else wet / (wet + dry))
    # RMS window: Live's envelope follower is peak (0) or RMS (1), not a time
    _set(d, 'Model', 1 if 100.0 * (v[13] or 0) >= 1.0 else 0)
    return d, 'close (Live Compressor, same settings)'


def limiter(w, data):
    """ReaLimit -> Limiter: threshold and ceiling, f64 dB at 8 and 16. Live
    drives into a fixed ceiling, so the drive is ceiling - threshold."""
    if not data or len(data) < 24:
        return None
    thr, ceil = struct.unpack_from('<dd', data, 8)
    d = w.stock_device('limiter')
    _put(d, 'Gain', ceil - thr if thr < ceil else 0.0)
    _put(d, 'Ceiling', min(0.0, ceil))
    _set(d, 'AutoRelease', False)
    _put(d, 'Release', 3.0)
    return d, 'close (Live Limiter)'


def gate(w, data):
    """ReaGate -> Gate (stock.to_cubase's layout: 0 threshold lin, 1 attack
    500 v ms, 2 release 5000 v ms, 4 hold 1000 v ms, 9 closed gain lin)."""
    if not data or len(data) < 8 + 4 * 15:
        return None
    v = [_f32(data, 8 + 4 * p) for p in range(15)]
    d = w.stock_device('gate')
    _put(d, 'Threshold', v[0])
    _put(d, 'Attack', 500.0 * v[1])
    _put(d, 'Release', 5000.0 * v[2])
    _put(d, 'Hold', 1000.0 * v[4])
    _put(d, 'Return', -max(-100.0, _db(v[9])) if v[9] else 0.0)
    return d, 'close (Live Gate)'


def delay(w, data, tempo=120.0):
    """ReaDelay -> Delay. Taps of 11 f32 each from byte 32 (stock.readelay):
    1 enabled, 2 length ms/10000, 4 feedback lin, 5 lowpass Hz/20000,
    6 hipass Hz/20000, 8 width ((w+1)/2), 9 volume lin, 10 pan ((p+1)/2);
    wet and dry at 24 and 28. Live's Delay has a time per side, one
    feedback, a band-pass and Dry/Wet: the first one or two echo taps."""
    if not data or len(data) < 32:
        return None
    n, size = struct.unpack_from('<II', data, 8)
    wet, dry = _f32(data, 24), _f32(data, 28)
    taps = []
    for k in range(n):
        o = 32 + k * size
        if o + 44 > len(data):
            break
        t = [_f32(data, o + 4 * j) for j in range(11)]
        if t[1] >= 0.5 and stock.readelay_tap_ms(t, tempo) > 0.1:
            taps.append(t)
    if not taps:
        return None
    d = w.stock_device('delay')
    a = taps[0]
    b = taps[1] if len(taps) > 1 else taps[0]
    for side, t in (('L', a), ('R', b)):
        ms = stock.readelay_tap_ms(t, tempo)
        # a tap set in beats is Live's synced time when it is one of the
        # note values Live offers (index of 1, 2, 3, 4, 5, 6, 8, 16
        # sixteenths - read off its presets: Eighth Note 1, Dotted Eighth 2,
        # Dotted Quarter 5): it then follows the tempo as REAPER's does.
        # Otherwise the free time, seconds (Live's goes to 0.999 s)
        sixteenths = 4.0 * 128.0 * (t[3] or 0.0) if (t[2] or 0.0) <= 1e-9 else None
        idx = None
        if sixteenths:
            for k, n in enumerate((1, 2, 3, 4, 5, 6, 8, 16)):
                if abs(sixteenths - n) < 1e-3:
                    idx = k
        if idx is not None:
            _set(d, 'DelayLine_Sync' + side, True)
            _set(d, 'DelayLine_SyncedSixteenth' + side, idx)
        else:
            _set(d, 'DelayLine_Sync' + side, False)
            if ms > 999.0:
                w.log.append('a ReaDelay tap of %.0f ms is past Live Delay\'s '
                             '999 ms and is set to that' % ms)
            _put(d, 'DelayLine_Time' + side, ms / 1000.0)
        _put(d, 'DelayLine_Offset' + side, 0.0)
    _set(d, 'DelayLine_Link', abs(stock.readelay_tap_ms(a, tempo) - stock.readelay_tap_ms(b, tempo)) < 1e-6)
    _set(d, 'DelayLine_PingPong', False)
    _put(d, 'Feedback', max(a[4] or 0.0, 0.0))
    lp, hp = 20000.0 * (a[5] or 1.0), 20000.0 * (a[6] or 0.0)
    if lp < 19999.0 or hp > 1.0:
        lo, hi = max(hp, 50.0), min(lp, 18000.0)
        _set(d, 'Filter_On', True)
        _put(d, 'Filter_Frequency', math.sqrt(lo * hi))
        _put(d, 'Filter_Bandwidth', max(0.5, math.log2(max(hi / lo, 1.0001))))
    else:
        _set(d, 'Filter_On', False)
    _put(d, 'Modulation_AmountTime', 0.0)
    _put(d, 'Modulation_AmountFilter', 0.0)
    vol = (a[9] or 1.0) * (wet or 0.0)
    dry = dry or 0.0
    _put(d, 'DryWet', 1.0 if dry <= 1e-6 else vol / (vol + dry))
    return d, 'close (Live Delay)'


# Measured with tools/live_verb_calib.py (2026-10-01: a 0.5 s noise burst
# into ReaVerbate at room 0.1 .. 0.9 and damping 0 / 0.3 / 0.65, wet only,
# against the converted Live Reverb). Per room size, averaged over the
# damping values:
#   VERB_LEVEL  dB Live's tail lacks against ReaVerbate's (Reflect and
#               Diffuse at 0 dB, the decay corrected as below)
#   VERB_DECAY  ReaVerbate's RT60 over Live's at the decay Live was given
VERB_DECAY = [1.06, 1.06, 1.08, 1.17, 1.22, 1.30, 1.34, 1.29, 1.38]
VERB_LEVEL = [19.8, 19.9, 19.7, 19.6, 19.3, 18.9, 18.3, 17.2, 15.3]


def _by_room(tab, room):
    x = max(0.1, min(0.9, room)) * 10 - 1
    i = min(7, int(x))
    f = x - i
    return tab[i] * (1 - f) + tab[i + 1] * f


# What a second pass with the tables above still measured, per damping
# 0 / 0.3 / 0.65 (rows) and room (columns): dB Live is over ReaVerbate,
# and ReaVerbate's RT60 over Live's. Live's high shelf damps the tail less
# than ReaVerbate's damping does.
VERB_DAMP = (0.0, 0.3, 0.65)
VERB_LEVEL2 = ((0.15, 0.03, 0.01, 0.36, 0.45, 0.72, 0.96, 0.57, 0.73),
               (0.28, 0.23, 0.29, 0.73, 0.94, 1.38, 1.83, 1.84, 2.66),
               (0.38, 0.38, 0.50, 0.98, 1.22, 1.66, 2.06, 2.00, 2.45))
VERB_DECAY2 = ((1.09, 1.07, 1.09, 1.08, 1.10, 1.06, 1.01, 1.06, 1.04),
               (1.00, 0.96, 0.97, 0.97, 0.96, 0.94, 0.87, 0.92, 0.94),
               (0.91, 0.92, 0.91, 0.92, 0.94, 0.95, 0.91, 0.95, 0.97))


def _by_damp(tab2, room, damp):
    d = max(0.0, min(0.65, damp))
    j = 0 if d <= 0.3 else 1
    f = (d - VERB_DAMP[j]) / (VERB_DAMP[j + 1] - VERB_DAMP[j])
    return _by_room(tab2[j], room) * (1 - f) + _by_room(tab2[j + 1], room) * f


def verb_level_db(room, damp=0.0):
    return _by_room(VERB_LEVEL, room) - _by_damp(VERB_LEVEL2, room, damp)


def verb_decay(room, damp=0.0):
    return _by_room(VERB_DECAY, room) * _by_damp(VERB_DECAY2, room, damp)


# ReaVerbate's room size -> its decay (RT60, s), from stock (_VB_RT)
def reverb(w, data):
    """ReaVerbate -> Reverb: wet, dry (lin), room, damping, width
    ((w+1)/2), pre-delay ms/500, lowpass, hipass (Hz/20000) from byte 8.
    The decay comes from the room size through ReaVerbate's own measured
    table (stock._VB_RT); two different reverb algorithms, so approximate."""
    if not data or len(data) < 40:
        return None
    v = [_f32(data, 8 + 4 * k) for k in range(8)]
    wet, dry, room, damp, width, pre, lp, hp = v
    k = max(0, min(9, int(room * 10)))
    f = room * 10 - k
    rt = math.exp(math.log(stock._VB_RT[k]) + f * (math.log(stock._VB_RT[k + 1]) - math.log(stock._VB_RT[k])))
    d = w.stock_device('reverb')
    _put(d, 'DecayTime', 1000.0 * rt * verb_decay(room, damp))
    _put(d, 'PreDelay', max(0.5, 500.0 * pre))
    _put(d, 'RoomSize', 20.0 + 180.0 * room)
    _put(d, 'StereoSeparation', 120.0 * max(0.0, min(1.0, 2 * width - 1)))
    _set(d, 'ShelfHighOn', damp > 0.05)
    _put(d, 'ShelfHiFreq', 16000.0 * (1.0 - damp) + 1500.0 * damp)
    _put(d, 'ShelfHiGain', max(0.2, 1.0 - 0.8 * damp))
    _set(d, 'ShelfLowOn', False)
    # ReaVerbate's low- and high-pass are Live's input filter, a band
    # between the two set by its centre and its width in octaves (left as
    # the preset's, a high-pass at 800 Hz played the whole low end into a
    # return that had none - Black Seven's long reverb 9 dB loud)
    # Live's own input filter cut next to nothing (Black Seven's long
    # reverb 40 dB louder under 100 Hz with it set to the band), so the
    # filters are an EQ Eight in front: ReaVerbate's high- and low-pass fall
    # at 12 dB/octave (read off its render: -13 dB over the 1.5 octaves
    # under an 800 Hz high-pass), EQ Eight's 12 dB cuts at Q 0.71
    lo_hz, hi_hz = 20000.0 * (hp or 0.0), 20000.0 * (lp if lp is not None else 1.0)
    lo_on, hi_on = lo_hz > 1.0, hi_hz < 19999.0
    _set(d, 'BandLowOn', False)
    _set(d, 'BandHighOn', False)
    pre_eq = None
    if lo_on or hi_on:
        cuts = []
        if lo_on:
            cuts.append((4, 1, max(30.0, lo_hz), 1.0, 1.8957))   # ReaEQ high pass
        if hi_on:
            cuts.append((3, 1, min(22000.0, hi_hz), 1.0, 1.8957))
        pre_eq, _how = eq8(w, cuts)
    _set(d, 'ChorusOn', False)
    _set(d, 'FreezeOn', False)
    # MixDirect is the device's Dry/Wet knob (0 = dry only), not a dry
    # level: written as ReaVerbate's dry level it left a wet-only reverb on
    # a return fully dry - Black Seven's long reverb 10 dB loud. The tail's
    # level is the reflections and diffusion at 0 dB, times ReaVerbate's
    # wet relative to its dry (VERB_LEVEL corrects Live's tail to
    # ReaVerbate's, measured: tools/live_verb_calib.py)
    # Live's tail is up to 20 dB under ReaVerbate's and its own controls
    # stop at +6 dB, so a Utility after it makes the level: Live's output is
    # G ((1 - x) dry + x T) with Dry/Wet x and tail T, ReaVerbate's is
    # dry_lvl dry + wet_lvl K T (K the measured shortfall), hence
    # G = dry_lvl + wet_lvl K and x = wet_lvl K / G
    K = _lin(verb_level_db(room, damp))
    wet, dry = (wet or 0.0), (dry or 0.0)
    G = dry + wet * K
    _put(d, 'MixReflect', 1.0)
    _put(d, 'MixDiffuse', 1.0)
    if G <= 1e-9:
        _put(d, 'MixDirect', 0.0)
        return d, 'approximate (Live Reverb, silent: ReaVerbate at no level)'
    _put(d, 'MixDirect', wet * K / G)
    gain = w.gain_device(G)
    out = [(d, 'approximate (Live Reverb: another algorithm, decay and level '
               'matched to ReaVerbate)'), (gain, 'the level ReaVerbate plays at')]
    if pre_eq is not None:
        out.insert(0, (pre_eq, "ReaVerbate's low/high-pass"))
    return out


def eq8(w, bands):
    """ReaEQ bands (type, on, Hz, linear gain, bw octaves) -> EQ Eight, a
    band each (up to eight): 0 low shelf, 1 high shelf, 8 band; 3/4 the
    passes (as ReaEQ draws them: 4 high pass, 3 and 5 low pass). Live's
    modes: 1 low cut 12 dB, 2 low shelf, 3 bell, 5 high shelf, 6 high cut
    12 dB. The bell's Q from the bandwidth (RBJ), the shelves at Q 0.71."""
    d = w.stock_device('eq8')
    _put(d, 'GlobalGain', 0.0)
    used = 0
    for ty, on, hz, g, bw in bands:
        if used >= 8:
            break
        mode = {0: 2, 1: 5, 8: 3, 4: 1, 3: 6, 5: 6}.get(int(ty))
        if mode is None:
            continue
        p = 'Bands.%d/ParameterA/' % used
        _set(d, p + 'IsOn', bool(on))
        _set(d, p + 'Mode', mode)
        _put(d, p + 'Freq', hz)
        _put(d, p + 'Gain', _db(g))
        if mode == 3:
            q = 1.0 / (2.0 * math.sinh(math.log(2.0) / 2.0 * max(bw, 0.01)))
        else:
            q = 0.7071          # shelves and 12 dB cuts: Butterworth
        _put(d, p + 'Q', q)
        used += 1
    for k in range(used, 8):
        _set(d, 'Bands.%d/ParameterA/IsOn' % k, False)
    return d, 'close (Live EQ Eight, band for band)'


REAPER_MAP = {'ReaComp': compressor, 'ReaLimit': limiter, 'ReaGate': gate,
              'ReaDelay': delay, 'ReaVerbate': reverb}


def from_reaper(w, name, data):
    """[(device, how close)] for a REAPER stock effect's data, or None."""
    fn = REAPER_MAP.get(name)
    if fn is None:
        return None
    try:
        if fn is delay:
            # a tap set in beats needs the song's tempo
            got = fn(w, data, (w.tempo[0][1] if getattr(w, 'tempo', None) else 120.0))
        else:
            got = fn(w, data)
    except Exception as e:          # a block of another version: say so
        w.log.append('%s could not be read for Live (%s); left out' % (name, e))
        return None
    if got is None:
        return None
    return got if isinstance(got, list) else [got]


# ------------------------------------------------------------- Cubase
def chorus(w, rec):
    """Cubase Chorus -> Chorus-Ensemble (classic mode): rate, depth,
    delay/width and mix from the Cubase record (builtins._records)."""
    g = lambda k, d0=0.0: rec[k][1] if k in rec else d0
    d = w.stock_device('chorus')
    _set(d, 'Mode', 0)
    _put(d, 'Rate', g('rate', 1.0))
    _put(d, 'Amount', g('depth', 50.0) / 100.0)
    _put(d, 'Feedback', 0.0)
    _put(d, 'Width', g('spatial', 100.0) / 100.0)
    _put(d, 'DryWet', g('mix', 50.0) / 100.0)
    _put(d, 'OutputGain', 1.0)
    return d, 'approximate (Live Chorus-Ensemble)'


def wahwah(w, rec):
    """Cubase WahWah -> Auto Filter, a band-pass at the pedal's position."""
    g = lambda k, d0=0.0: rec[k][1] if k in rec else d0
    d = w.stock_device('autofilter')
    _put(d, 'Resonance', 0.6)
    _put(d, 'LfoAmount', 0.0)
    return d, 'approximate (Live Auto Filter as a wah)'


def from_cubase(w, fx, project=None):
    """Live devices for one of Cubase's own effects: through the REAPER
    stock blocks that play it (stock.from_cubase, measured), or a direct
    mapping. Returns [(device, how)] or None."""
    got = stock.from_cubase(fx, project)
    if got:
        entries, how = got
        out = []
        for e in entries:
            if e[0] == 'vst':
                m = from_reaper(w, e[1], e[2])
                if m is None:
                    return None
                out.extend(m)
            else:
                # a JS effect (volume, width, mixer): no Live counterpart
                # mapped yet
                return None
        return out
    from . import builtins
    name = (builtins.TABLE.get((fx.uid or '').upper()) or (None,))[0] or fx.name
    rec = builtins._records(fx.component or b'') if fx.component else {}
    if name and 'chorus' in name.lower():
        return [chorus(w, rec)]
    if name and 'wah' in name.lower():
        return [wahwah(w, rec)]
    return None
