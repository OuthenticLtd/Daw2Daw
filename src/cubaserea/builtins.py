"""Cubase's own effects: their saved states, read and built.

Cubase's own effects (Cubase Plug-in Set.vst3) load in no other host, and
REAPER's own in no other host either. Each side gets the other's closest
stock effect (stock.py for Cubase -> REAPER, cubase_equivalent here for
REAPER's JS effects -> Cubase); nothing is ever installed.

MonoToStereo (Steinberg), measured 2026-09-29 on Cubase 15 exports: it
folds the input to m = (L + R) / 2 and adds a side signal - m delayed by
Delay, filtered by two first-order high-pass and two first-order low-pass
sections whose cutoffs Colour sets, times 0.510257 x Width / 100. Width 0 or
Mono Out is the plain fold, which REAPER's stock utility/channelmixer at
its defaults (-6 dB on all four paths, gains 2^(dB/6) = 0.5) computes.
"""
import struct

MONO_TO_STEREO_UID = '1AF350AC983B46CAB104990A0726EAD6'

# Cubase's meters: they pass the sound through untouched, so leaving one out
# changes nothing that is heard (SuperVision is in 14 of the user's
# projects, Tuner in one)
METER_UIDS = {
    '56535453636F336D756C746973636F70': 'SuperVision',
    '6B9B08D2CA294270BF092A62865521BF': 'Tuner',
}


def is_meter(fx):
    return (fx.uid or '').upper() in METER_UIDS
CHANNEL_MIXER = 'utility/channelmixer'


def mono_to_stereo_state(width=83.0, delay=12.175, colour=45.0, monoout=0.0):
    """MonoToStereo's component state: u32 0x360, then per parameter a
    128-byte name, u32 id and f64 value (as Cubase saves it)."""
    recs = [('width', 0, width), ('delay', 1, delay), ('color', 2, colour),
            ('monoout', 3, monoout), ('bypass', 8, 0.0), ('widthVu', 10, 0.0)]
    body = b''.join(n.encode('latin1').ljust(128, b'\0') + struct.pack('<Id', i, v)
                    for n, i, v in recs)
    return struct.pack('<I', 0x360) + body




def mono_to_stereo_params(state):
    """(width, delay, colour, monoout) from MonoToStereo's saved state."""
    vals = {}
    o = 4
    while o + 140 <= len(state or b''):
        name = state[o:o + 128].split(b'\0')[0].decode('latin1')
        pid, val = struct.unpack_from('<Id', state, o + 128)
        vals[name] = val
        o += 140
    return (vals.get('width', 100.0), vals.get('delay', 12.0),
            vals.get('color', 50.0), vals.get('monoout', 0.0))


VOLUME_UID = 'C0EF553E8A5540998CA28C00D8FB0A2A'
# a Volume state as Cubase saved it (Krastovden's Kaval, gain +6 dB),
# zlib + base64: the records are rewritten in it for the way back
_VOLUME_TEMPLATE = (
    'eNrFlz0sQ1EUx89rfSuK+toaCbPvr4VGGBApkg4WKUoEbaM6+EhYrEgk0kk6GjsanxiaEKNVSoNGSJqwWDx97r19754gWM4Zevtvz+35vftv/nnPAwMwDhPQDB4IwBKEYRl8kKljzxCY6vRo8/Bt63uNayQify+0+8Yh9QW5Tmuaxj8aUDIv7xmtMP3qTxr9ysGlpGEbfizB4Lpj84W2Zzk2JC59rl7x4Zcsh15n4alBXasJh3T9jfxXLHxtQPPFflHq9dWo/vnFDtvv5H0qny/64IHp83u2Wnmf80Huc35z3bHgLjv/2ISkt7lWkD/A5wt+cT5xft37nEOfl2/aphm+fZbos/9sS9YPUdFb2Y/E7e/8cPM+pZ7t/qsf0eTXfriT8jnvp5ieTMl+xFL/80P8N/s8LqlP7WD62MJWdY6tJ1wLP8Qc4Ufa5oLflME3xvyqAJj3LviBuPpteT2RJ61XZ2ki5FCy79KfLM2ELBbE0kLIYkUsrYQsOYiljZAlF7G0E7LkIZYOQpZ8xNJJyFKAWLoIWQoRSzchSxHOOsLgLcYshMFrwyyEwVuCWQiDtxSzEAZvGWYhDF47ZiEM3nLMQhi8FZiFMHgrMQth8DrwfR1h7lZhFsLcrcYshLlbg1kIc7cWsxDmbp2JZcUX8q0S3r88Gg8lsBSYWaR8jn02sUyvBb2hEN257BphByte/2xgeWHdR8SyZ2L5AIepToE=')


