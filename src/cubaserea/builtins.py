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


def volume_params(state):
    """(gain, channel 1 gain, channel 2 gain, bypass) of a Volume state."""
    r = _records(state)
    g = lambda k, d: r[k][1] if k in r else d
    return g('gain', 1.0), g('gain0', 1.0), g('gain1', 1.0), g('bypass', 0.0)


def volume_state(gain=1.0, g0=1.0, g1=1.0, bypass=0.0):
    """A Volume component state with these settings (the template's other
    records unchanged)."""
    import base64
    import zlib
    b = bytearray(zlib.decompress(base64.b64decode(_VOLUME_TEMPLATE)))
    r = _records(bytes(b))
    for k, v in (('gain', gain), ('gain0', g0), ('gain1', g1), ('bypass', bypass)):
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


def _template(name):
    import base64
    import json
    import os
    import zlib
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cubase_templates.json')
    t = json.load(open(p)).get(name)
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
