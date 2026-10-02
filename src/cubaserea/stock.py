"""REAPER's own plug-ins, written with settings - no REAPER needed.

The conversion to REAPER uses only what every REAPER installation has:
the Cockos plug-ins (ReaEQ, ReaComp, ReaDelay, ReaLimit, ...) and the JS
effects REAPER ships (sstillwell/stereowidth, utility/volume, ...). A Cubase
effect that no other host loads becomes the closest of those; the report
says how close.

A Cockos plug-in's state in an .rpp is three base64 blocks: REAPER's VST
header (whose last-but-two u32 is the size of the next block), the
plug-in's own data, and a fixed trailer. The templates below are the
default instances REAPER 7 saved (2026-09-30); a setting is written into
the data block at the place REAPER keeps it (measured by setting each
parameter from a script and reading the saved state back):
  ReaComp   raw parameter values, f32, from byte 8, in parameter order
            (threshold/dry/wet linear 0..2, ratio 1 + 99 v, attack 500 v ms,
            release 5000 v ms, RMS 100 v ms, knee 24 v dB, filters 20000 v Hz)
  ReaDelay  tap count, wet, dry (linear), then per tap 11 f32 values: a flag, enabled,
            length (ms / 10000), musical length, feedback (linear), lowpass
            and hipass (Hz / 20000), resolution, stereo width ((w+1)/2),
            volume (linear), pan ((p+1)/2)
  ReaLimit  threshold and ceiling as f64 dB
"""
import base64
import os
import struct

TEMPLATES = {
    'ReaComp': ('<VST "VST: ReaComp (Cockos)" reacomp.dll 0 "" 1919247213<5653547265636D726561636F6D700000> ""',
                'bWNlcu9e7f4EAAAAAQAAAAAAAAACAAAAAAAAAAQAAAAAAAAACAAAAAAAAAACAAAAAQAAAAAAAAACAAAAAAAAAFwAAAAAAAAAAAAQAA==',
                '776t3g3wrd4AAIA/ED74PKabxDsK16M8AAAAAAAAAAAAAIA/AAAAAAAAAAAAAAAAnNEHMwAAgD8AAAAAzcxMPQAAAAAAAAAAAAAAAAAAgD4AAAAAAAAAAAAAAAA=',
                'AAAQAAAA'),
    'ReaDelay': ('<VST "VST: ReaDelay (Cockos)" readelay.dll 0 "" 1919247468<5653547265646C72656164656C617900> ""',
                 'bGRlcu5e7f4CAAAAAQAAAAAAAAACAAAAAAAAAAIAAAABAAAAAAAAAAIAAAAAAAAATAAAAAEAAAAAABAA',
                 'AAAAAAAAAAABAAAALAAAAAIAAAAAAAAAQwIAPwAAgD8AAAAAAACAPwAAAAAAAAA8nNEHMwAAgD8AAAAAAACAPwAAgD8AAIA/AAAAPw==',
                 'AAAQAAAA'),
    'ReaLimit': ('<VST "VST: ReaLimit (Cockos)" realimit.dll 0 "" 1919708532<565354726C6D747265616C696D697400> ""',
                 'dG1scu5e7f4CAAAAAQAAAAAAAAACAAAAAAAAAAIAAAABAAAAAAAAAAIAAAAAAAAAMAAAAAEAAAAAABAA',
                 'AwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAAACAAAAAAAAAAAAAD+fduMQw8Y/',
                 'AAAQAAAA'),
    'ReaVerbate': ('<VST "VST: ReaVerbate (Cockos)" reaverbate.dll 0 "" 1920361016<56535472766238726561766572626174> ""',
                   'OGJ2cu9e7f4CAAAAAQAAAAAAAAACAAAAAAAAAAIAAAABAAAAAAAAAAIAAAAAAAAAKAAAAAAAAAAAABAA',
                   '776t3g3wrd4X2U4+/KlxPnE9Cj7jpRs+Vg4tPsl2Pj47308+rkdhPg==', 'AAAQAAAA'),
    'ReaGate': ('<VST "VST: ReaGate (Cockos)" reagate.dll 0 "" 1919248244<56535472656774726561676174650000> ""',
                'dGdlcu9e7f4EAAAAAQAAAAAAAAACAAAAAAAAAAQAAAAAAAAACAAAAAAAAAACAAAAAQAAAAAAAAACAAAAAAAAAFwAAAAAAAAAAAAQAA==',
                '776t3g3wrd4X2U4+/KnxPXE9Cj7jpRs+Vg4tPsl2Pj47308+rkdhPiGwcj5KDAI/g8AKP7x0Ez/2KBw/L92kPsP1WECiRbY+2/m+PhSuxz5OYlA/AAAAAMHK4T4=',
                'AAAQAAAA'),
    'ReaPitch': ('<VST "VST: ReaPitch (Cockos)" reapitch.dll 0 "" 1919250531<56535472657063726561706974636800> ""',
                 'Y3Blcu5e7f4CAAAAAQAAAAAAAAACAAAAAAAAAAIAAAABAAAAAAAAAAIAAAAAAAAATAAAAAEAAAAAABAA',
                 'AAAAAP////8BAAAALAAAAAIAAAAAAAAAF9lOPvypcT4AAAAAcT0KPuOlGz5WDi0+yXY+PjvfTz6uR2E+IbByPkoMgj6DwAo/vHSTPg==',
                 'AAAQAAAA'),
    'ReaEQ': ('<VST "VST: ReaEQ (Cockos)" reaeq.dll 0 "" 1919247729<56535472656571726561657100000000> ""',
              'cWVlcu5e7f4CAAAAAQAAAAAAAAACAAAAAAAAAAIAAAABAAAAAAAAAAIAAAAAAAAAzQAAAAEAAAAAABAA',
              None, 'AAAQAAAA'),
}


