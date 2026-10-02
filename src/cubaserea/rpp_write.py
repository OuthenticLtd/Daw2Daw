"""Write the intermediate model out as a REAPER project (.rpp)."""
import hashlib
import os
import re

from .model import flatten_lanes, Send, output_routes

from . import plugins as P
from . import progress


# Cubase's Project Setup > Stereo Pan Law as the gain of a mono signal
# placed at the centre (panlaw.CUBASE_LAW_GAIN; Equal Power -3.01 dB).
# Where it acts depends on the channel (measured on Gradila's export,
# 2026-09-29, by re-panning channels of a copy and exporting again):
#   - a STEREO channel playing a MONO file puts the file on both sides at
#     that gain, before the inserts, and then balance-pans (L = 1,
#     R = 1 - |c|); so it goes on the REAPER item, where REAPER plays a
#     mono file on both sides at unity;
#   - a MONO channel (bus arrangement mono, PannerType 4) plays the file
#     at unity through its inserts and pans it with a sine/cosine law
#     after them (panlaw.cubase_mono_gains) - full level on the near side
#     hard-panned, the law's gain on both at the centre; that is REAPER's
#     own -3 dB-law panner, so the pan maps by L/R ratio like any other
#     and the level difference to the written PANLAW goes on the fader.
from .panlaw import CUBASE_LAW_GAIN as PAN_LAW_GAIN


def law_gain(project):
    return PAN_LAW_GAIN.get(getattr(project, 'panlaw_code', None), 1.0)


def mono_item_gain(it, t, project):
    """What Cubase multiplies this item by for being a mono file on a
    stereo channel (1.0 otherwise). The channel count comes from the
    project's own clip record; failing that from the WAV header."""
    if getattr(project, 'pan_law_of', None) != 'cubase':
        return 1.0
    if getattr(t, 'mono', False) or it.kind != 'audio':
        return 1.0
    ch = getattr(it, 'channels', None)
    if ch is None and it.file and os.path.exists(it.file):
        try:
            from . import media
            ch = media.wav_channels(it.file)
        except Exception:
            ch = None
    return law_gain(project) if ch == 1 else 1.0


def _pan_args(t, project):
    """Keyword arguments for panlaw's mapping: which Cubase panner."""
    return dict(mono=bool(getattr(t, 'mono', False)),
                law_code=getattr(project, 'panlaw_code', None) or 6)


# the pan law this writer puts in the project header (PANLAW 1 = 0 dB, the
# REAPER default and what every project of the user's carries)
WRITER_PANLAW = 1.0
WRITER_PANMODE = 3


def reaper_mix(t, project):
    """(fader gain, pan) for the REAPER track that plays what the Cubase
    channel played: Cubase's balance panner is linear, REAPER's is an angle
    law with a boost (panlaw.py), so the pan with the same L/R ratio is
    written and the level difference goes on the fader."""
    if getattr(project, 'pan_law_of', None) != 'cubase':
        return t.vol, t.pan
    from . import panlaw
    p, gain = panlaw.cubase_to_reaper(t.pan, WRITER_PANLAW, WRITER_PANMODE,
                                      **_pan_args(t, project))
    return t.vol * gain, p


def reaper_pan_curve(t, project, pts):
    if getattr(project, 'pan_law_of', None) != 'cubase':
        return pts
    from . import envelope, panlaw
    kw = _pan_args(t, project)

    def fwd(c):
        return panlaw.cubase_to_reaper(c, WRITER_PANLAW, WRITER_PANMODE, **kw)[0]

    def back(p):
        return panlaw.reaper_to_cubase(p, WRITER_PANLAW, WRITER_PANMODE, **kw)[0]
    # Cubase's panner moves straight in its own position and REAPER's in
    # its own; extra points keep the ramp the same (envelope.py)
    return [(s, fwd(v)) for s, v in envelope.pan_curve(pts, fwd, back)]



def _interp_pts(pts, x):
    """Straight-line interpolation over (x, y) points, as Cubase plays a
    fade drawn with points."""
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 if x1 <= x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


_FIT_CURVES = [k / 20.0 for k in range(-20, 21)]


def fade_line(item, key, stats=None, log=None, track=''):
    """The FADEIN/FADEOUT line for a Cubase fade.

    A fade Cubase drew with more than its two end points has a shape, and
    REAPER holds a fade as a shape number and a curve (fades.py). The pair
    that follows the points most closely is chosen; a straight fade is
    shape 0 as before. Returns the line's tail after the keyword."""
    import math
    from . import fades
    secs = item.fadein if key == 'in' else item.fadeout
    pts = (getattr(item, 'fade_points', None) or {}).get(key)
    if not pts or len(pts) <= 2 or secs <= 0:
        return '0 %s 0 1 0 0 0' % fmt(secs)
    pts = sorted(pts)
    if key == 'out':
        # judge the fade-out as a fade-in run backwards
        pts = sorted((1.0 - x, y) for x, y in pts)
    xs = [k / 64.0 for k in range(1, 64)]
    want = [_interp_pts(pts, x) for x in xs]
    if all(abs(w - x) < 1e-3 for w, x in zip(want, xs)):
        return '0 %s 0 1 0 0 0' % fmt(secs)
    best = None
    for shape in (0, 1, 2, 5, 6):
        for c in _FIT_CURVES:
            err = 0.0
            for x, w in zip(xs, want):
                g = fades.fadein_gain(shape, c, x)
                if w > 0.01 and g > 0.01:
                    err = max(err, abs(20.0 * math.log10(g / w)))
                else:
                    err = max(err, abs(g - w) * 20.0)
            if best is None or err < best[0]:
                best = (err, shape, c)
    err, shape, c = best
    if stats is not None:
        stats['fades fitted'] = stats.get('fades fitted', 0) + 1
        if err > 0.1:
            stats['fades approximate'] = stats.get('fades approximate', 0) + 1
            if log is not None:
                log.append('%r: a %d-point fade curve has no REAPER shape closer '
                           'than %.1f dB; the nearest (shape %d, curve %+.2f) is '
                           'written' % (track, len(pts), err, shape, c))
    return '%d %s 0 1 0 %s 0' % (shape, fmt(secs), fmt(c))