def _records(state):
    """name -> (offset of its f64, value) for a Steinberg effect state made
    of 128-byte names each followed by a u32 id and an f64."""
    out = {}
    o = 0
    data = state or b''
    # the usual layout: u32 size of the rest, then 140-byte records whose
    # 128-byte name field may hold leftover bytes after its NUL (Cubase's
    # factory presets of Gate, Limiter, Expander, ... do) - read by stride
    # (the leading u32 is not always the size - StudioEQ, DJ-Eq and
    # VintageCompressor write other numbers there; the stride is what holds)
    first = data[4:132].split(b'\0', 1)[0] if len(data) >= 144 else b''
    if (len(data) - 4) % 140 == 0 and first[:1].isalpha() and first.isascii() \
            and all(c == 95 or chr(c).isalnum() for c in first):
        for o in range(4, len(data) - 139, 140):
            name = data[o:o + 128].split(b'\0', 1)[0]
            try:
                key = name.decode('ascii')
            except UnicodeDecodeError:
                continue
            if key:
                out.setdefault(key, (o + 132, struct.unpack_from('<d', data, o + 132)[0]))
        return out
    while o + 140 <= len(data):
        raw = data[o:o + 128]
        name = raw.split(b'\0', 1)[0]
        if name and len(name) < 40 and all(32 < c < 127 for c in name) \
                and raw[len(name):].strip(b'\0') == b'':
            out.setdefault(name.decode('ascii'),
                           (o + 132, struct.unpack_from('<d', data, o + 132)[0]))
            o += 140
        else:
            o += 1
    return out


# Cubase's Volume does not play its gain records as linear gain: renders
# of the drum loop through it (Cubase 15) give this curve, record -> dB.
# Exactly 1.0 is 0 dB and exactly 4.0 +12.04 dB, but 0.999 is -0.86 and
# 3.999 +6.02: the curve is smooth around those two, so a level is written
# as 1.0 when it is unity and off the curve's points otherwise, and a
# Volume gives at most +6.02 dB (VOLUME_MAX_DB; more takes a second one).
VOLUME_CURVE = ((0.02, -28.02), (0.05, -21.44), (0.1, -15.53), (0.15, -12.03), (0.2, -9.91),
                (0.25, -8.43), (0.3, -7.31), (0.4, -5.58), (0.5, -4.17), (0.6, -3.17),
                (0.7071, -2.35), (0.8, -1.79), (0.9, -1.28), (0.95, -1.058), (0.99, -0.892),
                (0.999, -0.856), (1.001, -0.848), (1.01, -0.812), (1.05, -0.66), (1.1, -0.48),
                (1.2, -0.16), (1.4142, 0.86), (1.5, 1.25), (1.75, 2.20), (2.0, 2.94), (2.5, 4.06),
                (3.0, 4.87), (3.5, 5.51), (3.6, 5.617), (3.8, 5.827), (3.9, 5.925), (3.95, 5.973),
                (3.99, 6.011), (3.999, 6.020))
VOLUME_MAX_DB = 6.02


def volume_db_of(rec):
    """The level (dB) a Volume gain record plays at."""
    import math
    if rec <= 3e-8:
        return -150.0
    if abs(rec - 1.0) < 1e-12:
        return 0.0
    if rec >= 4.0:
        return 12.04
    c = VOLUME_CURVE
    if rec <= c[0][0]:
        # below the measured points: its slope there, per dB of the record
        s = (c[1][1] - c[0][1]) / (20 * math.log10(c[1][0] / c[0][0]))
        return c[0][1] + s * 20 * math.log10(rec / c[0][0])
    for (x0, y0), (x1, y1) in zip(c, c[1:]):
        if rec <= x1:
            return y0 + (y1 - y0) * (rec - x0) / (x1 - x0)
    return c[-1][1]