def _data(name):
    return bytearray(base64.b64decode(TEMPLATES[name][2]))


def vst_lines(name, data, indent='      ', bypass=0, offline=0, fxid=None):
    """The .rpp lines of a Cockos plug-in with this data block."""
    head, hdr_b64 = TEMPLATES[name][0], TEMPLATES[name][1]
    hdr = bytearray(base64.b64decode(hdr_b64))
    struct.pack_into('<I', hdr, len(hdr) - 12, len(data))
    b64 = base64.b64encode(bytes(data)).decode()
    lines = ['%sBYPASS %d %d 0' % (indent, bypass, offline), indent + head,
             '%s  %s' % (indent, base64.b64encode(bytes(hdr)).decode())]
    lines += ['%s  %s' % (indent, b64[i:i + 128]) for i in range(0, len(b64), 128)]
    lines += ['%s  %s' % (indent, TEMPLATES[name][3]), indent + '>', indent + 'FLOATPOS 0 0 0 0']
    if fxid:
        lines.append('%sFXID %s' % (indent, fxid))
    lines.append(indent + 'WAK 0 0')
    return lines


def js_lines(path, sliders, indent='      ', bypass=0, offline=0, fxid=None):
    """The .rpp lines of one of REAPER's own JS effects with its sliders."""
    vals = ['%.10g' % v for v in sliders] + ['-'] * (64 - len(sliders))
    lines = ['%sBYPASS %d %d 0' % (indent, bypass, offline),
             '%s<JS %s ""' % (indent, path),
             '%s  %s' % (indent, ' '.join(vals)),
             indent + '>', indent + 'FLOATPOS 0 0 0 0']
    if fxid:
        lines.append('%sFXID %s' % (indent, fxid))
    lines.append(indent + 'WAK 0 0')
    return lines


def db2lin(db):
    return 10 ** (db / 20.0)


def reacomp(threshold_db=0.0, ratio=4.0, attack_ms=3.0, release_ms=100.0,
            knee_db=0.0, rms_ms=5.0, makeup_db=0.0, auto_makeup=False,
            auto_release=False, precomp_ms=0.0, dry_db=None, limit=False):
    """A ReaComp data block. ReaComp's makeup is its Wet gain."""
    d = _data('ReaComp')
    raw = {0: min(2.0, db2lin(threshold_db)),
           1: 1.0 if (limit or ratio >= 100) else (ratio - 1.0) / 99.0,
           2: attack_ms / 500.0, 3: release_ms / 5000.0, 4: precomp_ms / 250.0,
           10: 0.0 if dry_db is None else min(2.0, db2lin(dry_db)),
           11: min(2.0, db2lin(makeup_db)),
           13: rms_ms / 100.0, 14: knee_db / 24.0,
           15: 1.0 if auto_makeup else 0.0, 16: 1.0 if auto_release else 0.0}
    for p, v in raw.items():
        struct.pack_into('<f', d, 8 + 4 * p, float(v))
    return d


def readelay(taps, wet_db=0.0, dry_db=0.0):
    """A ReaDelay data block. taps: dicts with ms, feedback (linear),
    lowpass, hipass (Hz), width (-1..1), volume (linear), pan (-1..1)."""
    t = _data('ReaDelay')
    # the template: 8 bytes, u32 tap count, u32 bytes per tap, 16 bytes
    # ending in wet and dry (f32), then the taps of 11 f32 each
    one = t[32:32 + 44]
    out = bytearray(t[:8]) + struct.pack('<II', len(taps), 44) + t[16:32]
    struct.pack_into("<ff", out, 24, db2lin(wet_db), 0.0 if dry_db is None else db2lin(dry_db))
    for tp in taps:
        b = bytearray(one)
        vals = {1: 1.0, 2: tp.get('ms', 0.0) / 10000.0, 3: 0.0,
                4: tp.get('feedback', 0.0), 5: min(1.0, tp.get('lowpass', 20000.0) / 20000.0),
                6: tp.get('hipass', 0.0) / 20000.0, 8: (tp.get('width', 1.0) + 1) / 2.0,
                9: tp.get('volume', 1.0), 10: (tp.get('pan', 0.0) + 1) / 2.0}
        for k, v in vals.items():
            struct.pack_into('<f', b, 4 * k, float(v))
        out += b
    return out


def realimit(threshold_db=0.0, ceiling_db=0.0):
    """A ReaLimit data block: threshold and ceiling as f64 dB at bytes 8
    and 16 (the release, f64 at 40, keeps REAPER's default)."""
    d = _data('ReaLimit')
    struct.pack_into('<d', d, 8, float(threshold_db))
    struct.pack_into('<d', d, 16, float(ceiling_db))
    return d


def reaverbate(wet_db=0.0, dry_db=0.0, room=0.5, damping=0.3, width=1.0,
               delay_ms=0.0, lowpass=20000.0, hipass=0.0):
    """A ReaVerbate data block: after its 8-byte magic, wet and dry
    (linear 0..2), room size and dampening (0..1), width ((w+1)/2),
    pre-delay (ms / 500), lowpass and hipass (Hz / 20000), all f32."""
    d = _data('ReaVerbate')
    lin = lambda db: 0.0 if db is None else min(2.0, db2lin(db))
    vals = [lin(wet_db), lin(dry_db), room, damping, (width + 1) / 2.0,
            delay_ms / 500.0, min(1.0, lowpass / 20000.0), hipass / 20000.0]
    for k, v in enumerate(vals):
        struct.pack_into('<f', d, 8 + 4 * k, float(max(0.0, min(2.0, v))))
    return d


