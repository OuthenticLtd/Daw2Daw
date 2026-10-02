"""Read a REAPER project (.rpp) into the intermediate model."""
import base64
import os
import re

from . import plugins as P
from .model import Project, Track, Item, Marker, Fx, Send


def split_tokens(line):
    """RPP quoting: a token may be wrapped in " ' or ` ."""
    out = []
    i = 0
    n = len(line)
    while i < n:
        while i < n and line[i] == ' ':
            i += 1
        if i >= n:
            break
        if line[i] in '"\'`':
            qc = line[i]
            j = line.find(qc, i + 1)
            if j < 0:
                j = n
            out.append(line[i + 1:j])
            i = j + 1
        else:
            j = line.find(' ', i)
            if j < 0:
                j = n
            out.append(line[i:j])
            i = j
    return out


class Block:
    def __init__(self, name, args):
        self.name = name
        self.args = args
        self.lines = []      # (name, args) for plain lines
        self.blocks = []     # child Block
        self.raw = []        # every child line verbatim, in order

    def get(self, name, default=None):
        for k, a in self.lines:
            if k == name:
                return a
        return default

    def all(self, name):
        return [a for k, a in self.lines if k == name]

    def child(self, name):
        for b in self.blocks:
            if b.name == name:
                return b
        return None

    def children(self, name):
        return [b for b in self.blocks if b.name == name]


def parse(path):
    return parse_lines(open(path, encoding='utf-8', errors='replace'))