def volume_record(lin):
    """The Volume gain record that plays linear gain lin (to +6.02 dB)."""
    import math
    if lin <= 3e-8:
        return 0.0
    db = 20 * math.log10(lin)
    if abs(db) < 0.005:
        return 1.0
    c = VOLUME_CURVE
    if db >= c[-1][1]:
        return c[-1][0]
    if db <= c[0][1]:
        s = (c[1][1] - c[0][1]) / (20 * math.log10(c[1][0] / c[0][0]))
        return c[0][0] * 10 ** ((db - c[0][1]) / s / 20)
    for (x0, y0), (x1, y1) in zip(c, c[1:]):
        if db <= y1:
            return x0 + (x1 - x0) * (db - y0) / (y1 - y0)
    return c[-1][0]


def volume_split(lin):
    """Linear gain lin as Volumes' gains, each within one Volume's reach."""
    import math
    out = []
    while lin > 10 ** (VOLUME_MAX_DB / 20) * (1 + 1e-9):
        step = 10 ** (VOLUME_MAX_DB / 20)
        out.append(step)
        lin /= step
    return out + [lin]


def volume_params(state):
    """(gain, channel 1 gain, channel 2 gain, bypass) of a Volume state,
    the gains linear (what they play at)."""
    r = _records(state)
    g = lambda k, d: r[k][1] if k in r else d
    lin = lambda k: 10 ** (volume_db_of(g(k, 1.0)) / 20) if g(k, 1.0) > 3e-8 else 0.0
    return lin('gain'), lin('gain0'), lin('gain1'), g('bypass', 0.0)


def volume_state(gain=1.0, g0=1.0, g1=1.0, bypass=0.0):
    """A Volume component state with these settings (the template's other
    records unchanged)."""
    import base64
    import zlib
    b = bytearray(zlib.decompress(base64.b64decode(_VOLUME_TEMPLATE)))
    r = _records(bytes(b))
    # the gains are linear, written as the records that play them
    for k, v in (('gain', volume_record(gain)), ('gain0', volume_record(g0)), ('gain1', volume_record(g1)),
                 ('bypass', bypass)):
        if k in r:
            struct.pack_into('<d', b, r[k][0], float(v))
    return bytes(b)


# Cubase effects whose REAPER stand-in takes its settings straight from the
# saved records: uid -> (name, [(record, default)]).
# The way back rebuilds Cubase's state from a template
# (cubase_templates.json, one real state per effect from the user's
# projects) with those records written in.
TABLE = {
    '94DEB7BF378041EE9E2FEDA24E19EF60': ('Brickwall Limiter',
        [('threshold', -0.3), ('release', 3.0), ('autorelease', 0.0),
         ('link', 1.0), ('bypass', 0.0)]),
    'B94789B3C4C944EFB0058694DAB8704E': ('Limiter',
        [('input', 0.0), ('release', 500.0), ('autorelease', 0.0),
         ('output', 0.0), ('bypass', 0.0)]),
    '5B38F28281144FFE80285FF7CCF20483': ('Compressor',
        [('threshold', -20.0), ('ratio', 2.0), ('attack', 1.0), ('release', 100.0),
         ('autorelease', 0.0), ('hold', 0.0), ('makeUp', 0.0), ('automakeup', 0.0),
         ('softknee', 0.0), ('rms', 0.0), ('limit', 0.0), ('drymix', 0.0),
         ('live', 0.0), ('bypass', 0.0)]),
    '001DCD3345D14A13B59DAECF75A37536': ('StereoDelay',
        [('delay1', 100.0), ('temposync1', 0.0), ('syncnote1', 0.0), ('feedback1', 50.0),
         ('filterL1', 50.0), ('filterLOn1', 1.0), ('filterH1', 15000.0), ('filterHOn1', 1.0),
         ('mix1', 100.0), ('pan1', -100.0),
         ('delay2', 20.0), ('temposync2', 0.0), ('syncnote2', 0.0), ('feedback2', 50.0),
         ('filterL2', 50.0), ('filterLOn2', 1.0), ('filterH2', 15000.0), ('filterHOn2', 1.0),
         ('mix2', 100.0), ('pan2', 100.0), ('bypass', 0.0)]),
}