def reagate(threshold_db=-40.0, attack_ms=3.0, release_ms=100.0, hold_ms=0.0,
            preopen_ms=0.0, rms_ms=0.0, lowpass=20000.0, hipass=0.0, closed_db=None):
    """A ReaGate data block: raw values from byte 8, in parameter order
    (threshold/dry/wet linear 0..2, attack 500 v ms, release 5000 v,
    pre-open 250 v, hold 1000 v, filters 20000 v Hz, RMS 100 v ms). The
    closed level is its Dry."""
    d = _data('ReaGate')
    lin = lambda db: 0.0 if db is None else min(2.0, db2lin(db))
    raw = {0: lin(threshold_db), 1: attack_ms / 500.0, 2: release_ms / 5000.0,
           3: preopen_ms / 250.0, 4: hold_ms / 1000.0, 5: min(1.0, lowpass / 20000.0),
           6: hipass / 20000.0, 7: 0.0, 8: 0.0, 9: lin(closed_db), 10: 1.0, 11: 0.0,
           12: 1.0, 13: 0.0, 14: rms_ms / 100.0, 15: 0.0, 16: 0.0, 17: 0.0, 18: 0.0,
           19: 0.0, 20: 0.0}
    for k, v in raw.items():
        struct.pack_into('<f', d, 8 + 4 * k, float(v))
    return d


def reapitch(voices, dry=1.0):
    """A ReaPitch data block: voices of (semitones -24..24, linear volume)."""
    t = _data('ReaPitch')
    one = t[32:32 + 44]
    out = bytearray(t[:8]) + struct.pack('<II', len(voices), 44) + t[16:32]
    struct.pack_into('<ff', out, 24, 1.0, float(dry))
    for semis, vol in voices:
        b = bytearray(one)
        vals = {1: 1.0, 2: (semis + 24.0) / 48.0, 3: 0.5, 4: 0.5, 5: 0.5,
                6: 0.5, 7: 0.5, 8: 0.5, 9: float(vol), 10: 0.5}
        for k, v in vals.items():
            struct.pack_into('<f', b, 4 * k, float(v))
        out += b
    return out


# ReaVerbate measured (REAPER renders of an impulse, dampening 0.3, wet
# 0 dB): room size 0..1 in tenths -> RT60 (s) and the tail's energy (dB);
# it adds 25 ms of its own before the first reflection
_VB_RT = [0.21, 0.24, 0.27, 0.32, 0.38, 0.46, 0.58, 0.78, 1.15, 2.10, 9.72]
_VB_EN = [-2.1, -1.9, -1.7, -1.4, -1.0, -0.6, 0.0, 0.7, 1.6, 3.0, 6.4]


def verbate_for(rt60, level_db, predelay_ms, mix=1.0, dry_db=None, **kw):
    """ReaVerbate with the RT60 and tail level (dB, energy of an impulse's
    answer) of a Cubase reverb: the room size that gives that RT60, the wet
    gain that gives that level."""
    lr = math.log(max(0.21, min(9.72, rt60)))
    for i in range(10):
        a, b = math.log(_VB_RT[i]), math.log(_VB_RT[i + 1])
        if lr <= b or i == 9:
            f = max(0.0, min(1.0, (lr - a) / (b - a)))
            room = (i + f) / 10.0
            en = _VB_EN[i] + f * (_VB_EN[i + 1] - _VB_EN[i])
            break
    return reaverbate(wet_db=level_db - en, dry_db=dry_db, room=room, damping=0.3,
                      delay_ms=max(0.0, predelay_ms - 25.0), **kw)


# Cubase's Compressor Auto Make-Up, measured (Cubase 15 export of a steady
# -60 dBFS tone through each threshold / ratio, 2026-09-30): dB by
# threshold (rows) and ratio (columns); the soft knee does not change it
# and Cubase's ratio stops at 8
_MK_T = [-60.0, -50.0, -40.0, -30.0, -20.0, -10.0, 0.0]
_MK_R = [1.0, 1.5, 2.0, 4.0, 8.0]
_MK = [[0.0, 9.6, 14.3, 21.5, 25.1],
       [0.0, 6.94, 10.42, 15.63, 18.23],
       [0.0, 4.44, 6.67, 10.0, 11.67],
       [0.0, 2.5, 3.75, 5.62, 6.56],
       [0.0, 1.11, 1.67, 2.5, 2.92],
       [0.0, 0.28, 0.42, 0.63, 0.73],
       [0.0, 0.0, 0.0, 0.0, 0.0]]
# (the probe's states carried a 6.5 % Dry Mix, added after the make-up:
# taken out; the user's 12 non-Limit states then come within 0.16 dB, and
# with Limit on Cubase makes up as for ratio 8)


def cubase_auto_makeup(threshold_db, ratio):
    """Cubase's Auto Make-Up gain (dB) for a threshold and ratio."""
    def at(tab, x, v):
        x = max(tab[0], min(tab[-1], x))
        for i in range(len(tab) - 1):
            if x <= tab[i + 1]:
                return i, (x - tab[i]) / (tab[i + 1] - tab[i])
        return len(tab) - 2, 1.0
    i, fi = at(_MK_T, threshold_db, None)
    j, fj = at(_MK_R, min(8.0, max(1.0, ratio)), None)
    a = _MK[i][j] + fj * (_MK[i][j + 1] - _MK[i][j])
    b = _MK[i + 1][j] + fj * (_MK[i + 1][j + 1] - _MK[i + 1][j])
    return a + fi * (b - a)


