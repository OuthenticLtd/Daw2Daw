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
    # ReaComp's detector filters (bytes 32/36, Hz / 20000) are Live's side
    # chain EQ: a band between them is its band pass (mode 1 - Ableton's
    # own De-esser preset), a high pass alone mode 5 (its Glue "sidechain
    # EQ" presets), a low pass alone mode 3
    lp = 20000.0 * (_f32(data, 32) if len(data) >= 36 else 1.0)
    hp = 20000.0 * (_f32(data, 36) if len(data) >= 40 else 0.0)
    sc = d.find('.//SideChainEq')
    if sc is not None and (hp > 20.0 or lp < 19000.0):
        _set(sc, 'On', True)
        if hp > 20.0 and lp < 19000.0:
            f0 = math.sqrt(hp * lp)
            _set(sc, 'Mode', 1)
            _put(sc, 'Freq', f0)
            _put(sc, 'Q', max(0.1, f0 / max(1.0, lp - hp)))
        elif hp > 20.0:
            _set(sc, 'Mode', 5)
            _put(sc, 'Freq', hp)
        else:
            _set(sc, 'Mode', 3)
            _put(sc, 'Freq', lp)
        _put(sc, 'Gain', 0.0)
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
    if v[0] < 0.0099:
        w.log.append("a ReaGate threshold of %.1f dB is under Live Gate's "
                     '-40 dB and is set to that' % _db(v[0]))
    _put(d, 'Threshold', v[0])
    _put(d, 'Attack', 500.0 * v[1])
    _put(d, 'Release', 5000.0 * v[2])
    _put(d, 'Hold', 1000.0 * v[4])
    # ReaGate's closed level (its Dry) is Live's Floor, the record's Gain
    # (-75..0 dB); Live's Return is a hysteresis, which ReaGate has none of
    _put(d, 'Gain', max(-75.0, _db(v[9])) if v[9] else -75.0)
    _put(d, 'Return', 0.0)
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
    # ReaDelay sets its echoes and the dry each at a level; Live's Dry/Wet
    # crossfades the two. The blend is Dry/Wet, the overall level a Utility
    # after it (as for the Reverb): G ((1 - x) dry + x wet) with G = dry + vol
    G = vol + dry
    if G <= 1e-9:
        _put(d, 'DryWet', 1.0)
        return d, 'close (Live Delay, silent: ReaDelay at no level)'
    _put(d, 'DryWet', vol / G)
    if abs(G - 1.0) > 1e-6:
        return [(d, 'close (Live Delay)'), (w.gain_device(G), 'the level ReaDelay plays at')]
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


def reaeq(w, data):
    """ReaEQ -> EQ Eight, band for band (eq8)."""
    from . import chan_eq
    rb = chan_eq.reaeq_state_bands(data)
    return eq8(w, rb) if rb else None