_DEFAULTS = None


def defaults():
    """name -> {uid, component, controller} of Cubase 15's own effects at
    their defaults (cubase_defaults.json)."""
    global _DEFAULTS
    if _DEFAULTS is None:
        import json
        import os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cubase_defaults.json')
        try:
            _DEFAULTS = json.load(open(p))
        except OSError:
            _DEFAULTS = {}
    return _DEFAULTS


def name_of_uid(uid):
    """The Cubase effect a class id belongs to, if it is one of Cubase's own."""
    uid = (uid or '').upper()
    for k, v in defaults().items():
        if v.get('uid', '').upper() == uid:
            return k.split(' ')[0] if k.endswith(uid[:6]) else k
    return None


def uid_of_name(name):
    t = defaults().get(name)
    return t.get('uid') if t else None


def _template(name):
    import base64
    import json
    import os
    import zlib
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cubase_templates.json')
    t = json.load(open(p)).get(name)
    if not t:
        # every other Cubase effect at the defaults Cubase 15 itself saved
        # (an empty state loaded and saved back - cubase_defaults.json)
        t = defaults().get(name)
    if not t:
        return None, b''
    return (zlib.decompress(base64.b64decode(t['component'])),
            zlib.decompress(base64.b64decode(t['controller'])) if t.get('controller') else b'')


def table_state(name, recs):
    """A Cubase state for effect `name` with these record values."""
    comp, _ctrl = _template(name)
    if comp is None:
        return None
    b = bytearray(comp)
    r = _records(comp)
    for k, v in recs.items():
        if k in r:
            struct.pack_into('<d', b, r[k][0], float(v))
    return bytes(b)


STEREO_ENHANCER_UID = '77BBA7CA90F14C9BB298BA9010D6DD78'
STILLWELL_WIDTH = 'sstillwell/stereowidth'


def stereo_enhancer_state(width, mono=0.0, bypass=0.0):
    return table_state('StereoEnhancer', {'width': width, 'delayon': 0.0, 'colouron': 0.0,
                                          'monoout': mono, 'bypass': bypass})


def cubase_equivalents(js_path, sliders):
    """[(uid, component state, name)] of the Cubase effects that together
    play exactly like a REAPER JS effect, or None. Mostly one effect; the
    Stillwell Stereo Width is two: StereoEnhancer (an exact mid/side matrix,
    measured) for the width and Volume for the level."""
    name = js_path.replace(chr(92), '/').lower()
    if name.endswith(STILLWELL_WIDTH):
        vals = []
        for v in sliders:
            try:
                vals.append(float(v))
            except ValueError:
                break
        vals += [0.0] * (5 - len(vals))
        wb_db, cb_db, g_db, bal, rot = vals[:5]
        if abs(bal) > 1e-9 or abs(rot) > 1e-9:
            return None         # a balance or rotation is not a mid/side matrix
        import math
        wb, cb, g = (10 ** (x / 20.0) for x in (wb_db, cb_db, g_db))
        # the JS: L' = g ((1 + cb) mid + (1 + wb sqrt2) side), R' likewise
        mg = g * (1 + cb)
        sg = g * (1 + wb * 2 * math.cos(math.pi / 4))
        width = 100.0 * sg / mg
        if width < 0.0:
            return None
        # StereoEnhancer stops at 200 %; in series their side gains multiply
        n = 1
        while width / 100.0 > 2.0 ** n and n < 8:
            n += 1
        each = 100.0 * (width / 100.0) ** (1.0 / n)
        return [(STEREO_ENHANCER_UID, stereo_enhancer_state(each), 'StereoEnhancer')] * n + \
               [(VOLUME_UID, volume_state(mg), 'Volume')]
    if name.endswith(CHANNEL_MIXER):
        # a symmetric channel mixer (L->L = R->R, L->R = R->L) is a mid/side
        # matrix too: mid gain ll + lr, side gain ll - lr - StereoEnhancer's
        # width for the side over the mid, Volume for the mid (what
        # stock.stereo_width writes for Cubase's StereoEnhancer up to 100 %)
        vals = []
        for v in sliders:
            try:
                vals.append(float(v))
            except ValueError:
                break
        if len(vals) >= 4 and abs(vals[0] - vals[1]) < 1e-6 and abs(vals[2] - vals[3]) < 1e-6 \
                and vals[2] > -119.0 and abs(vals[0] - vals[2]) > 1e-6:
            ll, lr = 2.0 ** (vals[0] / 6.0), 2.0 ** (vals[2] / 6.0)
            mid, side = ll + lr, ll - lr
            if side >= 0.0:
                out = [(STEREO_ENHANCER_UID, stereo_enhancer_state(100.0 * side / mid), 'StereoEnhancer')]
                if abs(mid - 1.0) > 1e-6:
                    out.append((VOLUME_UID, volume_state(mid), 'Volume'))
                return out
    one = cubase_equivalent(js_path, sliders)
    return [one] if one else None