# ---- Cubase's own effects -> REAPER's own -------------------------------

import math

SQ2 = math.sqrt(2.0)
REVERENCE_UID = 'ED824AB48E0846D5959682F5626D0972'
ROOMWORKS_UID = '56535452655641726F6F6D776F726B73'
ROOMWORKS_SE_UID = '56535452655642726F6F6D776F726B73'
REVELATION_UID = '143AE812D7E249D8B503B4A6E3EFC9F8'
PINGPONG_UID = '37A3AA84E3A24D069C39030EC68768E1'
GATE_UID = '3B660266B3CA4B57BBD487AE1E6C0D2A'
OCTAVER_UID = '4114D8E30C024C1DB0DE375FC53CDBED'
# StereoDelay's tempo-sync notes, in quarter notes, by the index Cubase
# stores: 1/1 .. 1/32, the same as triplets, then dotted
SYNC_NOTES = [4, 2, 1, 0.5, 0.25, 0.125]
SYNC_NOTES = SYNC_NOTES + [v * 2 / 3 for v in SYNC_NOTES] + [v * 1.5 for v in SYNC_NOTES]


def js_db(g):
    """REAPER's utility JS effects take 2^(s/6) for their dB sliders."""
    return -150.0 if g <= 3e-8 else 6.0 * math.log2(g)


def channelmixer(ll, rr, lr, rl):
    """utility/channelmixer with linear gains L->L, R->R, L->R, R->L."""
    return ('js', 'utility/channelmixer', [max(-120.0, min(6.0, js_db(v))) for v in (ll, rr, lr, rl)])


def stereo_width(width_pct, mono=False):
    """Cubase's StereoEnhancer (an exact mid/side matrix, side x width/100)
    as REAPER's own effects: the channel mixer up to 100 % (L' = L (1+w)/2 +
    R (1-w)/2), Stillwell's Stereo Width above it
    (L' = g (C (1 + cb) + S (1 + wb sqrt2)))."""
    if mono:
        return channelmixer(0.5, 0.5, 0.5, 0.5)
    w = max(0.0, width_pct / 100.0)
    if w <= 1.0:
        return channelmixer((1 + w) / 2, (1 + w) / 2, (1 - w) / 2, (1 - w) / 2)
    wb = 0.1
    cb = (1 + wb * SQ2) / w - 1
    if cb < 0.1:
        cb = 0.1
        wb = (w * (1 + cb) - 1) / SQ2
    g = 1.0 / (1 + cb)
    db = lambda v: 20 * math.log10(v)
    return ('js', 'sstillwell/stereowidth', [db(wb), db(cb), db(g), 0.0, 0.0])