def parse_lines(lines):
    root = Block('ROOT', [])
    stack = [root]
    for raw in lines:
        line = raw.rstrip('\r\n')
        s = line.strip()
        if not s:
            stack[-1].raw.append('')
            continue
        if s.startswith('<'):
            tok = split_tokens(s[1:])
            b = Block(tok[0] if tok else '', tok[1:])
            stack[-1].blocks.append(b)
            stack[-1].raw.append(b)
            stack.append(b)
        elif s == '>':
            if len(stack) > 1:
                stack.pop()
        else:
            tok = split_tokens(s)
            if tok:
                stack[-1].lines.append((tok[0], tok[1:]))
                stack[-1].raw.append(s)
    return root


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _i(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def pan_env_pts(block):
    """A REAPER pan envelope's points in the pan knob's sense (-1 left):
    REAPER draws pan envelopes the other way up, +1 hard left."""
    return [(t, -v) for t, v in env_pts(block)]


def env_pts(block, kind='value'):
    """A REAPER envelope block's points as straight segments.

    A point's third field is the shape of the segment that leaves it and
    its seventh the bezier tension; both are turned into extra points
    (envelope.expand_shapes), within 0.02 dB for a volume envelope and
    0.002 for anything else, since the model and Cubase only draw lines.
    A volume envelope saved with fader scaling (VOLTYPE 1, REAPER's
    default) runs its segments on the fader's scale, not in gain - the
    points are re-drawn in gain to play the same (measured on take
    envelopes, 2026-09-30; track and master ones are drawn alike: Cherry
    Link's master ramps came out up to 2.6 dB off, 2026-10-01)."""
    from . import envelope
    pts = []
    for a in block.all('PT'):
        if len(a) < 2:
            continue
        shape = _i(a[2], 0) if len(a) > 2 else 0
        tens = _f(a[6], 0.0) if len(a) > 6 else 0.0
        pts.append((_f(a[0]), _f(a[1]), shape, tens))
    if kind == 'volume' and _i((block.get('VOLTYPE') or ['0'])[0], 0) == 1             and len(pts) > 1:
        return envelope.volume_from_reaper_fader_shaped(pts)
    if not any(s not in (0, None) for _, _, s, _ in pts):
        return [(t, v) for t, v, _, _ in pts]
    if kind == 'volume':
        return envelope.expand_shapes(pts, envelope.db_err, envelope.DB_TOL)
    return envelope.expand_shapes(pts, envelope.pan_err, envelope.PAN_TOL)


def _fold_trim(t, automode):
    """Make a track's envelopes absolute.

    In REAPER's default automation mode, Trim/Read (AUTOMODE 0), the fader
    is multiplied into the volume envelope and the pan control is added to
    the pan envelope; in Read and the writing modes the envelope drives the
    control and the stored value plays no part. Cubase's lane always
    replaces the fader, so the model keeps envelopes as what actually
    plays (identity test 2026-09-28: a fader at 0.7 under an envelope came
    out 3.1 dB louder in Cubase until this was folded in)."""
    if automode != 0:
        if t.volenv:
            t.vol = 1.0
        if t.panenv:
            t.pan = 0.0
        return
    if t.volenv and abs(t.vol - 1.0) > 1e-9:
        t.volenv = [(s, g * t.vol) for s, g in t.volenv]
        t.vol = 1.0
    if t.panenv and abs(t.pan) > 1e-9:
        t.panenv = [(s, max(-1.0, min(1.0, v + t.pan))) for s, v in t.panenv]
        t.pan = 0.0


def rgb_from_peakcol(v):
    if not v or v < 0x1000000:
        return None
    v &= 0xFFFFFF
    return (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


NATIVE_BLOCKS = ('JS', 'CLAP', 'AU', 'LV2', 'DX', 'VIDEO_EFFECT')


def clap_state(blk):
    """The plug-in's settings out of a <CLAP> block's <STATE>.

    A CLAP keeps its state as one base64 blob rather than as the three
    REAPER wraps a VST in, so it is read whole."""
    for sub in blk.blocks:
        if sub.name != 'STATE':
            continue
        j = ''.join(x.strip() for x in sub.raw if isinstance(x, str))
        if not j:
            return b''
        try:
            return base64.b64decode(j + '=' * (-len(j) % 4))
        except Exception:
            return b''
    return b''


def clap_as_vst(fx, blk, index):
    """Point a <CLAP> at the VST build of the same plug-in, if there is one.

    Cubase loads no CLAP, but the plug-in behind one usually ships as a VST3
    as well: Hive loaded as a CLAP and Hive loaded as a VST3 are the same
    synth reading the same patch. Matching the two by product name keeps the
    track a track - its MIDI, its lanes and its instrument - where the only
    other way across is printing it to audio, which flattens all three.

    Sets the plug-in up exactly as a <VST> block would have and returns True
    when it matched; leaves it REAPER-only and returns False when it did
    not, so such a track is still printed."""
    disp = blk.args[0] if blk.args else ''
    clap_id = blk.args[1] if len(blk.args) > 1 else ''
    product, vendor, inst = P.clap_identity(disp, clap_id)
    fx.is_instrument = inst
    fx.clap_id = clap_id
    fx.mapped_from = 'CLAP'
    entry = index.match_product(product, vendor, inst) if index else None
    if entry is None or not entry['uid']:
        # named for the print note: 'Hive (CLAP)' says which of the two
        # builds of a plug-in the project actually used
        if product:
            fx.name = '%s (CLAP)' % product
        return False
    fx.name = product
    fx.uid = entry['uid'].upper()
    fx.native = False
    if not os.environ.get('CPR_CLAP_NO_STATE'):
        fx.component, fx.controller = P.state_for_vst3(clap_state(blk))
    return True


def note_empty(p, log):
    """Say what the items with no take were, and where."""
    per = {}
    for t in p.tracks:
        n = sum(1 for i in t.items if i.kind == 'empty')
        if n:
            per[t.name] = n
    if not per:
        return
    total = sum(per.values())
    labels = [i.name for t in p.tracks for i in t.items
              if i.kind == 'empty' and i.name][:6]
    log.append('%d item(s) hold no audio and no notes - empty REAPER items, '
               'which is how a chord chart or a written note on the timeline '
               'is made (%s). Cubase has no such event, so they were left '
               'out rather than written as sound: %s'
               % (total,
                  ', '.join('%s x%d' % (n, c) for n, c in sorted(per.items())),
                  ', '.join(labels) + (' ...' if len(labels) == 6 else '')
                  if labels else 'they carry no text'))


def note_clap(p, log):
    """One line each for the CLAPs that crossed over and those that did not."""
    got, missed = {}, {}
    tracks = list(p.tracks) + ([p.master] if p.master is not None else [])
    for t in tracks:
        chain = ([t.instrument] if t.instrument is not None else []) + list(t.fx)
        for f in chain:
            if getattr(f, 'mapped_from', '') != 'CLAP':
                continue
            where = missed if getattr(f, 'native', False) else got
            where[f.name] = where.get(f.name, 0) + 1
    def listed(d):
        return ', '.join('%s x%d' % (n, c) if c > 1 else n
                         for n, c in sorted(d.items()))
    if got:
        log.append('%d CLAP plug-in(s) were loaded as the VST3 build of the '
                   'same plug-in, settings and all, since Cubase loads no '
                   'CLAP: %s. Their tracks keep their MIDI and their lanes'
                   % (sum(got.values()), listed(got)))
    if missed:
        log.append('%d CLAP plug-in(s) have no VST build in the scanned '
                   'plug-in list, so nothing in Cubase can load them: %s'
                   % (sum(missed.values()), listed(missed)))


def read_fx(chain, log, index=None):
    out = []
    bypass_for_next = 0
    offline_for_next = 0
    group = None            # the lines belonging to the plug-in being read
    for entry in chain.raw:
        if isinstance(entry, str):
            tok = split_tokens(entry)
            # the preset a plug-in was on, written after its block; what a
            # plug-in that arrives without its settings can be put back to
            if tok and tok[0] == 'PRESETNAME' and len(tok) > 1 and out:
                out[-1].preset = tok[1]
            if tok and tok[0] == 'BYPASS':
                bypass_for_next = _i(tok[1] if len(tok) > 1 else 0)
                offline_for_next = _i(tok[2] if len(tok) > 2 else 0)
                group = [entry]
            elif group is not None:
                group.append(entry)
            continue
        b = entry
        if group is None:
            group = []
        group.append(b)
        # a parameter envelope follows the plug-in it automates
        if b.name == 'PARMENV' and out:
            pts = env_pts(b)
            if pts:
                on = _i((b.get('ACT') or ['1'])[0], 1)
                (out[-1].envelopes if on else out[-1].envelopes_idle).append(
                    (_i(b.args[0] if b.args else 0), pts))
            continue
        if b.name not in ('VST', 'VST3'):
            if b.name in NATIVE_BLOCKS:
                # a plug-in Cubase cannot load as it stands: read so the
                # track can be printed through it (print_tracks). A CLAP
                # gets one chance first at being the VST build of itself
                fx = Fx()
                fx.name = '%s: %s' % (b.name, b.args[0] if b.args else '?')
                fx.native = True
                fx.bypass = bypass_for_next
                fx.offline = offline_for_next
                fx.chain_pos = len(out)
                fx.raw_group = group
                if b.name == 'CLAP':
                    clap_as_vst(fx, b, index)
                if b.name == 'JS' and b.args:
                    # one of REAPER's own JS effects that one of Cubase's
                    # own does the same as (builtins.py) becomes that one
                    from . import builtins
                    sl = next((x.split() for x in b.raw
                               if isinstance(x, str) and x.strip()), [])
                    eqs = builtins.cubase_equivalents(b.args[0], [
                        v for v in sl if v != '-'][:64] if sl else [])
                    if eqs:
                        fx.uid, fx.component, fx.name = eqs[0]
                        fx.native = False
                        fx.raw_group = None
                        for uid2, comp2, name2 in eqs[1:]:
                            # the rest of the equivalent, right after it
                            f2 = Fx()
                            f2.uid, f2.component, f2.name = uid2, comp2, name2
                            f2.native = False
                            f2.bypass = fx.bypass
                            f2.offline = fx.offline
                            f2.mapped_from = ident_js = b.args[0]
                            out.append(fx)
                            fx = f2
                bypass_for_next = offline_for_next = 0
                out.append(fx)
            continue
        fx = Fx()
        ident = b.args[0] if b.args else ''
        fx.name = ident.split(': ', 1)[-1].split(' (')[0]
        # REAPER's own effects and instruments exist nowhere else
        fx.native = '(Cockos)' in ident
        fx.chain_pos = len(out)
        fx.raw_group = group
        for a in b.args:
            if '{' in a and '}' in a:
                fx.uid = a[a.index('{') + 1:a.index('}')].upper()
            elif '<' in a and '>' in a and not fx.uid:
                # a VST2: REAPER writes the same 16-byte identity Cubase
                # builds for it ("VST" + four-character id + name)
                m = re.search(r'<([0-9A-Fa-f]{32})>', a)
                if m:
                    fx.uid = m.group(1).upper()
        blobs = P.unwrap_b64([x for x in b.raw if isinstance(x, str)])
        if len(blobs) >= 2:
            head = P.parse_vst_header(blobs[0])
            if head:
                fx.n_in, fx.n_out = head['n_in'], head['n_out']
                if not fx.uid:
                    fx.vst2_id = head['num']
            if blobs[1][:8] == P.PARAM_DUMP:
                # not the plug-in's state but REAPER's parameter dump,
                # which only REAPER can put back (rehost.py does)
                fx.param_dump = True
                fx.component, fx.controller = b'', b''
            else:
                fx.component, fx.controller = P.parse_vst_state(blobs[1])
            # the chunk as REAPER wrote it: a VST2 (ReaEQ) keeps its state
            # raw there, not as the VST3 pair parse_vst_state splits
            fx.raw_state = blobs[1]
        fx.bypass = bypass_for_next
        fx.offline = offline_for_next
        fx.is_instrument = ident.split(':', 1)[0].endswith('i')
        # the plug-in format, as REAPER names it ('VST3', 'VST3i', 'VST',
        # 'VSTi', 'CLAP'...): a VST3 built on Steinberg's VST2 wrapper has a
        # class id beginning 'VST' too (Pianoteq, Kontakt, Valhalla), so the
        # id alone does not tell a VST2 from a VST3
        fx.format = ident.split(':', 1)[0].rstrip('i') if ':' in ident else ''
        bypass_for_next = offline_for_next = 0
        out.append(fx)
    return out


def finish_fx(t, p, log):
    """What a track's chain becomes beyond REAPER: a ReaEQ last in it is
    Cubase's channel EQ, REAPER's own effects with a Cubase counterpart are
    that (keeping the REAPER original as fx.reaper_stock). Also run on the
    REAPER blocks Live's own devices become (als_read)."""
    # the channel EQ, if it came across as the converter's JS effect
    ce = [f for f in t.fx if getattr(f, 'chan_eq_bands', None) is not None]
    if ce:
        t.chan_eq = [] if ce[-1].bypass else list(ce[-1].chan_eq_bands)
        t.fx = [f for f in t.fx if f not in ce]
    elif t.fx and t.fx[-1].native and (t.fx[-1].name or '').startswith('ReaEQ')                     and not t.fx[-1].offline:
        # a ReaEQ last in the chain plays where Cubase's channel EQ
        # does (after the inserts): it becomes that, when its bands
        # fit Cubase's four
        from . import chan_eq
        rb = chan_eq.reaeq_state_bands(getattr(t.fx[-1], 'raw_state', None) or t.fx[-1].component)
        if rb is not None:
            # the Cubase bands that play it most closely (both EQs
            # modelled from renders; up to four bands)
            bands, worst = chan_eq.fit_reaeq(rb)
            if bands is None:
                # more kinds of band than Cubase's four hold (a
                # shelf and a cut both on top): the closest with one
                # band less, judged against the whole ReaEQ curve
                full = chan_eq._total(chan_eq.reaeq_band_db, rb, 48000.0)
                best = None
                for k in range(len(rb)):
                    sub = rb[:k] + rb[k + 1:]
                    b2, _w = chan_eq.fit_reaeq(sub) if sub else (None, None)
                    if b2 is None:
                        continue
                    got = chan_eq._total(chan_eq.cubase_band_db, b2, 48000.0)
                    w = max(abs(x - y) for x, y in zip(got, full))
                    if best is None or w < best[0]:
                        best = (w, b2)
                if best is not None:
                    worst, bands = best
            if bands is not None and any(x[0] == 3 for x in rb):
                # the channel EQ has no low pass (only shelves standing in for
                # one, 0.6 dB off on a Cubase export); Frequency has the real one
                bands = None
            if bands is not None and worst > 0.5:
                # Frequency (an insert, eight bands) plays it closer than the
                # channel EQ's four: left for that (below)
                from . import freq_eq
                if freq_eq.records_for(rb)[1] < worst:
                    bands = None
            if bands is not None:
                t.chan_eq = [] if t.fx[-1].bypass else bands
                # ReaEQ's own bands, for a writer with an EQ that
                # takes them more directly than Cubase's four (Live's
                # EQ Eight - als_write)
                t.reaeq_bands = [] if t.fx[-1].bypass else rb
                t.fx = t.fx[:-1]
                log.append("%r: its ReaEQ became Cubase's channel EQ (%s), within "
                           "%.2f dB of ReaEQ's curve%s"
                           % (t.name, chan_eq.describe(bands), worst,
                              '' if worst <= 0.5 else ' - more than the 0.5 dB this '
                              'converter holds itself to: check it by ear'))
    # REAPER's own plug-ins with a Cubase counterpart become that
    # one of Cubase's own effects, with the same settings (stock.py)
    from . import stock
    eqs = []
    for fx in list(t.fx):
        if fx.native and (fx.name or '').startswith('ReaEQ') and not fx.offline:
            # a ReaEQ anywhere else in the chain: Cubase's Frequency, a
            # band for each (more than eight: a second one after it)
            from . import chan_eq, freq_eq, builtins
            data = getattr(fx, 'raw_state', None) or fx.component
            rb = chan_eq.reaeq_state_bands(data)
            if rb is None:
                continue
            recs, worst = freq_eq.records_for(rb, log)
            new = []
            for rec in recs:
                st = builtins.table_state('Frequency', rec)
                if st is None:
                    new = []
                    break
                f2 = Fx()
                f2.uid, f2.component, f2.name = freq_eq.UID, st, 'Frequency'
                f2.controller = builtins._template('Frequency')[1]
                f2.bypass, f2.offline, f2.chain_pos = fx.bypass, fx.offline, fx.chain_pos
                f2.reaper_stock = ('ReaEQ', data)
                # a second Frequency only holds the bands past eight: the
                # one ReaEQ (or EQ Eight) elsewhere has them all
                f2.cubase_only = bool(new)
                if getattr(fx, 'rpp_lines', None) and not new:
                    f2.rpp_lines = fx.rpp_lines
                new.append(f2)
            if new:
                k = t.fx.index(fx)
                t.fx[k:k + 1] = new
                log.append("%r: ReaEQ -> Cubase's Frequency (%d band(s)), within %.2f dB of "
                           "ReaEQ's curve%s" % (t.name, len(rb), worst,
                                                '' if worst <= 0.5 else ' - check it by ear'))
    for fx in t.fx:
        if fx.native and (fx.name or '') in ('ReaComp', 'ReaDelay', 'ReaLimit', 'ReaGate', 'ReaVerbate', 'ReaPitch'):
            data = getattr(fx, 'raw_state', None) or fx.component
            got = stock.to_cubase(fx.name, data, tempo=(p.tempo[0][1] if p.tempo else 120.0))
            if got:
                uid, st, cname, how = got
                # the REAPER original, for a writer whose own stock
                # effects map from it rather than from Cubase's
                # (als_write -> live_stock)
                fx.reaper_stock = (fx.name, data)
                log.append('%r: %s -> %s' % (t.name, fx.name, how))
                fx.uid, fx.component, fx.name = uid, st, cname
                fx.controller = b''
                fx.native = False
                fx.raw_group = None
    # the rest of REAPER's own effects that one of Cubase's own plays
    # (natives.py): ReaXcomp, and the JS effects it maps
    from . import natives
    tempo = p.tempo[0][1] if p.tempo else 120.0
    for fx in t.fx:
        if not fx.native or fx.offline:
            continue
        nm = fx.name or ''
        if nm in ('ReaXcomp', 'ReaPitch'):
            key, payload = nm, getattr(fx, 'raw_state', None) or fx.component
        elif nm.startswith('JS: '):
            key, payload = 'JS:' + nm[4:].strip(), natives.js_sliders(fx)
        else:
            continue
        got = natives.to_cubase(key, payload, tempo)
        if not got:
            continue
        uid, st, cname, how = got[:4]
        if len(got) > 4:
            # a level after it (Cubase's Volume): the effect has none of its own
            k = t.fx.index(fx)
            for uid2, st2, name2 in got[4]:
                f2 = Fx()
                f2.uid, f2.component, f2.name = uid2, st2, name2
                f2.bypass, f2.offline = fx.bypass, fx.offline
                f2.cubase_only = True       # REAPER and Live keep the original's level
                t.fx.insert(k + 1, f2)
        if key in ('ReaXcomp', 'ReaPitch'):
            fx.reaper_stock = (key, payload)
        else:
            fx.reaper_js = (key[3:], payload)
        log.append('%r: %s -> %s' % (t.name, nm, how))
        fx.uid, fx.component, fx.name = uid, st, cname
        fx.controller = b''
        fx.native = False
        fx.raw_group = None


def read(path, log=None, _depth=0, printer=None, index=None):
    """Read a .rpp. `printer`, when given, is called with (project, parsed
    tree, path, log) once the tracks are in - see print_tracks - so tracks
    that only REAPER can play are printed before anything else is done.
    `index`, when given, is the scanned plug-in list, which is what decides
    whether a CLAP crosses over as the VST build of the same plug-in rather
    than its track being printed."""
    log = log if log is not None else []
    root = parse(path)
    pr = root.blocks[0] if root.blocks else Block('REAPER_PROJECT', [])
    p = Project()
    p.name = os.path.splitext(os.path.basename(path))[0]
    p.srcdir = os.path.dirname(os.path.abspath(path))
    sr = pr.get('SAMPLERATE')
    if sr:
        p.samplerate = _i(sr[0], 48000)
    # header settings a print project has to repeat for an item to play
    # the same there as here (render_reaper): the default stretch mode
    p.header_keep = []
    # everything in the header that can change how an item sounds: the
    # default stretch/pitch modes, the sample rate and its flags, the
    # tempo and play rate, the timebase locks, item mix behaviour, the
    # pan law. A print with only DEFPITCHMODE repeated rendered a pitch-
    # shifted take differently from the project (null -5 dB, REAPER
    # against itself -95 dB)
    for key in ('DEFPITCHMODE', 'SAMPLERATE', 'TEMPO', 'PLAYRATE',
                'TIMELOCKMODE', 'TEMPOENVLOCKMODE', 'ITEMMIX', 'AUTOXFADE',
                'PANLAW', 'PANMODE', 'PANLAWFLAGS', 'FEEDBACK', 'PEAKGAIN',
                'GROUPOVERRIDE', 'MAXPROJLEN', 'PROJOFFS', 'TIMEMODE',
                'VIDEO_CONFIG', 'MIXERUIFLAGS', 'RECMODE', 'SMPTESYNC'):
        v = pr.get(key)
        if v is not None:
            p.header_keep.append(' '.join([key] + list(v)))
    pl = pr.get('PANLAW')
    if pl:
        p.panlaw = _f(pl[0], 1.0)
    # the project offset (timeline starting before bar 1, a count-in): it
    # moves no audio, but bar numbers and the musical time ARA plug-ins are
    # given count from it - Melodyne put edited notes a sample apart with
    # and without it (2026-10-01) - so it goes to Cubase's Project Setup >
    # Start, which is the same thing
    # Opt-in (CPR_ON_PROJOFFS=1): Cubase's Melodyne rendered a REAPER
    # document with Start at -8 s further from REAPER's render (kaval -52 ->
    # -24 dB null) than with Start 0, so by default the offset stays behind
    po = pr.get('PROJOFFS')
    if po and os.environ.get('CPR_ON_PROJOFFS'):
        p.start = _f(po[0], 0.0)
    pm = pr.get('PANMODE')
    if pm:
        p.panmode = _i(pm[0], 3)
    p.pan_law_of = 'reaper'
    tempo = pr.get('TEMPO')
    if tempo:
        p.tempo = [(0.0, _f(tempo[0], 120.0))]
        if len(tempo) >= 3:
            p.tsig = (_i(tempo[1], 4), _i(tempo[2], 4))
    env = pr.child('TEMPOENVEX')
    if env:
        pts = [(_f(a[0]), _f(a[1])) for a in env.all('PT') if len(a) >= 2]
        if pts:
            p.tempo = pts

    # markers: a region is two lines sharing an index
    pending = {}
    for a in pr.all('MARKER'):
        if len(a) < 3:
            continue
        mid, pos, name = a[0], _f(a[1]), a[2]
        # the fourth field is a bag of flags, not a yes/no: bit 0 marks a
        # region, and the others carry things like whether it is in the
        # render matrix. Testing for exactly "1" turned a region whose
        # flags read 9 into two loose markers.
        isrgn = len(a) > 3 and (_i(a[3], 0) & 1) == 1
        if not isrgn:
            m = Marker(name, pos)
            m.rid = _i(mid, 0) or None
            p.markers.append(m)
        elif mid in pending:
            m = pending.pop(mid)
            m.end = pos
            p.markers.append(m)
        else:
            m = Marker(name, pos)
            m.rid = _i(mid, 0) or None
            pending[mid] = m
    for m in pending.values():
        p.markers.append(m)

    # REAPER's master track is Cubase's output bus. It is not one of the
    # TRACK blocks - its level, its effects and its automation are written
    # beside them - so it is read into p.master and the Cubase side turns it
    # back into an output bus rather than losing it.
    mv = pr.get('MASTER_VOLUME')
    mfx = pr.child('MASTERFXLIST')
    menv = pr.child('MASTERVOLENV2') or pr.child('MASTERVOLENV')
    if mv or mfx or menv:
        m = Track('Stereo Out', 0)
        m.kind = 'other'
        if mv and len(mv) >= 2:
            m.vol, m.pan = _f(mv[0], 1.0), _f(mv[1], 0.0)
        if mfx is not None:
            m.fx = read_fx(mfx, log, index)
        if menv is not None:
            m.volenv = env_pts(menv, 'volume')
        _fold_trim(m, _i(pr.get('MASTERAUTOMODE', ['0'])[0], 0))
        if m.fx or m.volenv or m.vol != 1.0 or m.pan != 0.0:
            p.master = m

    depth = 0
    aux = []          # (dest index, src index, Send)
    for ti, tb in enumerate(pr.children('TRACK')):
        name = tb.get('NAME', [''])[0]
        t = Track(name, depth)
        vp = tb.get('VOLPAN')
        if vp and len(vp) >= 2:
            t.vol, t.pan = _f(vp[0], 1.0), _f(vp[1], 0.0)
        po = tb.get('PLAYOFFS')
        if po:
            # media playback offset: value, flags. Measured on renders
            # (2026-09-29): flag 1 = off (REAPER's default '0 1'), flag 2 =
            # the value is in samples, otherwise seconds; negative = earlier
            v, fl = _f(po[0], 0.0), (_i(po[1], 1) if len(po) > 1 else 0)
            if not (fl & 1) and v:
                t.delay = (v / float(p.samplerate or 48000)) if (fl & 2) else v
        pm = tb.get('PANMODE')
        if pm:
            # a track's own pan mode; -1 means the project's
            v = _i(pm[0], -1)
            t.panmode = v if v >= 0 else None
        ms = tb.get('MUTESOLO')
        if ms:
            t.mute = _i(ms[0])
            t.solo = _i(ms[1]) if len(ms) > 1 else 0
        pc = tb.get('PEAKCOL')
        if pc:
            t.color = rgb_from_peakcol(_i(pc[0]))
        isbus = tb.get('ISBUS', ['0', '0'])
        t.is_folder = isbus[0] == '1'
        # Master/parent send: off, the track feeds only what it sends to
        # (a group, the way a Cubase channel outputs into one)
        mainsend = tb.get('MAINSEND')
        t.main_send = not (mainsend and _i(mainsend[0], 1) == 0)
        t.raw = tb
        for a in tb.all('AUXRECV'):
            if len(a) >= 4:
                s = Send(dest=ti, vol=_f(a[2], 1.0), pan=_f(a[3], 0.0),
                         mode=_i(a[1]))
                # AUXRECV src mode vol pan MUTE mono phase ...: a muted
                # send sends nothing (Banatul Dance's PIANO -> REVERB was
                # muted, and played unmuted it put 1.9 dB on the reverb)
                s.mute = len(a) > 4 and _i(a[4], 0) == 1
                aux.append((ti, _i(a[0]), s))
        ve = tb.child('VOLENV2')
        pe = tb.child('PANENV') or tb.child('PANENV2')
        # an envelope switched off (ACT 0) plays nothing: kept as a lane
        # that does not play (volenv_idle / panenv_idle), with the fader
        # carrying the level, as Cubase does with automation Read off
        ve_on = ve is not None and _i((ve.get('ACT') or ['1'])[0], 1)
        pe_on = pe is not None and _i((pe.get('ACT') or ['1'])[0], 1)
        if ve is not None:
            if ve_on:
                t.volenv = env_pts(ve, 'volume')
            else:
                t.volenv_idle = [(s, g * t.vol) for s, g in env_pts(ve, 'volume')]
        if pe is not None:
            # a REAPER pan envelope runs the other way to the pan knob: a
            # point at +1 is hard left (rendered, 2026-10-01); the model
            # holds the knob's sense (-1 left), as VOLPAN does
            if pe_on:
                t.panenv = pan_env_pts(pe)
            else:
                t.panenv_idle = [(s, max(-1.0, min(1.0, v + t.pan)))
                                 for s, v in pan_env_pts(pe)]
        _fold_trim(t, _i(tb.get('AUTOMODE', ['0'])[0], 0))
        chain = tb.child('FXCHAIN')
        if chain:
            t.fx = read_fx(chain, log, index)
            finish_fx(t, p, log)
            for k, fx in enumerate(t.fx):
                if getattr(fx, 'is_instrument', False):
                    t.instrument = t.fx.pop(k)
                    break
        read_lanes(t, tb)
        for ib in tb.children('ITEM'):
            t.items.extend(read_item(ib, t, log, index))
        finish_lanes(t, log)
        if any(i.kind == 'midi' for i in t.items):
            t.kind = 'midi'
        elif t.items and all(i.kind == 'video' for i in t.items):
            t.kind = 'video'
        else:
            t.kind = 'audio'
        if not t.name and not t.is_folder:
            # REAPER allows a nameless track; writing an empty string where
            # Cubase expects a name produced a record it displayed as
            # garbage, so an unnamed track is called what Cubase would call
            # it. Done here so a comparison of the two projects agrees too.
            base = ('Instrument' if t.instrument is not None else
                    'MIDI' if t.kind == 'midi' else 'Audio')
            t.name = '%s %02d' % (base, len(p.tracks) + 1)
        p.tracks.append(t)
        # REAPER records folder nesting as a delta on the last child
        if t.is_folder:
            depth += 1
        else:
            depth += min(0, _i(isbus[1] if len(isbus) > 1 else 0))
        depth = max(0, depth)

    for dest, src, s in aux:
        if 0 <= src < len(p.tracks):
            s.dest = dest
            p.tracks[src].sends.append(s)
    note_clap(p, log)
    note_empty(p, log)
    read_ara(p, pr, log)
    if printer is not None:
        printer(p, root, path, log)
    if not os.environ.get('CPR_NO_REHOST'):
        from . import rehost
        rehost.apply(p, log, index if index is not None else P.PluginIndex())
    expand_subprojects(p, log, _depth, printer, index)
    return p


def expand_subprojects(p, log, depth=0, printer=None, index=None):
    """Every subproject item becomes a folder holding the subproject's
    tracks, placed where the item was.

    REAPER plays a subproject as one item: the whole .rpp rendered, cut to
    the item's span, starting `soffs` seconds in. Cubase has nothing like
    it, and the nearest thing that keeps all the content editable is a
    summing folder at the item's position with every track of the
    subproject inside it, each event shifted by the item's position and cut
    to the item's bounds, and the subproject's master effects and level on
    the folder. The subproject's own tempo is not followed: its events are
    placed in seconds."""
    from . import media as _media
    if depth > 3:
        return
    out = []
    moved = {}
    inserted = 0
    for old_i, t in enumerate(p.tracks):
        moved[old_i] = len(out)
        subs = [i for i in t.items if i.kind == 'rpp']
        t.items = [i for i in t.items if i.kind != 'rpp']
        # a track that only held the subproject item has nothing left to
        # say: the folder takes its place (and its effects, if any)
        hollow = (subs and not t.items and not t.instrument
                  and not t.is_folder and not t.sends)
        if not hollow:
            out.append(t)
        else:
            moved[old_i] = len(out)     # sends to it land on the folder
        for it in subs:
            path = _media.resolve(it.file, p.srcdir) if it.file else None
            if not path or not os.path.isfile(path):
                alt = _media.locate(it.file or '', [p.srcdir]) if it.file else None
                if alt:
                    path = alt
                else:
                    log.append('%r: subproject %r is not there; its item was left '
                               'out' % (t.name, it.file))
                    continue
            try:
                sub = read(path, log, _depth=depth + 1, printer=printer,
                           index=index)
            except Exception as e:
                log.append('%r: subproject %r could not be read (%s)'
                           % (t.name, os.path.basename(path), e))
                continue
            name = it.name or os.path.splitext(os.path.basename(path))[0]
            if name.lower().endswith('.rpp'):
                name = name[:-4]
            folder = Track(name, t.depth)
            folder.is_folder = True
            folder.kind = 'other'
            folder.color = t.color
            if sub.master is not None:
                folder.fx = list(sub.master.fx)
                folder.vol = sub.master.vol
                folder.pan = sub.master.pan
            if hollow:
                folder.fx = list(folder.fx) + list(t.fx)
                folder.vol *= t.vol
                folder.volenv = list(t.volenv)
            folder.vol *= (1.0 if it.gain is None else it.gain)
            folder.mute = it.mute
            out.append(folder)
            base_index = len(out)
            start, end = it.pos, it.pos + it.length
            shift = it.pos - it.soffs
            kept = 0
            for st in sub.tracks:
                st.depth += t.depth + 1
                items = []
                for si in st.items:
                    if si.kind in ('audio', 'video', 'rpp'):
                        si.file = _media.resolve(si.file, sub.srcdir) if si.file else si.file
                    a, b = si.pos + shift, si.pos + shift + si.length
                    if b <= start or a >= end:
                        continue
                    if a < start:
                        cut = start - a
                        si.soffs += cut * (si.playrate or 1.0)
                        si.length -= cut
                        a = start
                        if si.kind == 'midi':
                            # ticks per second at the tempo the notes were
                            # written against
                            tps = si.ppq * (p.tempo[0][1] / 60.0)
                            si.notes = [(n[0] - cut * tps,) + tuple(n[1:])
                                        for n in si.notes
                                        if n[0] - cut * tps + n[1] > 0]
                    if b > end:
                        si.length -= (b - end)
                    si.pos = a
                    items.append(si)
                    kept += 1
                st.items = items
                for s in st.sends:
                    if s.dest is not None:
                        s.dest += base_index
                        s._sub = True
                out.append(st)
            inserted += 1
            log.append('%r: subproject %r became the folder %r with %d track(s) '
                       'and %d event(s) inside it, cut to the item'
                       % (t.name, os.path.basename(path), name, len(sub.tracks), kept))
    if inserted:
        p.tracks = out
        # the host's sends still name tracks by their old positions; the
        # subprojects' were re-pointed as they were placed
        for t in p.tracks:
            for s in t.sends:
                if s.dest is not None and not getattr(s, '_sub', False):
                    s.dest = moved.get(s.dest, s.dest)


def read_lanes(t, tb):
    """A track's fixed item lanes, REAPER's comping mechanism.

    A lane is an alternative for the same stretch of time with one of them
    playing, which is what a Cubase track version is, so they are carried on
    the model's lane fields either way round. `ITEMLANES` says how many,
    `LANENAME` names them and `LANESOLO` is a bitmask of the ones that
    play - the lowest set bit is the one taken as active, since a model
    track has a single active lane."""
    n = _i((tb.get('ITEMLANES') or ['0'])[0])
    if n <= 1:
        return
    names = tb.get('LANENAME') or []
    t.lane_names = [names[i] if i < len(names) else 'Lane %d' % (i + 1)
                    for i in range(n)]
    solo = _i((tb.get('LANESOLO') or ['1'])[0], 1)
    t.lanes_playing = 0
    for i in range(n):
        if solo & (1 << i):
            if not t.lanes_playing:
                t.active_lane = i
            t.lanes_playing += 1


def item_lane(ib, n_lanes):
    """Which lane an item sits in.

    REAPER puts an item in a lane by where it is drawn: YPOS is the top of
    the item as a fraction of the track's height, and with N lanes each one
    is 1/N tall. Round rather than truncate, so a height that does not
    divide exactly still lands on the intended lane."""
    y = ib.get('YPOS')
    if not y or n_lanes <= 1:
        return 0
    try:
        lane = int(round(_f(y[0]) * n_lanes))
    except Exception:
        return 0
    return max(0, min(n_lanes - 1, lane))


def finish_lanes(t, log):
    """Reconcile lanes and takes once every item on the track is read."""
    need = max([i.lane for i in t.items] + [0]) + 1
    if len(t.lane_names) < need:
        t.lane_names = (list(t.lane_names)
                        + ['Lane %d' % (i + 1)
                           for i in range(len(t.lane_names), need)])
    if len(t.lane_names) > 1 and t.active_lane >= len(t.lane_names):
        t.active_lane = 0
    if t.lanes_playing > 1:
        log.append('%r: %d lanes play at once in REAPER; a Cubase track plays '
                   'one version at a time, so lane %r is the active version '
                   'and the others are versions you can switch to'
                   % (t.name, t.lanes_playing, t.lane_names[t.active_lane]))


def split_takes(ib):
    """An ITEM's takes, each as the lines belonging to it.

    Takes are written one after another inside the item, separated by a
    TAKE line; everything before the first separator belongs to the first
    take. `TAKE SEL` marks the take that follows as the one REAPER plays.
    Returns (list of take blocks, index of the active one)."""
    head = Block(ib.name, ib.args)
    takes = [Block('TAKE', [])]
    active = 0
    seen_source = False
    for entry in ib.raw:
        if isinstance(entry, Block):
            if entry.name == 'SOURCE':
                seen_source = True
            takes[-1].blocks.append(entry)
            continue
        tok = split_tokens(entry) if entry else []
        if not tok:
            continue
        if tok[0] == 'TAKE':
            if 'SEL' in tok[1:]:
                active = len(takes)
            takes.append(Block('TAKE', tok[1:]))
            continue
        # anything before the first source describes the item, not the take
        target = takes[-1] if seen_source or len(takes) > 1 else head
        target.lines.append((tok[0], tok[1:]))
        if target is head:
            takes[0].lines.append((tok[0], tok[1:]))
    takes = [tk for tk in takes if tk.lines or tk.blocks]
    if active >= len(takes):
        active = 0
    return head, takes, active


_take_groups = [0]


def fade_length(toks):
    """The seconds a FADEIN/FADEOUT line actually fades over: the
    auto-crossfade length (third field) when it is set, else the length."""
    auto = _f(toks[2]) if len(toks) > 2 else 0.0
    return auto if auto > 0 else _f(toks[1])


def fade_shape(toks):
    """(shape 0..6, curve -1..1) of a FADEIN/FADEOUT line."""
    shape = int(_f(toks[0])) if toks else 0
    curve = _f(toks[5]) if len(toks) > 5 else 0.0
    return shape, curve


def read_item(ib, track, log, index=None):
    """One REAPER item as one or more model items, one per take.

    A take is an alternative recording of the same passage with one of them
    playing. Every take is kept, on the lane the item sits in, and the model
    remembers which takes belong together and which one plays - on the
    Cubase side they become events stacked on one track, the audible one on
    top, which is exactly what Cubase's lanes show."""
    head, takes, active = split_takes(ib)
    lanes = len(track.lane_names)
    base_lane = item_lane(ib, lanes)
    if len(takes) <= 1:
        it = read_take(ib, log, index)
        it.lane = base_lane
        return [it]
    _take_groups[0] += 1
    out = []
    for k, tk in enumerate(takes):
        it = read_take(merge_take(head, tk), log, index)
        it.lane = base_lane
        it.take_group = _take_groups[0]
        it.take_no = k
        it.take_sel = (k == active)
        out.append(it)
    return out


def merge_take(head, take):
    """The item's own lines with one take's lines on top of them.

    The take's come first because get() returns the first match: the item
    carries the first take's name and offset among its own lines, and a
    later take has to override them rather than be overridden."""
    b = Block(head.name, head.args)
    b.lines = list(take.lines) + list(head.lines)
    b.blocks = list(take.blocks)
    return b


def read_ara(p, pr, log):
    """Melodyne on takes: the project's document and each take's ID in it.

    REAPER keeps one Melodyne document per project (<ARA ...> <BIN>) and,
    per take, the string that names its audio source there (<ARASRC>: take
    GUID, ID). A take whose only plug-in is Melodyne then crosses as
    Cubase's own Melodyne extension (ara.py) instead of being printed."""
    blk = pr.child('ARA')
    src = pr.child('ARASRC')
    if blk is None or src is None:
        return
    from . import ara
    import base64
    b = blk.child('BIN')
    try:
        data = base64.b64decode(''.join(x.strip() for x in (b.raw if b else [])
                                        if isinstance(x, str)))
        _h, raw = ara.unpack(data)
        ara.Doc(raw)
    except Exception as e:
        log.append('the Melodyne document could not be read (%s): takes with '
                   'Melodyne are printed instead' % e)
        return
    ids = {}
    for x in src.raw:
        if isinstance(x, str):
            tok = split_tokens(x.strip())
            if len(tok) >= 2:
                ids[tok[0].upper()] = tok[1]
    n = 0
    for t in p.tracks:
        for it in t.items:
            g = (getattr(it, 'take_guid', None) or '').upper()
            if g not in ids or not getattr(it, 'takefx', None):
                continue
            it.ara_id = ids[g]
            mel = [f for f in it.takefx_fx if 'melodyne' in (f.name or '').lower()]
            if mel and len(mel) == len(it.takefx_fx):
                # Melodyne was the take's only plug-in: nothing to print
                it.takefx = None
                it.takefx_fx = []
            n += 1
    if n:
        p.ara_docs = [raw]
        p.ara_from = 'reaper'


def read_take(ib, log, index=None):
    it = Item()
    it.take_guid = (ib.get('GUID') or [None])[0]
    it.pos = _f((ib.get('POSITION') or ['0'])[0])
    it.length = _f((ib.get('LENGTH') or ['0'])[0])
    so = ib.get('SOFFS') or ['0']
    it.soffs = _f(so[0])
    # REAPER writes a MIDI item's start offset twice: seconds, then the same
    # offset in quarter notes ('SOFFS 5.763 11.526' at 120bpm). The second
    # field is what the notes are measured in, so no tempo maths is needed
    # to find where the item's window onto its source begins.
    if len(so) > 1:
        it.soffs_qn = _f(so[1])
    it.name = (ib.get('NAME') or [''])[0]
    # the item's own colour ('COLOR <native rgb | 0x1000000> B'), the
    # take's (TAKECOLOR) over it when a take has one
    for key in ('COLOR', 'TAKECOLOR'):
        c = ib.get(key)
        if c:
            rgb = rgb_from_peakcol(_i(c[0]))
            if rgb:
                it.color = rgb
    # a stretched item plays its source faster or slower than the timeline,
    # which is what a Cubase musical-mode clip does
    pr = ib.get('PLAYRATE')
    if pr:
        it.playrate = _f(pr[0], 1.0) or 1.0
        it.warped = abs(it.playrate - 1.0) > 1e-6
        if len(pr) > 1:
            it.preserve_pitch = bool(_i(pr[1], 1))
        if len(pr) > 2:
            it.pitch = _f(pr[2], 0.0)
    # stretch markers: 'SM src pos + src pos ...', each pair a source
    # second mapped onto an item second. REAPER warps the audio between
    # them - a rendered brass take squeezed at its tail - and nothing
    # but REAPER plays that, so such a take is printed (media.render)
    sm = ib.get('SM')
    if sm:
        toks = [t for t in sm if t != '+']
        pairs = []
        for i in range(0, len(toks) - 1, 2):
            try:
                pairs.append((float(toks[i]), float(toks[i + 1])))
            except ValueError:
                break
        it.stretch_markers = pairs
    mu = ib.get('MUTE')
    it.mute = _i(mu[0]) if mu else 0
    lp = ib.get('LOOP')
    it.loop = bool(_i(lp[0], 0)) if lp else False
    # a take of a multi-take item keeps its own gain as TAKEVOLPAN, whose
    # fields are pan, volume, pan law - the item's VOLPAN is volume first
    vp = ib.get('VOLPAN')
    if vp:
        # item volume, take pan, take volume, pan law: both volumes play,
        # so the event's gain is their product (a take at 1.3236 with the
        # item at 1 was 2.43 dB quiet in the export comparison)
        it.gain = _f(vp[0], 1.0)
        if len(vp) > 2:
            it.gain *= _f(vp[2], 1.0)
    tvp = ib.get('TAKEVOLPAN')
    if tvp and len(tvp) > 1:
        it.gain = _f(tvp[1], 1.0)
    # FADEIN/FADEOUT: shape, length, auto-crossfade length, flag, ?, curve, ?
    # (measured on renders, 2026-09-28). When REAPER auto-crossfades two
    # overlapping items it leaves the length field at its 1 ms default and
    # puts the real overlap in the third field, which then IS the fade -
    # a 93 ms crossfade used to arrive as a 1 ms one.
    fi = ib.get('FADEIN')
    if fi and len(fi) > 1:
        it.fadein = fade_length(fi)
        it.fade_lines['FADEIN'] = list(fi)
    fo = ib.get('FADEOUT')
    if fo and len(fo) > 1:
        it.fadeout = fade_length(fo)
        it.fade_lines['FADEOUT'] = list(fo)
    # a pan curve drawn inside the item, which Cubase cannot hold
    pe = ib.child('PANENV') or ib.child('PANENV2')
    if pe is not None:
        it.panenv = pan_env_pts(pe)
        if not _i((pe.get('ACT') or ['1'])[0], 1):
            it.panenv = []          # drawn but switched off
    pe2 = ib.child('PITCHENV')
    if pe2 is not None and _i((pe2.get('ACT') or ['1'])[0], 1):
        it.pitchenv = env_pts(pe2)
    # a volume curve drawn inside the item (a take volume envelope); the
    # points are linear gain, and REAPER only applies it while ACT is set
    ve = ib.child('VOLENV') or ib.child('VOLENV2')
    if ve is not None and _i((ve.get('ACT') or ['1'])[0], 1):
        # VOLTYPE 1 (fader scaling) is handled in env_pts: the ramps are
        # re-drawn in gain there, shapes included
        it.volenv = env_pts(ve, 'volume')
    # A take envelope's times run in the take's own time, which moves at the
    # item's playrate - REAPER stretches the envelope with the audio. The
    # model holds seconds into the item, as it plays (measured 2026-09-29:
    # a pitch step written at 7.9575 on a 1.0769x item sounded at 7.389).
    rate = it.playrate or 1.0
    if abs(rate - 1.0) > 1e-9:
        for k in ('panenv', 'pitchenv', 'volenv'):
            env = getattr(it, k, None)
            if env:
                setattr(it, k, [(t / rate, v) for t, v in env])
    # a plug-in chain inside the item: Cubase has nothing like it, so the
    # audio is printed through REAPER on the way across (render_reaper)
    fx = ib.children('TAKEFX')
    if fx:
        it.takefx = fx
        for blk in fx:
            it.takefx_fx.extend(read_fx(blk, log, index))
    src = ib.child('SOURCE')
    while src is not None and src.args and src.args[0] == 'SECTION':
        # A section of the source, or the source reversed. MODE 3 was
        # measured against REAPER's own render (Cherry Link, 2026-09-28):
        # the whole file played backwards, SOFFS counted from its end,
        # nulling at -43 dB; the LENGTH written there is not a limit. Kept
        # on the item, which is then printed or rendered - Cubase has no
        # reversed event - and emitted as is when REAPER does the print.
        sec = {'startpos': _f((src.get('STARTPOS') or ['0'])[0]),
               'length': _f((src.get('LENGTH') or ['0'])[0]),
               'mode': _i((src.get('MODE') or ['0'])[0], 0),
               'overlap': _f((src.get('OVERLAP') or ['0.01'])[0])}
        sec['reverse'] = bool(sec['mode'] & 2)
        it.section = sec
        src = src.child('SOURCE')
    if src is None:
        # An item with no take: REAPER allows one, and it is how a chord
        # chart is written - a bar-long empty item per chord with the name
        # in its <NOTES>. It holds no audio and no notes, so writing it as
        # an audio event would put whatever the donor's event played
        # wherever the chart sits. Marked for what it is and left out.
        it.kind = 'empty'
        if not it.name:
            nb = ib.child('NOTES')
            if nb is not None:
                text = ' '.join(x.strip().lstrip('|').strip()
                                for x in nb.raw if isinstance(x, str))
                it.name = text.strip()
        return it
    kind = src.args[0] if src.args else ''
    if kind == 'RPP_PROJECT':
        # a REAPER subproject: another .rpp played as one item
        it.kind = 'rpp'
        f = src.get('FILE')
        it.file = f[0] if f else None
        return it
    if kind == 'MIDI':
        it.kind = 'midi'
        hd = src.get('HASDATA')
        ppq = _f(hd[1], 960.0) if hd and len(hd) > 1 else 960.0
        it.ppq = ppq
        it.notes, it.ccs, it.ticks = read_midi(src, ppq)
    else:
        if kind == 'VIDEO':
            it.kind = 'video'
        f = src.get('FILE')
        it.file = f[0] if f else None
        # what follows the path: REAPER writes `FILE "x.mp3" 1` for an MP3
        # read gapless (the LAME delay trimmed - 2257 samples at 44.1 kHz
        # against the plain decode); a print without it sat 51 ms late
        it.file_args = list(f[1:]) if f else []
    return it


def read_midi(src, ppq):
    """REAPER MIDI events -> (note list, source length in ticks)."""
    t = 0
    on = {}
    notes = []
    ccs = []
    for k, a in src.lines:
        # 'Em'/'em' is a muted event: its note plays nothing, and arrives as
        # a velocity-0 note, which Cubase plays as silence (measured). Its
        # time still counts towards the events after it
        if k not in ('E', 'e', 'Em', 'em') or len(a) < 4:
            continue
        muted = k in ('Em', 'em')
        try:
            dt = int(a[0])
            st = int(a[1], 16)
            d1 = int(a[2], 16)
            d2 = int(a[3], 16)
        except ValueError:
            continue
        t += dt
        ch = st & 0x0F
        if muted and (st & 0xF0) not in (0x80, 0x90):
            continue
        if (st & 0xF0) == 0x90 and d2 > 0:
            on.setdefault((ch, d1), []).append((t, 0 if muted else d2))
        elif (st & 0xF0) in (0x80, 0x90):
            k2 = (ch, d1)
            if on.get(k2):
                st_t, vel = on[k2].pop(0)
                # the note-off's own velocity rides along as a sixth
                # field: a release sample plays by it, and Cubase sends
                # whatever the part holds (0, where it was not written)
                notes.append((st_t, max(1, t - st_t), ch, d1, vel, d2))
        elif (st & 0xF0) in (0xA0, 0xB0, 0xC0, 0xD0, 0xE0):
            ccs.append((t, st, d1, d2))
    for k2, lst in on.items():
        for st_t, vel in lst:
            notes.append((st_t, max(1, t - st_t), k2[0], k2[1], vel, 64))
    # REAPER closes every MIDI source with All Notes Off (CC 123, value 0)
    # at its very end; that is the source's end marker, not music, and
    # Cubase has nothing to put it on
    ccs = [c for c in ccs
           if not ((c[1] & 0xF0) == 0xB0 and c[2] == 123 and c[3] == 0 and c[0] >= t)]
    notes.sort()
    return notes, ccs, t