def cubase_equivalent(js_path, sliders):
    """(uid, component state, name) of the Cubase effect that plays like
    one of REAPER's own JS effects with these sliders, or None."""
    import math
    path = js_path.replace(chr(92), '/').lower()
    vals = []
    for v in sliders:
        try:
            vals.append(float(v))
        except ValueError:
            break
    js = lambda s_: 2.0 ** (s_ / 6.0)     # REAPER's utility JS dB sliders
    if path.endswith('utility/volume') and vals:
        return VOLUME_UID, volume_state(js(vals[0])), 'Volume'
    if path.endswith(CHANNEL_MIXER) and len(vals) >= 4:
        ll, rr, lr, rl = vals[:4]
        if max(lr, rl) <= -119.0:
            # no crossfeed: a gain per side, which Cubase's Volume has
            return VOLUME_UID, volume_state(1.0, js(ll), js(rr)), 'Volume'
        if abs(ll - rr) < 1e-6 and abs(ll - lr) < 1e-6 and abs(ll - rl) < 1e-6 and abs(ll + 6.0) < 1e-6:
            # all four paths -6 dB: an exact mono fold, MonoToStereo with no width
            return MONO_TO_STEREO_UID, mono_to_stereo_state(0.0, 12.175, 45.0, 0.0), 'MonoToStereo'
        return None
    if path.endswith('loser/transientcontroller') and len(vals) >= 3:
        # REAPER's Transient Controller -> Cubase's EnvelopeShaper, the
        # closest of Cubase's own: attack/sustain % as +-20 dB
        st = table_state('EnvelopeShaper', {'attackgain': max(-20.0, min(20.0, vals[0] / 5.0)),
                                            'releasegain': max(-20.0, min(20.0, vals[1] / 5.0)),
                                            'output': vals[2], 'bypass': 0.0})
        if st is not None:
            return ENVELOPE_SHAPER_UID, st, 'EnvelopeShaper'
    return None


ENVELOPE_SHAPER_UID = 'C3D60417A5BB4FB288CB1A75FA641EDF'


def merge_volumes(fxs):
    """Cubase Volume inserts next to each other, all switched on, no lane
    on any, each the same on both channels: one gain, in as few Volumes as
    that needs (volume_split; a round trip through REAPER or Live stacked
    them). Returns (new list, how many went)."""
    out, gone = [], 0
    run = []

    def flush():
        nonlocal gone
        if len(run) < 2:
            out.extend(run)
        else:
            g = 1.0
            for f in run:
                gain, g0, g1, _b = volume_params(f.component)
                g *= gain * g0
            parts = volume_split(g)
            for k, part in enumerate(parts):
                f = run[k]
                f.component = volume_state(part)
                out.append(f)
            gone += len(run) - len(parts)
        run.clear()

    for f in fxs:
        ok = ((f.uid or '').upper() == VOLUME_UID and not f.bypass and not f.offline
              and not getattr(f, 'envelopes', None) and not getattr(f, 'fader_overflow', False))
        if ok:
            gain, g0, g1, byp = volume_params(f.component)
            ok = abs(g0 - g1) < 1e-9 and byp < 0.5
        if ok:
            run.append(f)
        else:
            flush()
            out.append(f)
    flush()
    return out, gone