def from_cubase(fx, project=None):
    """(what REAPER gets, how close) for one of Cubase's own effects, or
    None. What REAPER gets is a list of ('js', path, sliders) or
    ('vst', name, data block)."""
    from . import builtins
    uid = (fx.uid or '').upper()
    r = builtins._records(fx.component)
    g = lambda k, d=0.0: r[k][1] if k in r else d
    tempo = 120.0
    rate = float(getattr(project, 'samplerate', 48000) or 48000) if project is not None else 48000.0
    if project is not None and getattr(project, 'tempo', None):
        tempo = project.tempo[0][1]
    from . import freq_eq
    if uid == freq_eq.UID:
        # Frequency: a ReaEQ band for each of its bands (freq_eq, measured)
        notes = []
        rb, worst = freq_eq.reaeq_of(r, notes)
        if not rb:
            return None
        from . import chan_eq
        out = [('vst', 'ReaEQ', chan_eq.reaeq_data(rb))]
        if abs(g('equalizerAoutput')) > 1e-6:
            out.append(('js', 'utility/volume', [js_db(db2lin(g('equalizerAoutput'))), 150.0]))
        return out, 'close (ReaEQ, within %.1f dB of Frequency)' % worst
    if uid == builtins.STEREO_ENHANCER_UID:
        exact = g('delayon') < 0.5 and g('colouron') < 0.5
        return [stereo_width(g('width', 100.0), g('monoout') > 0.5)], \
            'exact' if exact else 'close (its Delay/Color have no REAPER counterpart)'
    if uid == builtins.VOLUME_UID:
        gain, g0, g1, _ = builtins.volume_params(fx.component)
        if abs(g0 - g1) < 1e-9:
            return [('js', 'utility/volume', [js_db(gain * g0), 150.0])], 'exact'
        return [channelmixer(gain * g0, gain * g1, 0.0, 0.0)], 'exact'
    if uid == builtins.MONO_TO_STEREO_UID:
        w, d, c, m = builtins.mono_to_stereo_params(fx.component)
        return [channelmixer(0.5, 0.5, 0.5, 0.5)], \
            'exact' if (m > 0.5 or w <= 0.0) else \
            'close (the mono fold; its widening has no REAPER counterpart)'
    name = (builtins.TABLE.get(uid) or (None,))[0]
    if name == 'Compressor':
        # Cubase's ratio stops at 8; its Auto Make-Up (measured table) is
        # written as ReaComp's fixed output gain - ReaComp's own automatic
        # make-up is another formula
        ratio = min(8.0, g('ratio', 2.0))
        mk = g('makeUp') + (cubase_auto_makeup(g('threshold', -20.0), 8.0 if g('limit') > 0.5 else ratio)
                            if g('automakeup') > 0.5 else 0.0)
        if g('drymix') > 0:
            # Dry Mix is a crossfade in Cubase (measured): the compressed
            # part down by 1 - drymix, ReaComp's dry adds the rest
            mk += 20 * math.log10(max(1e-6, 1 - g('drymix') / 100.0))
        # tuned on the user's ten Compressor settings against a Cubase
        # export (2026-09-30): ReaComp's release runs about twice as long
        # for the same number, Soft Knee is about 6 dB wide, RMS 100 % an
        # 8 ms window
        data = reacomp(threshold_db=g('threshold', -20.0), ratio=ratio,
                       attack_ms=g('attack', 1.0), release_ms=0.45 * g('release', 100.0),
                       knee_db=6.0 if g('softknee') > 0.5 else 0.0,
                       rms_ms=8.0 * g('rms') / 100.0,
                       makeup_db=min(6.0, mk),
                       auto_makeup=False, auto_release=g('autorelease') > 0.5,
                       precomp_ms=0.0 if g('live') > 0.5 else 1.83,
                       dry_db=(20 * math.log10(g('drymix') / 100.0)) if g('drymix') > 0 else None,
                       limit=g('limit') > 0.5)
        post = []
        if mk > 6.0:
            # past ReaComp's +6 dB of output gain: the rest in REAPER's volume
            post = [('js', 'utility/volume', [js_db(db2lin(mk - 6.0)), 150.0])]
        return [('vst', 'ReaComp', data)] + post, 'close (ReaComp, same settings)'
    if name == 'StereoDelay':
        # Cubase (measured): unit 1 hears the left input only, unit 2 the
        # right; each mixes its own channel as dry min(1, 2 (1 - mix)) +
        # wet min(1, 2 mix) and pans that sum linearly (centre -6 dB each
        # side). ReaDelay: a tap panned hard to a side with width 1 plays
        # that side's input there, width -1 (swapped) plays it on the other
        # side, so every path is a tap: the echoes and, at 0 ms, the dry.
        # ReaDelay's global dry stays off. Cubase's filters stop at 800 Hz
        # (Low Cut) and 1200 Hz (High Cut).
        taps = []
        fbk = g('feedback1') > 0 or g('feedback2') > 0
        for u, side in (('1', -1.0), ('2', 1.0)):
            ms = g('delay' + u, 100.0)
            if g('temposync' + u) > 0.5:
                ms = SYNC_NOTES[max(0, min(17, int(round(g('syncnote' + u)))))] * 60000.0 / max(tempo, 1.0)
            mix = g('mix' + u, 100.0) / 100.0
            wet, dry = min(1.0, 2 * mix), min(1.0, 2 * (1 - mix))
            pan = g('pan' + u, side * 100) / 100.0
            a_own, a_other = ((1 - pan) / 2, (1 + pan) / 2) if side < 0 else ((1 + pan) / 2, (1 - pan) / 2)
            # Cubase's repeats come D + 1 samples apart (D the delay in
            # whole samples, rounded down): ReaDelay's taps D + 1 long, and
            # the dry 1 sample, play Cubase's output one sample late
            fb = g('feedback' + u, 50.0) / 100.0
            if fbk:
                D = int(ms * rate / 1000.0)
                ms = (D + 1) * 1000.0 / rate
            echo = dict(ms=ms, feedback=fb,
                        hipass=min(800.0, g('filterL' + u, 50.0)) if g('filterLOn' + u) > 0.5 else 0.0,
                        lowpass=max(1200.0, g('filterH' + u, 15000.0)) if g('filterHOn' + u) > 0.5 else 20000.0)
            direct = dict(ms=(1000.0 / rate) if fbk else 0.0, feedback=0.0)
            for path, level in ((echo, wet), (direct, dry)):
                if level * a_own > 1e-9:
                    taps.append(dict(path, pan=side, width=1.0, volume=level * a_own))
                if level * a_other > 1e-9:
                    taps.append(dict(path, pan=-side, width=-1.0, volume=level * a_other))
        data = readelay(taps, wet_db=0.0, dry_db=None)
        return [('vst', 'ReaDelay', data)], 'close (ReaDelay)'
    if name == 'Limiter':
        # Cubase's Limiter: Input gain, a limit at 0 dB, Output gain, a
        # release up to 1000 ms. ReaLimit's release stops near 40 ms, so
        # ReaComp as a limiter: its threshold -input (where the driven
        # signal reaches 0 dB), ratio infinite, peak detection, input +
        # output as its output gain
        # ReaComp's own output gain stops at +6 dB, so the Input drive is
        # REAPER's volume effect in front and ReaComp limits at 0 dB
        # (-1 dB: Cubase limits a little harder - the closest level over
        # the user's eight settings, measured on a Cubase export)
        data = reacomp(threshold_db=-1.0, ratio=100.0, limit=True,
                       attack_ms=0.0, release_ms=g('release', 500.0), rms_ms=0.0,
                       makeup_db=g('output'), auto_release=g('autorelease') > 0.5,
                       precomp_ms=1.0)
        pre = [('js', 'utility/volume', [js_db(db2lin(g('input'))), 150.0])] if abs(g('input')) > 1e-6 else []
        return pre + [('vst', 'ReaComp', data)], 'close (ReaComp as a limiter)'
    if name == 'Brickwall Limiter':
        t = g('threshold', -0.3)
        data = reacomp(threshold_db=t, ratio=100.0, limit=True, attack_ms=0.0,
                       release_ms=g('release', 3.0), rms_ms=0.0, makeup_db=0.0,
                       auto_release=g('autorelease') > 0.5, precomp_ms=1.5)
        return [('vst', 'ReaComp', data)], 'close (ReaComp as a brickwall limiter)'
    if uid == REVERENCE_UID:
        # a convolution reverb whose impulse (Steinberg's library) exists
        # only in Cubase: REAPER's own algorithmic reverb with its decay,
        # pre-delay, filters and level. Measured on the user's REVerence
        # (Cubase export of an impulse): RT60 2.54 s at Time 100 %, the tail
        # 8.3 dB under what ReaVerbate plays at the same wet gain (level set on
        # its render of the probe: overall energy equal)
        mix = g('mix', 100.0) / 100.0
        out = g('output', 0.0)
        hp = g('lowfilterfreq', 100.0) if (g('eqon') > 0.5 and g('lowfilteron') > 0.5 and g('lowfiltergain') < 0) else 0.0
        lp = g('highfilterfreq', 15000.0) if (g('eqon') > 0.5 and g('highfilteron') > 0.5 and g('highfiltergain') < 0) else 20000.0
        wet = min(1.0, 2 * mix)
        dry = min(1.0, 2 * (1 - mix))
        return [('vst', 'ReaVerbate', verbate_for(
            2.54 * g('time', 100.0) / 100.0, 20 * math.log10(max(wet, 1e-6)) + out - 8.3, g('predelay', 0.0),
            dry_db=(20 * math.log10(dry) + out) if dry > 0 else None, lowpass=lp, hipass=hp))],             'approximate (ReaVerbate: its impulse response is Steinberg data)'
    if uid == REVELATION_UID:
        mix = g('mix', 20.0) / 100.0
        out = g('outgain', 0.0)
        rt = 2.0 * g('reverbmaintime', 65.0) / 65.0 * (0.5 + g('roomsize', 50.0) / 100.0)
        return [('vst', 'ReaVerbate', verbate_for(
            rt, 20 * math.log10(max(mix, 1e-6)) + out - 9.0, g('predelay', 0.0),
            dry_db=(20 * math.log10(1 - mix) + out) if mix < 1 else None,
            lowpass=min(20000.0, g('highcut', 20000.0)),
            width=max(-1.0, min(1.0, g('width', 100.0) / 100.0))))], 'approximate (ReaVerbate)'
    if uid == PINGPONG_UID:
        # repeats alternating left and right, each quieter by the feedback
        ms = g('delay', 250.0)
        if g('temposync') > 0.5:
            ms = SYNC_NOTES[max(0, min(17, int(round(g('syncnote')))))] * 60000.0 / max(tempo, 1.0)
        fb = g('feedback', 50.0) / 100.0
        mix = g('mix', 50.0) / 100.0
        spread = g('pan', 100.0) / 100.0
        wet, dry = min(1.0, 2 * mix), min(1.0, 2 * (1 - mix))
        taps = [dict(ms=0.0, feedback=0.0, pan=0.0, width=1.0, volume=dry)] if dry > 0 else []
        k, level = 1, wet
        while k <= 8 and level > 0.001:
            taps.append(dict(ms=k * ms, feedback=0.0, pan=spread if k % 2 == 0 else -spread, width=0.0,
                             volume=level,
                             hipass=g('filterL', 20.0) if g('filterLOn') > 0.5 else 0.0,
                             lowpass=g('filterH', 20000.0) if g('filterHOn') > 0.5 else 20000.0))
            k += 1
            level *= fb
        return [('vst', 'ReaDelay', readelay(taps, wet_db=0.0, dry_db=None))], 'close (ReaDelay)'
    if uid == GATE_UID:
        # the side chain's band filter as ReaGate's detector filters
        lp, hp = 20000.0, 0.0
        if g('scon') > 0.5:
            fc, q = g('centerfreq', 1000.0), max(0.1, g('qfactor', 1.0))
            ty = int(round(g('filtertype', 0.0)))
            span = 2.0 ** (1.0 / (2.0 * q))
            if ty == 0:
                lp = fc
            elif ty == 2:
                hp = fc
            else:
                lp, hp = min(20000.0, fc * span), fc / span
        rng = g('range', -100.0)
        return [('vst', 'ReaGate', reagate(
            threshold_db=g('threshold', -20.0), attack_ms=g('attack', 1.0),
            release_ms=g('release', 150.0) / GATE_RELEASE,
            hold_ms=g('hold', 0.0), preopen_ms=0.0 if g('live') > 0.5 else 1.83,
            rms_ms=10.0 * g('rms') / 100.0, lowpass=lp, hipass=hp,
            closed_db=None if rng <= -100 else rng))], 'close (ReaGate)'
    if uid == OCTAVER_UID:
        voices = [(-12.0, g('oct1') / 100.0)] if g('oct1') > 0 else []
        if g('oct2') > 0:
            voices.append((-24.0, g('oct2') / 100.0))
        return [('vst', 'ReaPitch', reapitch(voices, dry=g('dir', 100.0) / 100.0))],             'approximate (ReaPitch: a pitch shifter, not an octave divider)'
    if uid in (ROOMWORKS_UID, ROOMWORKS_SE_UID):
        # measured on the user's RoomWorks (Cubase export of an impulse):
        # Time 0.58 -> RT60 1.79 s (0.2 s x 50^time), Pre-Delay 0.36 -> 65 ms
        # (180 ms full scale), the tail 9.3 dB under ReaVerbate's
        mix = g('mix', 0.5)
        return [('vst', 'ReaVerbate', verbate_for(
            0.2 * 50.0 ** g('time', 0.5), 20 * math.log10(max(mix, 1e-6)) - 9.3, 180.0 * g('preDelay', 0.0),
            dry_db=20 * math.log10(1 - mix) if mix < 1 else None,
            width=max(-1.0, min(1.0, g('width', 1.0)))))], 'approximate (ReaVerbate)'
    if fx.name == 'EnvelopeShaper':
        clamp = lambda v, a, b: max(a, min(b, v))
        return [('js', 'loser/TransientController',
                 [clamp(5.0 * g('attackgain'), -100, 100), clamp(5.0 * g('releasegain'), -100, 100),
                  clamp(g('output'), -12.0, 6.0)])], 'approximate (Transient Controller)'
    return None