def guid_from(*parts):
    h = hashlib.md5(('|'.join(str(x) for x in parts)).encode('utf-8')).hexdigest().upper()
    return '{%s-%s-%s-%s-%s}' % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def q(s):
    s = (s or '').replace('\r', ' ').replace('\n', ' ')
    if '"' not in s:
        return '"%s"' % s
    if "'" not in s:
        return "'%s'" % s
    if '`' not in s:
        return '`%s`' % s
    return '"%s"' % s.replace('"', "'")


def fmt(v):
    v = float(v)
    return str(int(v)) if v == int(v) else repr(round(v, 10))


SOURCE_BY_EXT = {
    '.wav': 'WAVE', '.aif': 'WAVE', '.aiff': 'WAVE', '.w64': 'WAVE',
    '.bwf': 'WAVE', '.rf64': 'WAVE',
    '.ogg': 'VORBIS', '.oga': 'VORBIS', '.opus': 'VORBIS',
    '.mp3': 'MP3', '.flac': 'FLAC',
    '.mp4': 'VIDEO', '.mov': 'VIDEO', '.avi': 'VIDEO', '.mkv': 'VIDEO',
    '.m4v': 'VIDEO', '.webm': 'VIDEO', '.wmv': 'VIDEO', '.mpg': 'VIDEO',
    '.mpeg': 'VIDEO', '.m2v': 'VIDEO',
}


def source_type(path, kind='audio'):
    ext = os.path.splitext(path or '')[1].lower()
    return SOURCE_BY_EXT.get(ext, 'VIDEO' if kind == 'video' else 'WAVE')


def peakcol(rgb):
    r, g, b = rgb
    return 0x1000000 | (b << 16) | (g << 8) | r


def midi_events(notes, ppq_in, ppq_out=960, ccs=()):
    scale = float(ppq_out) / float(ppq_in or 480.0)
    evs = []
    for pos, status, d1, d2 in ccs:
        # a controller before the part's start is kept by Cubase and never
        # sent; moved to 0 it would be (Gradila's piano perc: CC 1 = 1 at
        # -2.3 and -0.1 beats)
        if pos < -1e-6:
            continue
        evs.append((max(0, int(round(pos * scale))), status, d1 & 0x7f, d2 & 0x7f))
    for pos, nlen, ch, pitch, vel, *rest in notes:
        # a velocity-0 note is silent in Cubase; REAPER has no velocity 0 (a
        # note-on with 0 is a note-off), so it becomes a muted note ('Em')
        muted = int(vel) <= 0
        vel = max(1, min(127, int(vel)))
        offv = max(0, min(127, int(rest[0]))) if rest else 64
        a = max(0, int(round(pos * scale)))
        b = int(round((pos + max(nlen, 1.0)) * scale))
        if b <= a:
            b = a + 1
        evs.append((a, 0x90 | (ch & 0x0f), pitch & 0x7f, vel, muted))
        evs.append((b, 0x80 | (ch & 0x0f), pitch & 0x7f, offv, muted))
    evs = [e if len(e) == 5 else e + (False,) for e in evs]
    evs.sort(key=lambda e: (e[0], (e[1] & 0xf0) == 0x90))
    out, last = [], 0
    for t, st, d1, d2, muted in evs:
        out.append('%s %d %02x %02x %02x' % ('Em' if muted else 'E',
                                            max(0, t - last), st, d1, d2))
        last = t
    return out, last


HEADER = ('RIPPLE 0', 'GROUPOVERRIDE 0 0 0', 'AUTOXFADE 1', 'ENVATTACH 3',
          'MIXERUIFLAGS 11 48', 'PEAKGAIN 1', 'FEEDBACK 0', 'PANLAW 1',
          'PROJOFFS 0 0 0', 'MAXPROJLEN 0 0', 'GRID 3199 8 1 8 1 0 0 0',
          'TIMEMODE 1 5 -1 30 0 0 -1 0', 'PANMODE 3', 'CURSOR 0',
          'ZOOM 12 0 0', 'VZOOMEX 6 0', 'USE_REC_CFG 0', 'RECMODE 1',
          'LOOP 0', 'LOOPGRAN 0 4', 'RECORD_PATH "Audio" ""')
RENDER = ('RENDER_FILE ""', 'RENDER_PATTERN "$region"', 'RENDER_FMT 0 2 0',
          'RENDER_1X 0', 'RENDER_RANGE 2 0 0 0 1000', 'RENDER_RESAMPLE 3 0 1',
          'RENDER_ADDTOPROJ 0', 'RENDER_STEMS 0', 'RENDER_DITHER 0',
          'TIMELOCKMODE 1', 'TEMPOENVLOCKMODE 1', 'ITEMMIX 1',
          'DEFPITCHMODE 589824 0', 'TAKELANE 1')