REAPER_MAP = {'ReaComp': compressor, 'ReaLimit': limiter, 'ReaGate': gate, 'ReaEQ': reaeq,
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
    # Cubase's Chorus keeps its depth as 'width' (%), its stereo spread as
    # 'spatial' (%); a tempo-synced rate is a note value, read as 1 Hz
    _put(d, 'Rate', 1.0 if g('temposync') >= 0.5 else g('rate', 1.0))
    _put(d, 'Amount', g('width', 50.0) / 100.0)
    _put(d, 'Feedback', 0.0)
    _put(d, 'Width', g('spatial', 100.0) / 100.0)
    _put(d, 'DryWet', g('mix', 50.0) / 100.0)
    _put(d, 'OutputGain', 1.0)
    return d, 'approximate (Live Chorus-Ensemble)'


def wahwah(w, rec):
    """Cubase WahWah -> Auto Filter, a band-pass at the pedal's position."""
    g = lambda k, d0=0.0: rec[k][1] if k in rec else d0
    d = w.stock_device('autofilter')
    # the pedal (0..100) sweeps the band from freqlow to freqhigh,
    # geometrically, its Q from qlow to qhigh likewise
    x = max(0.0, min(1.0, g('pedal', 50.0) / 100.0))
    lo, hi = max(20.0, g('freqlow', 500.0)), max(20.0, g('freqhigh', 2000.0))
    hz = lo * (hi / lo) ** x
    q = max(1.0, g('qlow', 50.0) * (1 - x) + g('qhigh', 50.0) * x) / 10.0
    _set(d, 'FilterType', 2)                      # band-pass
    _put(d, 'Cutoff', 69.0 + 12.0 * math.log2(hz / 440.0))
    _put(d, 'Resonance', min(1.25, q / 8.0))
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
                m = from_js(w, e[1], e[2])
                if m is None:
                    return None
                out.append(m)
        return out
    from . import builtins
    name = (builtins.TABLE.get((fx.uid or '').upper()) or (None,))[0] or fx.name
    rec = builtins._records(fx.component or b'') if fx.component else {}
    if name and 'chorus' in name.lower():
        return [chorus(w, rec)]
    if name and 'wah' in name.lower():
        return [wahwah(w, rec)]
    return None


def utility(w, gain=1.0, width=1.0, mono=False):
    """Live's Utility: gain (linear), stereo width (1 = 100 %), mono."""
    d = w.gain_device(gain)
    _put(d, 'StereoWidth', width)
    _set(d, 'Mono', bool(mono))
    return d


def from_js(w, path, sliders):
    """REAPER's utility JS effects that Cubase's own effects become
    (stock.from_cubase) as Live's Utility: volume, and the stereo width
    the channel mixer or Stillwell's Stereo Width makes. (device, how)
    or None."""
    lin = lambda db: 2.0 ** (db / 6.0)          # stock.js_db's inverse
    if path in JS_MAP:
        return JS_MAP[path](w, sliders)
    if path == 'utility/volume' and sliders:
        return utility(w, lin(sliders[0])), 'exact (Live Utility gain)'
    if path == 'utility/channelmixer' and len(sliders) >= 4:
        ll, rr, lr, rl = (lin(v) for v in sliders[:4])
        if abs(ll - 0.5) < 1e-3 and abs(lr - 0.5) < 1e-3:
            return utility(w, 1.0, 1.0, True), 'exact (Live Utility, mono)'
        return utility(w, 1.0, max(0.0, ll - lr)), 'exact (Live Utility width)'
    if path == 'sstillwell/stereowidth' and len(sliders) >= 3:
        wb, cb, g = (10 ** (v / 20.0) for v in sliders[:3])
        return utility(w, 1.0, min(4.0, (1 + wb * math.sqrt(2)) / (1 + cb))), \
            'close (Live Utility width)'
    return None


# ------------------------------------------- Live -> REAPER (and Cubase)
# The other way round: each of Live's own devices as the REAPER stock
# effect it was mapped from above, with its settings - the inverse of each
# mapping, so a Set that came from REAPER or Cubase goes back as it was,
# and one made in Live gets the closest. From the REAPER blocks the reader
# goes on exactly as for a REAPER project (als_read -> rpp_read): Cubase's
# own effects (stock.to_cubase), the channel EQ, REAPER's own to REAPER.
# Each returns ([('vst', name, data) | ('js', path, sliders)], how) or None.

def _m(d, path, default=0.0):
    n = d.find(path + '/Manual')
    if n is None:
        return default
    v = n.get('Value')
    if v in ('true', 'false'):
        return v == 'true'
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def note_hz(n):
    """Auto Filter's cutoff is a MIDI note number (20..135)."""
    return 440.0 * 2.0 ** ((n - 69.0) / 12.0)


def rev_compressor(d, tempo):
    if int(_m(d, 'Model', 0)) == 2:
        # the Expand model: REAPER's Downward Expander
        from . import natives
        return [natives.js_expander(_db(_m(d, 'Threshold', 0.1)), _m(d, 'ExpansionRatio', 1.15),
                                    _m(d, 'Gain', 0.0), False, _m(d, 'Attack', 10.0),
                                    min(100.0, _m(d, 'Release', 100.0)))], \
            'close (REAPER Downward Expander)'
    ratio = _m(d, 'Ratio', 4.0)
    x = max(0.0, min(1.0, _m(d, 'DryWet', 1.0)))
    makeup = _m(d, 'Gain', 0.0)
    lp, hp = 20000.0, 0.0
    sc = d.find('.//SideChainEq')
    if sc is not None and _m(sc, 'On', False):
        mode, f0, q = int(_m(sc, 'Mode', 4)), _m(sc, 'Freq', 1000.0), max(0.1, _m(sc, 'Q', 0.71))
        if mode == 1:
            half = f0 / (2.0 * q)
            hp = max(20.0, math.sqrt(half * half + f0 * f0) - half)
            lp = min(20000.0, hp + f0 / q)
        elif mode == 5:
            hp = f0
        elif mode == 3:
            lp = f0
    data = stock.reacomp(threshold_db=_db(_m(d, 'Threshold', 1.0)),
                         ratio=min(ratio, 100.0), limit=ratio >= 100.0,
                         attack_ms=_m(d, 'Attack', 3.0), release_ms=_m(d, 'Release', 100.0),
                         knee_db=_m(d, 'Knee', 0.0),
                         rms_ms=5.0 if int(_m(d, 'Model', 0)) == 1 else 0.0,
                         makeup_db=makeup + _db(x) if x > 0 else -150.0,
                         auto_makeup=bool(_m(d, 'GainCompensation', False)),
                         auto_release=bool(_m(d, 'AutoReleaseControlOnOff', False)),
                         dry_db=None if x >= 1.0 else _db(1.0 - x))
    struct.pack_into('<ff', data, 32, min(1.0, lp / 20000.0), max(0.0, hp / 20000.0))
    return [('vst', 'ReaComp', data)], 'close (ReaComp, same settings)'


def rev_limiter(d, tempo):
    ceil = min(0.0, _m(d, 'Ceiling', 0.0))
    return [('vst', 'ReaLimit', stock.realimit(ceil - max(0.0, _m(d, 'Gain', 0.0)), ceil))], \
        'close (ReaLimit)'


def rev_gate(d, tempo):
    floor = _m(d, 'Gain', -75.0)
    data = stock.reagate(threshold_db=_db(_m(d, 'Threshold', 0.3)),
                         attack_ms=_m(d, 'Attack', 0.1), release_ms=_m(d, 'Release', 30.0),
                         hold_ms=_m(d, 'Hold', 10.0),
                         closed_db=None if floor <= -75.0 else floor)
    return [('vst', 'ReaGate', data)], 'close (ReaGate)'


def rev_delay(d, tempo, post_gain=1.0):
    x = max(0.0, min(1.0, _m(d, 'DryWet', 0.5)))
    fb = max(0.0, _m(d, 'Feedback', 0.0))
    lp, hp = 20000.0, 0.0
    if _m(d, 'Filter_On', False):
        f, bw = _m(d, 'Filter_Frequency', 1000.0), _m(d, 'Filter_Bandwidth', 8.0)
        hp, lp = f / 2.0 ** (bw / 2.0), min(20000.0, f * 2.0 ** (bw / 2.0))
    sixteenth_ms = 60000.0 / (tempo or 120.0) / 4.0
    sides = []
    for side in ('L', 'R'):
        if _m(d, 'DelayLine_Sync' + side, False):
            n = (1, 2, 3, 4, 5, 6, 8, 16)[max(0, min(7, int(_m(d, 'DelayLine_SyncedSixteenth' + side, 0))))]
            sides.append((n * sixteenth_ms, n))
        else:
            sides.append((1000.0 * _m(d, 'DelayLine_Time' + side, 0.25), None))
    linked = bool(_m(d, 'DelayLine_Link', True)) or sides[0] == sides[1]
    use = [sides[0]] if linked else sides
    pans = [0.0] if linked else [-1.0, 1.0]
    taps = [dict(ms=ms, feedback=fb, lowpass=lp, hipass=hp, pan=pn, volume=1.0)
            for (ms, _n), pn in zip(use, pans)]
    data = stock.readelay(taps, wet_db=_db(post_gain * x),
                          dry_db=_db(post_gain * (1.0 - x)) if x < 1 else None)
    # a synced side: ReaDelay's own musical length (what delay() and
    # stock.readelay_tap_ms read: sixteenths = 512 x the value, the time
    # in ms 0), so it follows the tempo as Live's does
    for k, (_ms, n) in enumerate(use):
        if n:
            struct.pack_into('<ff', data, 32 + 44 * k + 8, 0.0, n / 512.0)
    return [('vst', 'ReaDelay', data)], 'close (ReaDelay)'


def rev_reverb(d, tempo, post_gain=1.0, cuts=(None, None)):
    """Reverb -> ReaVerbate, the inverse of reverb(): the room size whose
    decay (through the same measured tables) is Live's DecayTime, the
    damping from the high shelf, the levels from Dry/Wet and the tail's
    measured shortfall. post_gain: a Utility right after it (what reverb()
    writes) folds into the levels; cuts: an EQ Eight right before it with
    only a low and/or high cut (ditto) - ReaVerbate's own filters."""
    damp = 0.0
    if _m(d, 'ShelfHighOn', False):
        damp = max(0.0, min(1.0, (16000.0 - _m(d, 'ShelfHiFreq', 16000.0)) / 14500.0))
    target = _m(d, 'DecayTime', 1200.0)

    def decay(room):
        k = max(0, min(9, int(room * 10)))
        f = room * 10 - k
        rt = math.exp(math.log(stock._VB_RT[k]) + f * (math.log(stock._VB_RT[k + 1]) - math.log(stock._VB_RT[k])))
        return 1000.0 * rt * verb_decay(room, damp)
    lo, hi = 0.0, 0.999
    for _ in range(40):
        mid = (lo + hi) / 2
        if decay(mid) < target:
            lo = mid
        else:
            hi = mid
    room = (lo + hi) / 2
    x = max(0.0, min(1.0, _m(d, 'MixDirect', 0.5)))
    K = _lin(verb_level_db(room, damp))
    wet, dry = post_gain * x / K, post_gain * (1.0 - x)
    width = max(-1.0, min(1.0, _m(d, 'StereoSeparation', 100.0) / 120.0))
    hp, lp = cuts
    data = stock.reaverbate(wet_db=_db(wet), dry_db=_db(dry) if dry > 1e-6 else None,
                            room=room, damping=damp, width=width,
                            delay_ms=0.0 if _m(d, 'PreDelay', 0.0) <= 0.5 + 1e-6 else _m(d, 'PreDelay', 0.0),   # 0.5 ms is Live's least
                            lowpass=lp or 20000.0, hipass=hp or 0.0)
    return [('vst', 'ReaVerbate', data)], \
        'approximate (ReaVerbate: another algorithm, decay and level matched)'


EQ8_TO_REAEQ = {2: 0, 5: 1, 3: 8, 1: 4, 0: 4, 6: 3, 7: 3}


def _bw_of_q(q):
    return 2.0 / math.log(2.0) * math.asinh(1.0 / (2.0 * max(0.1, q)))


def eq8_bands(d):
    """EQ Eight's bands (curve A) as ReaEQ's (type, on, Hz, linear gain,
    bandwidth in octaves) - eq8()'s inverse; a notch is a deep narrow bell."""
    rb = []
    for k in range(8):
        p = 'Bands.%d/ParameterA/' % k
        if not _m(d, p + 'IsOn', False):
            continue
        mode = int(_m(d, p + 'Mode', 3))
        hz, g, q = _m(d, p + 'Freq', 1000.0), _m(d, p + 'Gain', 0.0), _m(d, p + 'Q', 0.71)
        if mode == 4:
            rb.append((8, 1, hz, _lin(-30.0), _bw_of_q(q)))
            continue
        ty = EQ8_TO_REAEQ.get(mode)
        if ty is None:
            continue
        bw = _bw_of_q(q) if ty == 8 else (1.8957 if ty in (3, 4) else 2.0)
        rb.append((ty, 1, hz, _lin(g) if ty in (0, 1, 8) else 1.0, bw))
    return rb


def rev_eq8(d, tempo):
    from . import chan_eq
    rb = eq8_bands(d)
    out = [('vst', 'ReaEQ', chan_eq.reaeq_data(rb))] if rb else []
    gg = _m(d, 'GlobalGain', 0.0)
    if abs(gg) > 1e-6:
        out.append(('js', 'utility/volume', [stock.js_db(_lin(gg)), 150.0]))
    return (out, 'close (ReaEQ, band for band)') if out else ([], 'no bands on')


def rev_utility(d, tempo):
    if _m(d, 'Mute', False):
        return [('js', 'utility/volume', [-150.0, 150.0])], 'exact (muted)'
    out = []
    g = _m(d, 'Gain', 1.0)
    if abs(g - 1.0) > 1e-6:
        out.append(('js', 'utility/volume', [stock.js_db(g), 150.0]))
    if _m(d, 'Mono', False):
        out.append(stock.stereo_width(0.0, True))
    elif abs(_m(d, 'StereoWidth', 1.0) - 1.0) > 1e-6:
        out.append(stock.stereo_width(100.0 * _m(d, 'StereoWidth', 1.0)))
    return out, 'exact (REAPER volume/width)'


def rev_chorus(d, tempo):
    x = max(0.0, min(1.0, _m(d, 'DryWet', 0.5)))
    og = _m(d, 'OutputGain', 1.0)
    sl = [15.0, 3.0 if int(_m(d, 'Mode', 0)) == 1 else 2.0,
          max(0.1, min(16.0, _m(d, 'Rate', 0.6))), max(0.0, min(1.0, _m(d, 'Amount', 0.5))),
          max(-100.0, _db(x * og)), max(-100.0, _db((1.0 - x) * og))]
    return [('js', 'sstillwell/chorus', sl)], 'approximate (Stillwell Chorus)'


def rev_autofilter(d, tempo):
    """Auto Filter -> one ReaEQ band of its type at its cutoff (the
    filter standing still - an LFO or envelope on it is not carried)."""
    from . import chan_eq
    ty = {0: 3, 1: 4, 2: 7, 3: 6}.get(int(_m(d, 'FilterType', 0)), 3)
    hz = note_hz(_m(d, 'Cutoff', 135.0))
    bw = max(0.1, 2.0 - 1.6 * min(1.0, _m(d, 'Resonance', 0.0)))
    return [('vst', 'ReaEQ', chan_eq.reaeq_data([(ty, 1, hz, 1.0, bw)]))], \
        'approximate (ReaEQ, the filter at its cutoff; modulation not carried)'


LIVE_MAP = {'Compressor2': rev_compressor, 'Limiter': rev_limiter, 'Gate': rev_gate,
            'Delay': rev_delay, 'Reverb': rev_reverb, 'Eq8': rev_eq8,
            'StereoGain': rev_utility, 'Chorus2': rev_chorus, 'AutoFilter': rev_autofilter}


def _plain_gain(d):
    """A Utility that only sets a level (what gain_device writes)."""
    return (not _m(d, 'Mute', False) and not _m(d, 'Mono', False)
            and abs(_m(d, 'StereoWidth', 1.0) - 1.0) < 1e-6
            and abs(_m(d, 'Balance', 0.0)) < 1e-6)


def to_reaper(devs, tempo=120.0):
    """Live's stock devices of a chain, in order, as REAPER blocks: a list
    of (device, entries, how) - entries None where the device has no
    counterpart. The Reverb that reverb() writes (an EQ Eight of cuts in
    front, a Utility of gain behind) comes back as the one ReaVerbate."""
    devs = list(devs)
    out, k = [], 0
    while k < len(devs):
        d = devs[k]
        fn = LIVE_MAP.get(d.tag)
        if fn is None:
            out.append((d, None, None))
            k += 1
            continue
        if d.tag == 'Eq8' and k + 1 < len(devs) and devs[k + 1].tag == 'Reverb':
            rb = eq8_bands(d)
            if rb and all(b[0] in (3, 4) for b in rb) and abs(_m(d, 'GlobalGain', 0.0)) < 1e-6:
                hp = max([b[2] for b in rb if b[0] == 4] or [0.0])
                lp = min([b[2] for b in rb if b[0] == 3] or [20000.0])
                post, step = 1.0, 2
                nxt = devs[k + 2] if k + 2 < len(devs) else None
                if nxt is not None and nxt.tag == 'StereoGain' and _plain_gain(nxt):
                    post, step = _m(nxt, 'Gain', 1.0), 3
                ent, how = rev_reverb(devs[k + 1], tempo, post, (hp, lp))
                out.append((devs[k + 1], ent, how))
                k += step
                continue
        if d.tag in ('Reverb', 'Delay'):
            nxt = devs[k + 1] if k + 1 < len(devs) else None
            if nxt is not None and nxt.tag == 'StereoGain' and _plain_gain(nxt):
                ent, how = fn(d, tempo, _m(nxt, 'Gain', 1.0))
                out.append((d, ent, how))
                k += 2
                continue
        try:
            got = fn(d, tempo)
        except Exception:                # a device of another Live version
            got = None
        out.append((d, got[0] if got else None, got[1] if got else None))
        k += 1
    return out


def cubase_direct(d):
    """(Cubase effect name, its state) for a Live device Cubase has an
    effect of its own for that no REAPER block stands between: Chorus-
    Ensemble -> Chorus, a band-pass Auto Filter -> WahWah (the inverses of
    chorus() and wahwah()). None otherwise."""
    from . import builtins
    if d.tag == 'Chorus2':
        rec = {'rate': max(0.1, _m(d, 'Rate', 1.0)), 'temposync': 0.0,
               'width': 100.0 * max(0.0, min(1.0, _m(d, 'Amount', 0.5))),
               'spatial': 100.0 * max(0.0, min(1.0, _m(d, 'Width', 1.0))),
               'mix': 100.0 * max(0.0, min(1.0, _m(d, 'DryWet', 0.5))), 'bypass': 0.0}
        st = builtins.table_state('Chorus', rec)
        return ('Chorus', st) if st else None
    if d.tag == 'AutoFilter' and int(_m(d, 'FilterType', 0)) == 2:
        comp, _c = builtins._template('WahWah')
        if comp is None:
            return None
        r = builtins._records(comp)
        lo = max(20.0, r['freqlow'][1]) if 'freqlow' in r else 500.0
        hi = max(lo * 1.01, r['freqhigh'][1]) if 'freqhigh' in r else 2000.0
        hz = note_hz(_m(d, 'Cutoff', 81.0))
        x = max(0.0, min(1.0, math.log(max(hz, 1.0) / lo) / math.log(hi / lo)))
        st = builtins.table_state('WahWah', {'pedal': 100.0 * x, 'pedalEnable': 1.0, 'bypass': 0.0})
        return ('WahWah', st) if st else None
    return None


def template_uid(name):
    import json
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cubase_templates.json')
    return (json.load(open(p)).get(name) or {}).get('uid')


# ------------------------------------------------- dynamics (natives.py)
GLUE_RATIO = (2.0, 4.0, 10.0)
GLUE_ATTACK = (0.01, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0)                  # ms
GLUE_RELEASE = (100.0, 200.0, 400.0, 600.0, 800.0, 1200.0, None)       # ms, Auto


def multiband(w, data):
    """ReaXcomp -> Multiband Dynamics: three bands split where ReaXcomp's
    first two end (a fourth band joins the high one); each band's
    threshold and ratio above it (Live: ratio r in -1..0 is 1:1/(1+r)),
    attack, release and gain."""
    from . import natives
    bands = natives.reaxcomp_bands(data) or []
    if not bands:
        return None
    d = w.stock_device('multiband')
    use = bands[:3]
    if len(use) == 2:
        # two bands: the upper one fills Live's middle and high bands alike
        # (rev_multiband joins them again)
        use = [use[0], dict(use[1], top_hz=14999.0), use[1]]
    if len(bands) > 3:
        w.log.append('a ReaXcomp band past the third has no place in Multiband Dynamics; '
                     'its range plays through the high band')
    _put(d, 'SplitLowMid', use[0]['top_hz'] if len(use) > 1 else 3000.0)
    _put(d, 'SplitMidHigh', use[1]['top_hz'] if len(use) > 2 else 14999.0)
    _set(d, 'SoftKnee', any(b['knee_db'] > 1.0 for b in use))
    _set(d, 'EnvelopeIsPeak', all(b['rms_ms'] <= 1 for b in use))
    _put(d, 'GlobalAmount', 1.0)
    _put(d, 'GlobalTime', 1.0)
    _put(d, 'OutputGain', 0.0)
    for k, nm in enumerate(('Low', 'Mid', 'High')):
        b = use[k] if k < len(use) else None
        _set(d, 'Active' + nm, bool(b and b['active']))
        if not b:
            continue
        R = max(1e-3, b['ratio'])
        if R >= 1.0:
            _put(d, 'AboveThreshold' + nm, b['threshold_db'])
            _put(d, 'AboveRatio' + nm, 1.0 / R - 1.0)
            _put(d, 'BelowThreshold' + nm, -80.0)
            _put(d, 'BelowRatio' + nm, 0.0)
        else:
            _put(d, 'AboveThreshold' + nm, 0.0)
            _put(d, 'AboveRatio' + nm, 0.0)
            _put(d, 'BelowThreshold' + nm, b['threshold_db'])
            _put(d, 'BelowRatio' + nm, max(-3.0, 1.0 - 1.0 / R))
        _put(d, 'Attack' + nm, max(0.1, b['attack_ms']))
        _put(d, 'Release' + nm, max(0.1, b['release_ms']))
        _put(d, 'Gain' + nm, b['gain_db'])
        _put(d, 'InputGain' + nm, 0.0)
    return d, 'close (Live Multiband Dynamics, band for band)'


def expander_js(w, sliders):
    """REAPER's Downward Expander -> Live's Compressor in its Expand model
    (its expansion ratio stops at 1:2)."""
    v = list(sliders) + [0.0] * 7
    d = w.stock_device('compressor')
    _set(d, 'Model', 2)
    _put(d, 'Threshold', _lin(v[0]))
    if v[1] > 2.0:
        w.log.append("an expansion ratio of %.1f is past Live Compressor's 1:2 and is set "
                     "to that" % v[1])
    _put(d, 'ExpansionRatio', max(1.0, min(2.0, v[1])))
    _put(d, 'Attack', max(0.01, v[5]))
    _put(d, 'Release', max(1.0, v[6]))
    _put(d, 'Gain', v[2])
    _set(d, 'GainCompensation', False)
    _put(d, 'DryWet', 1.0)
    return d, 'close (Live Compressor, Expand)'


def rev_glue(d, tempo):
    """Glue Compressor -> ReaComp: its stepped ratio, attack and release as
    numbers (Release Auto is ReaComp's auto release), Makeup, Dry/Wet."""
    from . import natives
    ratio = GLUE_RATIO[max(0, min(2, int(_m(d, 'Ratio', 1))))]
    att = GLUE_ATTACK[max(0, min(6, int(_m(d, 'Attack', 3))))]
    rel = GLUE_RELEASE[max(0, min(6, int(_m(d, 'Release', 6))))]
    ent = natives._comp(_m(d, 'Threshold', 0.0), ratio, att, rel or 400.0, _m(d, 'Makeup', 0.0),
                        6.0, _m(d, 'DryWet', 1.0), auto_release=rel is None)
    return ent, "close (ReaComp; the Glue's soft clip and range are not carried)"


def rev_multiband(d, tempo):
    """Multiband Dynamics -> ReaXcomp: the three bands' compression above
    their thresholds (below-threshold processing has no ReaXcomp place)."""
    from . import natives
    bands = []
    tops = (_m(d, 'SplitLowMid', 120.0), _m(d, 'SplitMidHigh', 2500.0), 24000.0)
    below = False
    for k, nm in enumerate(('Low', 'Mid', 'High')):
        r = _m(d, 'AboveRatio' + nm, 0.0)
        R = 1.0 / max(1e-3, 1.0 + r)
        thr = _m(d, 'AboveThreshold' + nm, 0.0)
        br, bt = _m(d, 'BelowRatio' + nm, 0.0), _m(d, 'BelowThreshold' + nm, -80.0)
        if abs(r) < 1e-4 and br < -1e-4 and bt > -79.0:
            # a band that only expands below its threshold: ReaXcomp's
            # ratio under 1 does that
            R, thr = 1.0 / (1.0 - br), bt
        else:
            below = below or (bt > -79.0 and abs(br) > 1e-3)
        bands.append(dict(top_hz=tops[k], gain_db=_m(d, 'Gain' + nm, 0.0) + _m(d, 'InputGain' + nm, 0.0),
                          threshold_db=thr, ratio=R,
                          knee_db=6.0 if _m(d, 'SoftKnee', True) else 0.0,
                          attack_ms=_m(d, 'Attack' + nm, 10.0) * _m(d, 'GlobalTime', 1.0),
                          release_ms=_m(d, 'Release' + nm, 100.0) * _m(d, 'GlobalTime', 1.0),
                          rms_ms=0.0 if _m(d, 'EnvelopeIsPeak', False) else 5.0, makeup=False,
                          active=bool(_m(d, 'Active' + nm, True))))
    # neighbours with the same settings are one band (multiband() splits
    # a two-band ReaXcomp so)
    keys = ('gain_db', 'threshold_db', 'ratio', 'knee_db', 'attack_ms', 'release_ms', 'rms_ms', 'active')
    joined = [bands[0]]
    for b in bands[1:]:
        if all(abs(float(b[k]) - float(joined[-1][k])) < 1e-6 for k in keys):
            joined[-1] = dict(joined[-1], top_hz=b['top_hz'])
        else:
            joined.append(b)
    ent = [('vst', 'ReaXcomp', natives.reaxcomp(joined))]
    og = _m(d, 'OutputGain', 0.0)
    if abs(og) > 1e-6:
        ent.append(('js', 'utility/volume', [stock.js_db(_lin(og)), 150.0]))
    return ent, 'close (ReaXcomp, band for band%s)' % (
        '; its below-threshold settings are not carried' if below else '')


REAPER_MAP['ReaXcomp'] = multiband
LIVE_MAP['GlueCompressor'] = rev_glue
LIVE_MAP['MultibandDynamics'] = rev_multiband
JS_MAP = {'sstillwell/expander': expander_js}


def _js_eq(path):
    def fn(w, sliders):
        from . import natives
        got = natives.js_eq_bands(path, sliders)
        if not got:
            return None
        bands, out = got
        d, how = eq8(w, bands)
        _put(d, 'GlobalGain', out)
        return d, 'close (Live EQ Eight, band for band)'
    return fn


for _p in ('sstillwell/hpflpf', 'sstillwell/rbj4eq', 'sstillwell/rbj7eq', 'loser/3BandEQ', 'loser/4BandEQ'):
    JS_MAP[_p] = _js_eq(_p)