def lines(entries, indent='      ', bypass=0, offline=0, fxid=None):
    """The .rpp lines of from_cubase()'s list."""
    out = []
    for k, (kind, name, val) in enumerate(entries):
        fid = fxid if k == 0 else (fxid[:-2] + '%02X}' % k if fxid else None)
        if kind == 'js':
            out += js_lines(name, val, indent, bypass, offline, fid)
        else:
            out += vst_lines(name, val, indent, bypass, offline, fid)
    return out


# ---- REAPER's own plug-ins -> Cubase's own -----------------------------

def _f32(data, off):
    return struct.unpack_from('<f', data, off)[0] if data and len(data) >= off + 4 else None


def _db(lin):
    return -150.0 if lin <= 1e-8 else 20 * math.log10(lin)


# ReaGate closes on a straight gain ramp to nothing over its release,
# Cubase's Gate at a steady fall of about 9 dB per release time (measured on
# a Cubase export, 2026-10-01): the shapes differ, the tails carry the same
# energy at Cubase's release = 0.69 x ReaGate's
GATE_RELEASE = 0.69


def readelay_tap_ms(t, tempo=120.0):
    """A ReaDelay tap's delay in ms: its length in time (field 2, ms / 10000)
    plus its musical length (field 3, beats / 128), the beats at `tempo`.
    A tap set in beats has 0 in field 2 - read as 'no delay' it was taken
    for a dry path and the echo went missing (Black Seven's FX Delay, one
    beat at 0.0078125)."""
    return 10000.0 * (t[2] or 0.0) + 128.0 * (t[3] or 0.0) * 60000.0 / float(tempo or 120.0)