MASTER = ('PLAYRATE 1 0 0.25 4', 'SELECTION 0 0', 'SELECTION2 0 0',
          'MASTERAUTOMODE 0', 'MASTERTRACKHEIGHT 0 0', 'MASTERPEAKCOL 16576',
          'MASTERMUTESOLO 0', 'MASTER_NCH 2 2',
          'MASTER_PANMODE 3', 'MASTER_FX 1', 'MASTER_SEL 0')


def write(proj, path, media_root=None, plugin_index=None, log=None):
    log = log if log is not None else []
    idx = plugin_index if plugin_index is not None else P.PluginIndex()
    root = os.path.abspath(media_root) if media_root else None
    stats = {'fx': 0, 'fx_state': 0, 'fx_missing': {}, 'sends': 0,
             'master_fx': 0, 'master_env': 0,
             'parmenv': 0}
    # how close each of Cubase's own effects comes in REAPER's own plug-ins
    stock_notes = {}
    eq_worst = [0.0]
    # Melodyne's document from Cubase (ara.py): one per project, handed to
    # REAPER's Melodyne on the takes Cubase's events had it on
    ara_blob, ara_names, ara_takes = None, {}, []
    if getattr(proj, 'ara_docs', None) and any(
            getattr(it, 'ara_id', None) for t in proj.tracks for it in t.items):
        from . import ara as _ara
        try:
            wanted = {}
            for t_ in proj.tracks:
                for it_ in t_.items:
                    if getattr(it_, 'ara_mod', None):
                        if wanted.setdefault(it_.ara_id, it_.ara_mod) != it_.ara_mod:
                            log.append('Melodyne: two events play different '
                                       'versions of one file (%s); REAPER '
                                       'keeps one per file, %s'
                                       % (it_.ara_id, wanted[it_.ara_id]))
            ara_blob, ara_names = _ara.cubase_to_reaper(proj.ara_docs, log,
                                                        wanted)
        except Exception as e:
            log.append('Melodyne edits could not be carried over (%s): the '
                       'events play as recorded' % e)

    def media(p):
        if not p:
            return ''
        if root:
            try:
                rel = os.path.relpath(p, root)
                if not rel.startswith('..'):
                    return rel.replace('\\', '/')
            except Exception:
                pass
        if re.match(r'^[A-Za-z]:[\\/]', p):
            # a file left where the project said, on a Windows drive: in
            # Windows' own spelling, whichever system did the converting
            return p.replace('/', '\\')
        return p

    def fx_chain(t, i, indent='      '):
        """The body of an <FXCHAIN>/<MASTERFXLIST> for one track."""
        chain = []
        allfx = ([t.instrument] if t.instrument else []) + list(t.fx)
        from . import stock, builtins
        for k, fx in enumerate(allfx):
            if getattr(fx, 'cubase_only', False):
                continue
            if getattr(fx, 'rpp_lines', None):
                # REAPER's own plug-in a Live device became (als_read): its
                # block as made, at this chain's indent
                chain.extend(indent + x.strip() if not x.strip().startswith('>')
                             else indent + x.strip() for x in fx.rpp_lines)
                stats['fx'] += 1
                stats['native equivalents'] = stats.get('native equivalents', 0) + 1
                continue
            eq = None if getattr(fx, "native", False) else stock.from_cubase(fx, proj)
            if eq is not None:
                # one of Cubase's own effects: the closest of REAPER's own
                # plug-ins, with its settings (stock.py) - nothing to install
                entries, how = eq
                # the effect's own Bypass switch (a record in its state)
                # plays it dry, as the slot's bypass does
                own = builtins._records(fx.component).get('bypass')
                chain.extend(stock.lines(
                    entries, indent, 1 if (fx.bypass or (own and own[1] > 0.5)) else 0,
                    1 if (fx.offline or getattr(t, 'disabled', False)) else 0,
                    fxid=guid_from('fx', i, k, fx.name)))
                stats['fx'] += 1
                stats['native equivalents'] = stats.get('native equivalents', 0) + 1
                if how != 'exact':
                    stock_notes.setdefault('%s -> %s' % (fx.name, how), []).append(t.name)
                continue
            entry, usable = idx.lookup(fx)
            if entry is not None and entry.get('synthetic') \
                    and fx is t.instrument:
                # not in REAPER's scan, so only the slot says it is a synth
                entry = dict(entry, inst=True)
            if entry is not None and entry.get('synthetic'):
                log.append('%s on %r is not installed in REAPER on this '
                           'machine: written as itself, settings and all, so '
                           'it loads where it is installed'
                           % (fx.name, t.name))
            if entry is None and builtins.is_meter(fx):
                stats['meters left out'] = stats.get('meters left out', 0) + 1
                log.append('%s on %r is one of Cubase\'s meters - it passes '
                           'the sound through unchanged, so it was left out '
                           'and nothing heard is different' % (fx.name, t.name))
                continue
            if entry is None:
                stats['fx_missing'][fx.name] = fx.uid
                log.append('plug-in not installed in REAPER on this machine: '
                           '%s (%s) on %r - left out. If it is one of '
                           'Cubase\'s own effects it exists nowhere else: '
                           'render the track in place in Cubase before '
                           'converting to keep its sound'
                           % (fx.name, fx.uid, t.name))
                continue
            stats['fx'] += 1
            comp = fx.component if usable else b''
            ctrl = fx.controller if usable else b''
            if comp or (usable and not entry['vst3'] and (getattr(fx, 'raw_state', None)
                                                       or getattr(fx, 'param_dump', False))):
                stats['fx_state'] += 1
            else:
                log.append('%s on %r added without its saved settings'
                           % (fx.name, t.name))
            chain.append('%sBYPASS %d %d 0'
                         % (indent, 1 if fx.bypass else 0,
                            1 if (fx.offline or getattr(t, 'disabled', False)) else 0))
            ident = fx.uid if (fx.uid and not entry['uid']) else None
            raw = None
            if not entry['vst3'] and usable:
                # a VST2 build: its own chunk or REAPER's parameter dump, not
                # the VST3 pair (a Cubase VST2 is the wrapper's 'VstW' bank)
                from . import plugin_formats
                raw = plugin_formats.reaper_vst2_state(fx) or None
            chain.extend(P.vst_block(entry, comp, ctrl, fx.n_in, fx.n_out,
                                     fx.preset, indent=indent,
                                     vst2_ident=ident, raw_state=raw))
            chain.append('%sPRESETNAME %s' % (indent, q(fx.preset)))
            chain.append('%sFLOATPOS 0 0 0 0' % indent)
            chain.append('%sFXID %s' % (indent, guid_from('fx', i, k, fx.name)))
            chain.append('%sWAK 0 0' % indent)
            for pidx, pts, act in ([(a, b, 1) for a, b in fx.envelopes]
                                   + [(a, b, 0) for a, b in
                                      (getattr(fx, 'envelopes_idle', None) or [])]):
                chain.append('%s<PARMENV %d 0 1 0.5' % (indent, pidx))
                chain.append('%s  EGUID %s'
                             % (indent, guid_from('parmenv', i, k, pidx, act)))
                # ACT 0: a lane that does not play (Cubase automation Read off)
                chain.append('%s  ACT %d -1' % (indent, act))
                chain.append('%s  VIS 1 0 1' % indent)
                chain.append('%s  LANEHEIGHT 0 0' % indent)
                chain.append('%s  ARM 0' % indent)
                chain.append('%s  DEFSHAPE 0 -1 -1' % indent)
                for pos, val in pts:
                    chain.append('%s  PT %s %s 0' % (indent, fmt(pos), fmt(val)))
                chain.append('%s>' % indent)
                stats['parmenv'] += 1
        # Cubase's channel EQ sits after the inserts and before the fader;
        # it is not an insert. REAPER's own ReaEQ with the bands fitted to
        # Cubase's curve (each Cubase band type measured on Cubase exports),
        # last in the chain.
        if getattr(t, 'chan_eq', None):
            from . import chan_eq
            rb, err = chan_eq.to_reaeq(t.chan_eq, float(getattr(proj, 'samplerate', 48000) or 48000))
            chain.extend(stock.vst_lines('ReaEQ', chan_eq.reaeq_data(rb), indent, 0,
                                         1 if getattr(t, 'disabled', False) else 0,
                                         fxid=guid_from('chan_eq', i, t.name)))
            stats['chan_eq'] = stats.get('chan_eq', 0) + 1
            eq_worst[0] = max(eq_worst[0], err)
        return chain

    # sends live on the destination track in an RPP
    incoming = {}
    for i, t in enumerate(proj.tracks):
        for s in t.sends:
            if s.dest is not None:
                incoming.setdefault(s.dest, []).append((i, s))

    # A Cubase channel's output can be a group channel rather than Stereo
    # Out. A REAPER track only ever sums into its parent folder, so such a
    # track stops feeding its parent and sends, at unity after its own fader
    # and pan, to the group track instead - unless the group is the folder
    # it sits in, which sums it already. Left out, the group's effects never
    # heard those tracks (SuperThunderCrown: five groups silent, the mix
    # 1.1 dB off).
    routed = set()
    for i, g in output_routes(proj):
        incoming.setdefault(g, []).append((i, Send(dest=g, vol=1.0, pan=0.0, mode=0)))
        routed.add(i)

    L = []
    A = L.append
    A('<REAPER_PROJECT 0.1 "7.0/win64" 0')
    for x in HEADER:
        if x.startswith('PROJOFFS') and abs(getattr(proj, 'start', 0.0) or 0.0) > 1e-9:
            # Cubase's Project Setup > Start (negative: a pre-roll before
            # bar 1) is REAPER's project offset - time and whole bars
            st = float(proj.start)
            bpm = proj.tempo[0][1] if proj.tempo else 120.0
            num, den = (proj.tsig or (4, 4))[:2]
            bars = st * bpm / 60.0 / (num * 4.0 / den)
            x = 'PROJOFFS %s %d 0' % (fmt(st), int(round(bars)))
        A('  ' + x)
    A('  <RECORD_CFG')
    A('  >')
    A('  <APPLYFX_CFG')
    A('  >')
    for x in RENDER:
        A('  ' + x)
    A('  SAMPLERATE %d 0 0' % proj.samplerate)
    A('  <RENDER_CFG')
    A('  >')
    A('  LOCK 1')
    A('  <METRONOME 6 2')
    A('  >')
    A('  GLOBAL_AUTO -1')
    A('  TEMPO %s %d %d' % (fmt(proj.tempo[0][1]), proj.tsig[0], proj.tsig[1]))
    for x in MASTER:
        A('  ' + x)

    # Cubase's output bus is REAPER's master: its fader, its effects and its
    # automation all act on the summed mix, which is what they did in Cubase.
    m = getattr(proj, 'master', None)
    # MASTERAUTOMODE 0 (Trim/Read): the master fader multiplies the master
    # envelope, so it sits at unity under one
    A('  MASTER_VOLUME %s %s -1 -1 1'
      % (fmt(1.0 if (m and m.volenv) else (m.vol if m else 1.0)),
         fmt(m.pan) if m else '0'))
    if m is not None:
        mchain = fx_chain(m, -1, indent='    ')
        if mchain:
            A('  <MASTERFXLIST')
            A('    SHOW 0')
            A('    LASTSEL 0')
            A('    DOCKED 0')
            L.extend(mchain)
            A('  >')
            stats['master_fx'] = len(m.fx)
        if m.volenv:
            A('  <MASTERVOLENV2')
            A('    EGUID %s' % guid_from('mastervolenv', m.name))
            A('    ACT 1 -1')
            A('    VIS 1 0 1')
            A('    LANEHEIGHT 0 0')
            A('    ARM 0')
            A('    DEFSHAPE 0 -1 -1')
            # the model's points are straight in gain (amplitude scaling);
            # left out, REAPER may draw them on its fader scale instead
            A('    VOLTYPE 0')
            for pos, g in m.volenv:
                A('    PT %s %s 0' % (fmt(pos), fmt(g)))
            A('  >')
            stats['master_env'] = len(m.volenv)

    n = 0
    for m in sorted(proj.markers, key=lambda x: (x.start, x.name)):
        n += 1
        g = guid_from('marker', m.name, m.start)
        if m.end is None:
            A('  MARKER %d %s %s 0 0 1 R %s 0 0' % (n, fmt(m.start), q(m.name), g))
        else:
            A('  MARKER %d %s %s 1 0 1 R %s 0 0' % (n, fmt(m.start), q(m.name), g))
            A('  MARKER %d %s "" 1' % (n, fmt(m.end)))

    if len(proj.tempo) > 1:
        A('  <TEMPOENVEX')
        A('    ACT 1 -1')
        A('    VIS 1 0 1')
        A('    LANEHEIGHT 0 0')
        A('    ARM 0')
        A('    DEFSHAPE 1 -1 -1')
        for pos, bpm in proj.tempo:
            A('    PT %s %s 1' % (fmt(pos), fmt(bpm)))
        A('  >')

    tracks = proj.tracks
    bar = progress.Progress(len(tracks), 'writing REAPER project', 'tracks')
    for i, t in enumerate(tracks):
        bar.step(note=t.name[:26])
        if t.is_folder:
            isbus = '1 1'
        else:
            nxt = tracks[i + 1] if i + 1 < len(tracks) else None
            nd = nxt.depth if nxt else 0
            close = (nd - t.depth) if nxt else -t.depth
            isbus = '0 %d' % (close if close < 0 else 0)
        gid = guid_from('track', i, t.name)
        A('  <TRACK %s' % gid)
        A('    NAME %s' % q(t.name))
        A('    PEAKCOL %d' % (peakcol(t.color) if t.color else 16576))
        A('    BEAT -1')
        A('    AUTOMODE 0')
        vol_w, pan_w = reaper_mix(t, proj)
        # A mono Cubase channel's panner (sine/cosine, after the inserts)
        # is mapped like any other above: same L/R ratio, level on the
        # fader. Nothing else of the pan law touches the fader; a mono
        # file on a stereo channel gets the law's gain on the item (see
        # mono_item_gain), which is where Cubase applies it - before the
        # inserts, as the compressed and limited tracks of Gradila showed.
        if abs(pan_w - t.pan) > 1e-6 or abs(vol_w - t.vol) > 1e-6:
            stats['pans mapped'] = stats.get('pans mapped', 0) + 1
        # AUTOMODE 0 is Trim/Read: REAPER multiplies the fader into the
        # volume envelope and adds the pan control to the pan envelope. An
        # envelope holds what plays, so only the pan-law compensation
        # stays on the fader under one, and nothing on the pan
        venv = t.volenv
        if t.volenv:
            vol_w = vol_w / t.vol if abs(t.vol) > 1e-12 else 1.0
        if t.panenv:
            pan_w = 0.0
            if getattr(proj, 'pan_law_of', None) == 'cubase':
                # Cubase's panner keeps the louder channel at full level
                # and REAPER's boosts towards the sides: the level the
                # static case puts on the fader rides on the volume
                # envelope along a pan lane (envelope.py)
                from . import envelope, panlaw
                kw = _pan_args(t, proj)
                venv = envelope.volume_with_pan_gain(
                    t.volenv, 1.0, t.panenv,
                    lambda c: panlaw.cubase_to_reaper(
                        c, WRITER_PANLAW, WRITER_PANMODE, **kw)[1])
                # the fader keeps the channel's level (Trim/Read multiplies
                # it in) unless the envelope already carries it
                vol_w = 1.0 if t.volenv else t.vol
        A('    VOLPAN %s %s -1 -1 1' % (fmt(vol_w), fmt(pan_w)))
        # a disabled Cubase track plays nothing and has its plug-ins
        # unloaded: the nearest REAPER has is muted with its FX offline
        A('    MUTESOLO %d %d 0' % (1 if t.disabled else t.mute, t.solo))
        A('    IPHASE 0')
        dl = float(getattr(t, 'delay', 0.0) or 0.0)
        # the track delay as REAPER's media playback offset in seconds
        # (flag 0 = on and in seconds; '0 1' is REAPER's off)
        A('    PLAYOFFS %s' % (('%s 0' % fmt(dl)) if abs(dl) > 1e-9 else '0 1'))
        if abs(dl) > 1e-9:
            stats['track delays'] = stats.get('track delays', 0) + 1
        A('    ISBUS %s' % isbus)
        A('    BUSCOMP 0 0 0 0 0')
        A('    SHOWINMIX 1 0.6667 0.5 1 0.5 0 0 0')
        if len(t.lane_names) > 1:
            # FREEMODE 2 is what puts the track in fixed-lane mode; the
            # lane lines alone leave it in free positioning, where every
            # lane plays at once (six versions of a violin at once)
            A('    FREEMODE 2')
        A('    FIXEDLANES 9 0 0 0 0')
        if len(t.lane_names) > 1:
            # in the order REAPER itself saves them - LANESOLO (the lanes
            # that play) before ITEMLANES; written the other way round,
            # REAPER played every lane of a six-version violin track
            A('    LANESOLO %d 0 0 0 0 0 0 0' % (1 << t.active_lane))
            A('    LANEREC -1 -1 -1 %d' % len(t.lane_names))
            A('    LANENAME %s' % ' '.join(q(n) for n in t.lane_names))
            A('    ITEMLANES %d' % len(t.lane_names))
        A('    SEL 0')
        A('    REC 0 0 1 0 0 0 0 0')
        A('    VU 2')
        A('    TRACKHEIGHT 0 0 0 0 0 0 0')
        A('    INQ 0 0 0 0.5 100 0 0 100')
        A('    NCHAN 2')
        A('    FX 1')
        A('    TRACKID %s' % gid)
        A('    PERF 0')
        A('    MIDIOUT -1 -1')
        A('    MAINSEND %d 0' % (0 if i in routed else 1))
        for src, s in incoming.get(i, []):
            A('    AUXRECV %d %d %s %s %d 0 0 0 0 -1 0 -1'
              % (src, s.mode, fmt(s.vol), fmt(s.pan),
                 1 if getattr(s, 'mute', False) else 0))
            stats['sends'] += 1

        if venv:
            A('    <VOLENV2')
            A('      EGUID %s' % guid_from('volenv', i, t.name))
            A('      ACT 1 -1')
            A('      VIS 1 0 1')
            A('      LANEHEIGHT 0 0')
            A('      ARM 0')
            A('      DEFSHAPE 0 -1 -1')
            A('      VOLTYPE 0')
            # the fader multiplies the envelope (Trim/Read, VOLPAN above),
            # so a mono channel's pan law gain sits there, not on the points
            for pos, g in venv:
                A('      PT %s %s 0' % (fmt(pos), fmt(g)))
            A('    >')

        # lanes that do not play (Cubase automation Read off): written as
        # inactive envelopes (ACT 0), so REAPER plays the fader, as Cubase
        # does, and keeps the curves. Their points are relative to the
        # fader, as a Trim/Read envelope is, so switching one on plays what
        # switching Read on in Cubase plays.
        if getattr(t, 'volenv_idle', None) and not venv:
            base_v = t.vol if abs(t.vol) > 1e-12 else 1.0
            A('    <VOLENV2')
            A('      EGUID %s' % guid_from('volenv', i, t.name, 'idle'))
            A('      ACT 0 -1')
            A('      VIS 1 0 1')
            A('      LANEHEIGHT 0 0')
            A('      ARM 0')
            A('      DEFSHAPE 0 -1 -1')
            A('      VOLTYPE 0')
            for pos, g in t.volenv_idle:
                A('      PT %s %s 0' % (fmt(pos), fmt(g / base_v)))
            A('    >')
            stats['idle lanes'] = stats.get('idle lanes', 0) + 1
        if getattr(t, 'panenv_idle', None) and not t.panenv:
            A('    <PANENV')
            A('      EGUID %s' % guid_from('panenv', i, t.name, 'idle'))
            A('      ACT 0 -1')
            A('      VIS 1 1 1')
            A('      LANEHEIGHT 0 0')
            A('      ARM 0')
            A('      DEFSHAPE 0 -1 -1')
            for pos, v in reaper_pan_curve(t, proj, t.panenv_idle):
                # REAPER pan envelopes run the other way: +1 is hard left
                A('      PT %s %s 0' % (fmt(pos), fmt(-(v - pan_w))))
            A('    >')
            stats['idle lanes'] = stats.get('idle lanes', 0) + 1

        if t.panenv:
            A('    <PANENV')
            A('      EGUID %s' % guid_from('panenv', i, t.name))
            A('      ACT 1 -1')
            A('      VIS 1 1 1')
            A('      LANEHEIGHT 0 0')
            A('      ARM 0')
            A('      DEFSHAPE 0 -1 -1')
            for pos, v in reaper_pan_curve(t, proj, t.panenv):
                # REAPER pan envelopes run the other way: +1 is hard left
                A('      PT %s %s 0' % (fmt(pos), fmt(-v)))
            A('    >')

        chain = fx_chain(t, i)
        if chain:
            A('    <FXCHAIN')
            A('      SHOW 0')
            A('      LASTSEL 0')
            A('      DOCKED 0')
            L.extend(chain)
            A('    >')

        # Cubase stacks events on a track's lanes and plays the one on top,
        # the last in its list. Events sharing a span become one REAPER item
        # with a take each, the top one selected - REAPER's own way of
        # holding alternatives. Overlaps with different spans have no exact
        # counterpart: REAPER would play them all, so the covered ones are
        # muted and the summary says so.
        n_cut, n_gone = flatten_lanes(t, log)
        if n_cut or n_gone:
            stats['comped'] = stats.get('comped', 0) + n_cut + n_gone
        groups = []
        for it in sorted(t.items, key=lambda x: x.pos):
            key = (round(it.pos, 4), round(it.length, 4),
                   getattr(it, 'lane', 0), it.kind)
            if groups and groups[-1][0] == key:
                groups[-1][1].append(it)
            else:
                groups.append((key, [it]))
        for k, (_key, stack) in enumerate(groups):
            top = stack[-1]
            A('    <ITEM')
            A('      POSITION %s' % fmt(top.pos))
            A('      SNAPOFFS 0')
            A('      LENGTH %s' % fmt(max(top.length, 1e-7)))
            A('      LOOP 0')
            A('      ALLTAKES 0')
            A('      FADEIN ' + fade_line(top, 'in', stats, log, t.name))
            A('      FADEOUT ' + fade_line(top, 'out', stats, log, t.name))
            A('      MUTE %d 0' % top.mute)
            A('      SEL 0')
            if len(t.lane_names) > 1:
                n_l = len(t.lane_names)
                A('      YPOS %s %s 2'
                  % (fmt(round(top.lane / float(n_l), 6)),
                     fmt(round(1.0 / n_l, 6))))
            A('      IGUID %s' % guid_from('item', i, k, top.name, top.pos))
            A('      IID %d' % (k + 1))
            if getattr(top, 'color', None):
                A('      COLOR %d B' % peakcol(top.color))
            if len(stack) > 1:
                stats['takes'] = stats.get('takes', 0) + len(stack) - 1
            for n, it in enumerate(stack):
                if n:
                    A('      TAKE%s' % (' SEL' if it is top else ''))
                A('      NAME %s' % q(it.name))
                mg = mono_item_gain(it, t, proj)
                if mg != 1.0:
                    stats['mono files at the pan law'] = \
                        stats.get('mono files at the pan law', 0) + 1
                # a take volume envelope tops out at +6 dB in REAPER, and a
                # Cubase event curve goes to +24: the envelope is written
                # relative to its own peak and the take volume carries it
                env_scale = 1.0
                if it.kind == 'audio' and getattr(it, 'volenv', None):
                    peak = max(g for _t, g in it.volenv)
                    if peak > 2.0:
                        env_scale = peak
                take_vol = it.gain * mg * env_scale
                if (it.kind == 'video' and getattr(proj, 'pan_law_of', None) == 'cubase'
                        and not getattr(it, 'video_sound', False)):
                    # a Cubase video track plays no sound; REAPER plays a
                    # video file's audio through the item, so the take is
                    # silent here (Agata's video would have added its own
                    # soundtrack to the REAPER mix)
                    take_vol = 0.0
                    stats['video items silent as in Cubase'] =                         stats.get('video items silent as in Cubase', 0) + 1
                A('      VOLPAN %s 0 1 -1' % fmt(take_vol))
                A('      SOFFS %s' % fmt(it.soffs))
                # second field is preserve-pitch: a Cubase musical-mode clip
                # is pitch-preserved, so a stretched item keeps its key; the
                # third is the pitch shift in semitones (a Cubase event's
                # Transpose + Fine-tune, cpr_read's 'FtiP' record)
                A('      PLAYRATE %s 1 %s -1 0 0.0025'
                  % (fmt(it.playrate or 1.0), fmt(getattr(it, 'pitch', 0.0) or 0.0)))
                if getattr(it, 'stretch_markers', None):
                    # (position in take time, source second) pairs
                    A('      SM %s' % ' + '.join('%s %s' % (fmt(p), fmt(q))
                                                 for p, q in it.stretch_markers))
                A('      CHANMODE 0')
                A('      GUID %s' % guid_from('take', i, k, n, it.name, it.pos))
                if it.kind == 'empty':
                    pass        # an item with no take: REAPER writes no
                                # <SOURCE> for one, and that is what it is
                elif it.kind in ('audio', 'video'):
                    mel = ara_names.get(getattr(it, 'ara_id', None) or '')
                    if mel:
                        # the take's ARA modification: REAPER's first one
                        A('      ARAMOD %s' % _ara.FIRST_MOD)
                    sec = getattr(it, 'section', None) if it.kind == 'audio' else None
                    if sec:
                        # REAPER's own reversed or cut take: the source
                        # inside a SECTION (Item > Reverse writes MODE 3,
                        # LENGTH the file's, STARTPOS 0; SOFFS then counts
                        # into the reversed audio)
                        A('      <SOURCE SECTION')
                        A('        LENGTH %s' % fmt(sec.get('length') or 0.0))
                        A('        MODE %d' % int(sec.get('mode') or 0))
                        A('        STARTPOS %s' % fmt(sec.get('startpos') or 0.0))
                        A('        OVERLAP %s' % fmt(sec.get('overlap', 0.01)))
                        A('        <SOURCE %s' % source_type(it.file, it.kind))
                        A('          FILE %s' % q(media(it.file)))
                        A('        >')
                        A('      >')
                        if sec.get('reverse'):
                            stats['reversed items'] = stats.get('reversed items', 0) + 1
                    else:
                        A('      <SOURCE %s' % source_type(it.file, it.kind))
                        A('        FILE %s' % q(media(it.file)))
                        A('      >')
                    if mel:
                        L.extend(_ara.takefx('      '))
                        ara_takes.append(
                            (guid_from('take', i, k, n, it.name, it.pos), mel))
                        stats['Melodyne takes'] = stats.get('Melodyne takes', 0) + 1
                    if it.kind == 'audio' and getattr(it, 'volenv', None):
                        # the event's own volume curve (Cubase's drawn
                        # event envelope) as the take's volume envelope:
                        # seconds into the item, linear gain
                        A('      <VOLENV')
                        A('        EGUID %s' % guid_from('takevol', i, k, n, it.name))
                        A('        ACT 1 -1')
                        A('        VIS 1 1 1')
                        A('        LANEHEIGHT 0 0')
                        A('        ARM 0')
                        A('        DEFSHAPE 0 -1 -1')
                        # amplitude scaling: REAPER then draws straight
                        # lines in gain, which is what the model's points
                        # are (fader scaling, VOLTYPE 1, bends them)
                        A('        VOLTYPE 0')
                        # take time: the item's seconds times its playrate
                        # (REAPER stretches a take envelope with the audio)
                        tr_ = it.playrate or 1.0
                        for pos_e, g in it.volenv:
                            A('        PT %s %s 0' % (fmt(pos_e * tr_), fmt(g / env_scale)))
                        A('      >')
                        stats['event volume curves'] = (
                            stats.get('event volume curves', 0) + 1)
                    if it.kind == 'audio' and getattr(it, 'pitchenv', None):
                        # VariAudio's moved notes (cpr_read.variaudio_env):
                        # semitones over the item, stepping at the notes
                        A('      <PITCHENV')
                        A('        EGUID %s' % guid_from('takepitch', i, k, n, it.name))
                        A('        ACT 1 -1')
                        A('        VIS 1 1 1')
                        A('        LANEHEIGHT 0 0')
                        A('        ARM 0')
                        A('        DEFSHAPE 0 -1 -1')
                        tr_ = it.playrate or 1.0
                        for pos_e, v in it.pitchenv:
                            A('        PT %s %s 0' % (fmt(pos_e * tr_), fmt(v)))
                        A('      >')
                        stats['VariAudio pitch curves'] = (
                            stats.get('VariAudio pitch curves', 0) + 1)
                else:
                    lines, last = midi_events(it.notes, it.ppq, ccs=it.ccs)
                    A('      <SOURCE MIDI')
                    A('        HASDATA 1 960 QN')
                    for x in lines:
                        A('        ' + x)
                    end = int(round(it.ticks * (960.0 / (it.ppq or 480.0))))
                    A('        E %d b0 7b 00' % max(0, end - last))
                    A('      >')
            A('    >')
        A('  >')
    if ara_takes:
        # the document, and which ID each take's audio source has in it
        A('  <ARA %s 4' % _ara.CHUNK)
        A('    <BIN')
        L.extend(_ara.b64_lines(ara_blob, '      '))
        A('    >')
        A('  >')
        A('  <ARASRC %s 4' % _ara.CHUNK)
        for g, name in ara_takes:
            A('    %s %s' % (g, q(name)))
        A('  >')
        A('  ARA_POOLED_EDITS')
        log.append("%d take(s) with Melodyne edits: REAPER's Melodyne opens "
                   "Cubase's document with them (Melodyne has to be installed "
                   "to hear them)" % len(ara_takes))
    A('>')
    bar.done()
    if stats.get('video items silent as in Cubase'):
        log.append('%d video item(s): a Cubase video track plays no sound, so '
                   'the video\'s own audio is turned down to nothing in REAPER '
                   '(the picture plays as before)'
                   % stats['video items silent as in Cubase'])
    if stats.get('pans mapped'):
        log.append('%d panned channel(s): Cubase\'s balance panner is linear '
                   'and REAPER\'s (0 dB law) boosts the pair towards the '
                   'centre, so each track got the REAPER pan with the same '
                   'L/R ratio and the level difference on its fader - the '
                   'two play the same on both channels' % stats['pans mapped'])
    for how, tracks in sorted(stock_notes.items()):
        log.append('%s on %d track(s): %s - REAPER\'s own plug-in with the '
                   'same settings, nothing to install'
                   % (how, len(tracks), ', '.join(sorted(set(tracks)))))
    if stats.get('chan_eq'):
        log.append('%d channel EQ(s) became REAPER\'s ReaEQ with its bands fitted '
                   'to Cubase\'s curve (largest difference %.2f dB, 20 Hz - 20 kHz)'
                   % (stats['chan_eq'], eq_worst[0]))
    text = '\n'.join(L) + '\n'
    progress.stage('saving %s (%s)'
                   % (os.path.basename(path), progress.fmt_bytes(len(text))))
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return stats