def to_cubase(name, data, tempo=120.0):
    """(uid, Cubase component state, Cubase effect name, how close) for a
    Cockos plug-in's data block, or None."""
    from . import builtins
    uid_of = dict((v[0], k) for k, v in builtins.TABLE.items())
    if name == 'ReaComp' and data and len(data) >= 8 + 4 * 17:
        v = [_f32(data, 8 + 4 * p) for p in range(17)]
        ratio_raw = v[1]
        # fitted on the probe's three ReaComp settings against a Cubase
        # export (2026-10-01): Cubase's release matches ReaComp's at the
        # same number this way round, and its threshold 1 dB higher
        rec = {'threshold': _db(v[0]) + 1.0, 'ratio': 1.0 + 99.0 * ratio_raw if ratio_raw < 1 else 100.0,
               'limit': 1.0 if ratio_raw >= 1 else 0.0,
               'attack': 500.0 * v[2], 'release': 5000.0 * v[3],
               'live': 1.0 if v[4] <= 0 else 0.0,
               'makeUp': _db(v[11]), 'automakeup': 1.0 if v[15] >= 0.5 else 0.0,
               'autorelease': 1.0 if v[16] >= 0.5 else 0.0,
               'rms': max(0.0, min(100.0, 100.0 * (100.0 * v[13]) / 8.0)),
               'softknee': 1.0 if 24.0 * v[14] >= 3.0 else 0.0,
               'drymix': max(0.0, min(100.0, 100.0 * v[10])), 'bypass': 0.0}
        st = builtins.table_state('Compressor', rec)
        return (uid_of['Compressor'], st, 'Compressor', 'close (Cubase Compressor, same settings)') if st else None
    if name == 'ReaDelay' and data and len(data) >= 32:
        n, size = struct.unpack_from('<II', data, 8)
        wet, dry = _f32(data, 24), _f32(data, 28)
        taps, direct = [], 0.0
        for k in range(n):
            o = 32 + k * size
            if o + 44 > len(data):
                break
            t = [_f32(data, o + 4 * j) for j in range(11)]
            if t[1] < 0.5:
                continue
            ms = readelay_tap_ms(t, tempo)
            if ms <= 0.1:
                # a tap of (next to) no delay is a dry path
                direct = max(direct, t[9] * wet)
            elif t[8] >= 0.5:
                # an echo; a swapped tap (width -1) is the other side's
                # share of one already counted
                t = list(t)
                t[2] = ms / 10000.0         # the delay, beats included
                taps.append(t)
        if not taps:
            return None
        dry = max(dry, direct)
        units = (taps + taps)[:2] if len(taps) == 1 else taps[:2]
        rec = {'bypass': 0.0}
        for u, t in zip(('1', '2'), units):
            vol = t[9] * wet
            rec.update({'delay' + u: 10000.0 * t[2], 'temposync' + u: 0.0,
                        'feedback' + u: 100.0 * max(0.0, min(1.0, t[4])),
                        'filterH' + u: 20000.0 * t[5], 'filterHOn' + u: 1.0 if t[5] < 0.999 else 0.0,
                        'filterL' + u: 20000.0 * t[6], 'filterLOn' + u: 1.0 if t[6] > 0.0005 else 0.0,
                        # Cubase: wet min(1, 2 mix), dry min(1, 2 (1 - mix))
                        'mix' + u: 100.0 * ((1.0 - min(1.0, dry) / 2.0) if vol >= dry else min(1.0, vol) / 2.0),
                        'pan' + u: (-100.0 if u == '1' else 100.0) if len(taps) == 1 else 100.0 * (2 * t[10] - 1)})
        st = builtins.table_state('StereoDelay', rec)
        return (uid_of['StereoDelay'], st, 'StereoDelay', 'close (Cubase StereoDelay)') if st else None
    if name == 'ReaGate' and data and len(data) >= 8 + 4 * 15:
        v = [_f32(data, 8 + 4 * p) for p in range(15)]
        rec = {'threshold': _db(v[0]), 'attack': 500.0 * v[1], 'release': GATE_RELEASE * 5000.0 * v[2],
               'hold': 1000.0 * v[4], 'range': max(-100.0, _db(v[9])),
               'rms': max(0.0, min(100.0, 100.0 * 100.0 * v[14] / 10.0)),
               'live': 1.0 if v[3] <= 0 else 0.0,
               'scon': 1.0 if (v[5] < 0.999 or v[6] > 0.0005) else 0.0, 'bypass': 0.0}
        if rec['scon']:
            lp, hp = 20000.0 * v[5], 20000.0 * v[6]
            if hp > 0.5 and lp < 19999:
                rec.update(filtertype=1.0, centerfreq=math.sqrt(lp * hp),
                           qfactor=max(0.1, 1.0 / (2.0 * math.log2(max(lp / hp, 1.0001)))))
            elif lp < 19999:
                rec.update(filtertype=0.0, centerfreq=lp)
            else:
                rec.update(filtertype=2.0, centerfreq=hp)
        st = builtins.table_state('Gate', rec)
        return (GATE_UID, st, 'Gate', 'close (Cubase Gate)') if st else None
    if name == 'ReaVerbate' and data and len(data) >= 40:
        v = [_f32(data, 8 + 4 * k) for k in range(8)]
        wet, dry, room = v[0], v[1], v[2]
        k = max(0, min(9, int(room * 10)))
        f = room * 10 - k
        rt = math.exp(math.log(_VB_RT[k]) + f * (math.log(_VB_RT[k + 1]) - math.log(_VB_RT[k])))
        en = _VB_EN[k] + f * (_VB_EN[k + 1] - _VB_EN[k])
        # RoomWorks: tail 9.3 dB under ReaVerbate's at the same gain; its
        # Mix crossfades
        tail = 10 ** ((_db(wet) + en + 9.3) / 20.0)
        mix = max(0.0, min(1.0, tail / max(tail + dry, 1e-9)))
        rec = {'mix': mix, 'time': max(0.0, min(1.0, math.log(max(rt, 0.2) / 0.2) / math.log(50.0))),
               'preDelay': max(0.0, min(1.0, (500.0 * v[5] + 25.0) / 180.0)),
               'width': max(0.0, min(1.0, 2 * v[4] - 1)), 'bypass': 0.0}
        st = builtins.table_state('RoomWorks', rec)
        return (ROOMWORKS_UID, st, 'RoomWorks', 'approximate (Cubase RoomWorks)') if st else None
    if name == 'ReaPitch' and data and len(data) >= 32:
        n, size = struct.unpack_from('<II', data, 8)
        dry = _f32(data, 28)
        rec = {'dir': 100.0 * min(1.0, dry), 'oct1': 0.0, 'oct2': 0.0, 'bypass': 0.0}
        for k in range(n):
            o = 32 + k * size
            if o + 44 > len(data):
                break
            t = [_f32(data, o + 4 * j) for j in range(11)]
            if t[1] < 0.5:
                continue
            semis = -24.0 + 48.0 * t[2]
            if abs(semis + 12) < 0.5:
                rec['oct1'] = 100.0 * min(1.0, t[9])
            elif abs(semis + 24) < 0.5:
                rec['oct2'] = 100.0 * min(1.0, t[9])
            else:
                return None     # a shift Octaver does not make
        st = builtins.table_state('Octaver', rec)
        return (OCTAVER_UID, st, 'Octaver', 'approximate (Cubase Octaver)') if st else None
    if name == 'ReaLimit' and data and len(data) >= 24:
        thr, ceil = struct.unpack_from('<dd', data, 8)
        if abs(thr - ceil) < 1e-6:
            st = builtins.table_state('Brickwall Limiter', {'threshold': ceil, 'bypass': 0.0})
            return (uid_of['Brickwall Limiter'], st, 'Brickwall Limiter', 'close (Cubase Brickwall Limiter)') if st else None
        # release 3 ms: the closest Cubase Limiter to ReaLimit over a grid of
        # 1..1000 ms, with or without Auto, against four ReaLimit release
        # values (Cubase export, 2026-10-01); its default 500 ms played up
        # to 1.1 dB quieter
        st = builtins.table_state('Limiter', {'input': -thr, 'output': ceil, 'release': 3.0,
                                              'autorelease': 0.0, 'bypass': 0.0})
        return (uid_of['Limiter'], st, 'Limiter', 'close (Cubase Limiter)') if st else None
    return None
