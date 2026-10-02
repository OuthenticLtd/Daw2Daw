"""Print tracks whose plug-ins exist only in REAPER.

REAPER's own effects and instruments (ReaVerb, ReaEQ, ReaComp, ReaSynth
and the rest of the Rea* family), JS effects and AU plug-ins have no Cubase
counterpart at all, and so does a CLAP whose plug-in ships in no other
format (rpp_read.clap_as_vst carries the rest over as their VST build).
Leaving them out changes the sound; the only faithful way across is to
print what the track plays, and REAPER does the printing so the result is
exactly what the REAPER project sounds like.

A print is a flattening, though, and one thing it flattens is the music
itself: a printed instrument track arrives in Cubase as a single audio
event, and its MIDI, its lanes and its playable instrument are gone with
no way back. So a track whose synth Cubase *can* load is not printed for
the sake of an insert effect it cannot - the notes are kept and the effect
is left out and named. Only a track whose instrument itself has no Cubase
counterpart is printed, that being the one case where the notes cannot
sound in Cubase at all. CPR_PRINT_OVER_MIDI=1 prints those tracks anyway.

For every such track a throwaway copy of the project is written in which
that track keeps its items, its receives, its folder children and its
plug-in chain up to and including the last REAPER-only plug-in, has its
fader, pan and mute reset (Cubase keeps those on the track), and is
selected; REAPER renders the selected tracks' stems from the command line
(reaper -renderproject, RENDER_STEMS 3). The stem, covering the whole
project plus a tail for reverbs and delays, becomes the track's one audio
event in Cubase; plug-ins that sat after the last REAPER-only one stay as
inserts. A folder that was printed loses its children (their audio is in
the stem) and a printed track loses its incoming sends (their audio is in
the stem too).

The same module, in dry mode, tells verify.py what the builder did, so the
REAPER project can be compared with the Cubase one as it was actually
written.
"""
import hashlib
import json
import os
import re
import subprocess
import time

from . import media
from .model import Item, Fx
from .render_reaper import (block_text, q, fmt, RENDER_WAV24, _tok,
                            render_cfg, clipped)

TAG = ' [printed track]'
TAIL = float(os.environ.get('CPR_PRINT_TAIL', '5'))
# post-fader curves stay on the Cubase track, so the print must not have them
STRIP = ('VOLENV2', 'PANENV', 'PANENV2', 'WIDTHENV', 'WIDTHENV2', 'MUTEENV')


def active_native(fx):
    return bool(getattr(fx, 'native', False)) and not fx.bypass and not fx.offline


# Plug-ins measured to play differently under Cubase than under REAPER with
# the very same settings and the very same MIDI (the export comparison,
# 2026-09-18: Guitar Rig 7 +1.1 dB, Kontakt's bass patch nulling to -15 dB
# where its drums nulled to -66, Pianoteq -0.6 dB, Hive's oscillator
# phases). What they do is theirs, not the project file's, so a track that
# carries one is printed through it by REAPER and Cubase plays the print -
# the only way the two exports come out the same. The MIDI stays on a muted
# copy of the track, instrument and all, for editing later.
# CPR_HOST_DEPENDENT="a,b" replaces the list; CPR_HOST_DEPENDENT= (empty)
# switches this off.
# Effects on FX tracks (ValhallaDelay, Raum) are not on the list even
# though they do not null across hosts either: an FX track is routing -
# the sends into it, its own inserts - and that is what one tweaks later,
# so it stays a live channel fed by the (printed) sources, as in REAPER.
HOST_DEPENDENT = ('Guitar Rig 7', 'Kontakt 8', 'Pianoteq 9', 'Hive')


def host_dependent_names():
    v = os.environ.get('CPR_HOST_DEPENDENT')
    if v is None:
        return HOST_DEPENDENT
    return tuple(x.strip() for x in v.split(',') if x.strip())


def host_dependent(fx):
    """A plug-in that will not play the same in Cubase however faithfully
    its settings cross - printed through, like a REAPER-only one."""
    if fx.bypass or fx.offline or getattr(fx, 'native', False):
        return False
    name = (fx.name or '').lower()
    return any(name == h.lower() or name.startswith(h.lower())
               for h in host_dependent_names())


def must_print(fx):
    return active_native(fx) or host_dependent(fx)


def chain_of(t):
    """The track's plug-ins in REAPER's order, instrument included."""
    full = ([t.instrument] if t.instrument is not None else []) + list(t.fx)
    return sorted(full, key=lambda f: getattr(f, 'chain_pos', 0))


def keeps_midi(t):
    """True when printing this track would destroy music Cubase could play.

    A track carrying MIDI and an instrument Cubase can load is playable in
    Cubase as it stands. Printing it would leave one audio event where the
    notes, the lanes and the instrument were, so the REAPER-only inserts go
    instead - they colour the sound, the notes *are* the music."""
    if os.environ.get('CPR_PRINT_OVER_MIDI'):
        return False
    if t.instrument is None or getattr(t.instrument, 'native', False):
        return False                    # nothing in Cubase to play them
    return any(i.kind == 'midi' for i in t.items)


def plan(p):
    """-> [(track index, track, position of the last plug-in to print)]"""
    out = []
    for i, t in enumerate(p.tracks):
        full = chain_of(t)
        nat = [f for f in full if must_print(f)]
        if not nat:
            continue
        if keeps_midi(t) and not any(host_dependent(f) for f in nat):
            continue                    # handled by drop_natives, not here
        k = max(f.chain_pos for f in nat)
        if t.instrument is not None:
            # an instrument after the last printed effect still cannot feed
            # an audio event: everything up to it goes into the print
            k = max(k, t.instrument.chain_pos)
        out.append((i, t, k))
    return out


def spare_midi(p, log):
    """Strip the REAPER-only inserts off the tracks plan() spared.

    Reported plug-in by plug-in: the sound is not quite what REAPER made,
    and which effect is missing from which track is the one thing needed to
    put it back by hand."""
    n = 0
    for t in p.tracks:
        if not any(active_native(f) for f in chain_of(t)) or not keeps_midi(t):
            continue
        gone = drop_natives(t)
        if not gone:
            continue
        n += len(gone)
        log.append('%r kept its MIDI and its instrument %r rather than being '
                   'printed to audio; %d REAPER-only insert(s) it cannot take '
                   'to Cubase were left out: %s%s'
                   % (t.name, t.instrument.name if t.instrument else '?',
                      len(gone), describe(gone), gain_note(gone)))
    return n


def _hash_path(wav):
    return wav + '.print.json'


def _print_hash(wav):
    try:
        with open(_hash_path(wav), encoding='utf-8') as fh:
            return json.load(fh).get('hash')
    except (OSError, ValueError):
        return None


def _write_print_hash(wav, digest):
    try:
        with open(_hash_path(wav), 'w', encoding='utf-8') as fh:
            json.dump({'hash': digest}, fh)
    except OSError:
        pass


def safe_name(name):
    s = re.sub(r'[\\/:*?"<>|]+', '_', name or '').strip().rstrip('.')
    return s or 'Track'


def names_for(targets):
    names = {}
    taken = set()
    for i, t, _k in targets:
        base = safe_name(t.name)
        n = 0
        while True:
            n += 1
            cand = base + (TAG if n == 1 else TAG[:-1] + ' %d]' % n)
            if cand.lower() not in taken:
                break
        taken.add(cand.lower())
        names[i] = cand
    return names


def drop_natives(t):
    """Take the REAPER-only plug-ins off a track without printing."""
    gone = [f for f in t.fx if getattr(f, 'native', False)]
    t.fx = [f for f in t.fx if not getattr(f, 'native', False)]
    if t.instrument is not None and getattr(t.instrument, 'native', False):
        gone.append(t.instrument)
        t.instrument = None
    return gone


def describe(fxs):
    return ', '.join(f.name for f in fxs) or 'none'


JS_DIRS = (r'%APPDATA%\REAPER\Effects', r'%APPDATA%\REAPER\Scripts')
# a slider that is a plain output level, as opposed to one that shapes the
# sound and changes the level as a side effect ('Width Boost (dB)')
GAIN_SLIDER = re.compile(r'^(output\s+)?(gain|volume|level|trim)\b', re.I)
SLIDER_DECL = re.compile(r'^slider(\d+)\s*:.*?>(.*)$')


def js_block(fx):
    """The <JS ...> block of a REAPER-only insert, if it is a JS effect."""
    for e in (getattr(fx, 'raw_group', None) or []):
        if getattr(e, 'name', '') == 'JS':
            return e
    return None


def js_file(name):
    for root in JS_DIRS:
        p = os.path.join(os.path.expandvars(root), name.replace('/', os.sep))
        if os.path.isfile(p):
            return p
    return None


def js_gain_db(fx):
    """The output gain a dropped JS effect was applying, in dB, or None.

    A JS effect cannot go to Cubase, and dropping one silently changes the
    level whenever one of its sliders is an output gain: the bass here lost
    a Stillwell Stereo Width whose Gain slider sat at -3.1 dB and arrived
    3.1 dB louder for it. The slider's meaning is not in the project - it is
    declared in the effect's own source on disk ('slider3:0<-20,20>Gain
    (dB)') - so that is where it is read from, and only a slider that is
    plainly an output level is counted."""
    blk = js_block(fx)
    if blk is None or not blk.args:
        return None
    path = js_file(blk.args[0])
    if path is None:
        return None
    vals = None
    for line in blk.raw:
        if isinstance(line, str) and line.strip():
            vals = line.split()
            break
    if not vals:
        return None
    total = 0.0
    found = False
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except OSError:
        return None
    for line in text.split('\n'):
        m = SLIDER_DECL.match(line.strip())
        if not m:
            continue
        idx, label = int(m.group(1)), m.group(2).strip()
        if not GAIN_SLIDER.match(label) or '(db)' not in label.lower():
            continue
        if idx - 1 >= len(vals):
            continue
        try:
            v = float(vals[idx - 1])
        except ValueError:
            continue
        if abs(v) > 1e-9:
            total += v
            found = True
    return total if found else None


def gain_note(fxs):
    """'X dB of output gain went with them' for a set of dropped effects."""
    total = 0.0
    for f in fxs:
        g = js_gain_db(f)
        if g is not None:
            total += g
    if abs(total) < 0.05:
        return ''
    return (' - %s of that was output gain on its sliders, so the track '
            'arrives %.1f dB %s than REAPER played it; pull its fader '
            '%.1f dB %s to match'
            % ('%+.1f dB' % total, abs(total),
               'louder' if total < 0 else 'quieter',
               abs(total), 'down' if total < 0 else 'up'))


def project_end(p):
    end = 0.0
    for t in p.tracks:
        for it in t.items:
            end = max(end, it.pos + it.length)
    return end


# ------------------------------------------------------------ the throwaway
def _first_tok(line):
    s = line.strip()
    return s.split(None, 1)[0] if s else ''


SRCDIR = ['']

# Blocks left out of the throwaway project: ITEMs whose source file is not
# on disk. REAPER, asked to load a project that names a missing file, stops
# and asks where it went - "Replace missing file..." once per file, then a
# "Project Load Warning" - and a render started from the command line waits
# on that dialog for as long as the caller lets it (an hour, here). Nothing
# can be printed from a file that is not there anyway, so the item goes.
SKIP = set()


def _source_files(blk):
    """Every FILE a SOURCE block (or the SECTION wrapping one) names."""
    out = []
    if blk.name == 'SOURCE':
        for f in blk.all('FILE'):
            if f:
                out.append(f[0])
    for sub in blk.blocks:
        out.extend(_source_files(sub))
    return out


def missing_media(root, srcdir):
    """The ITEM blocks whose media is not on disk, and the files they name.

    Returns (set of id(ITEM block), sorted list of missing paths). A MIDI
    item has no file and is never missing; a subproject's .rpp counts as a
    file like any other, since REAPER asks for it the same way."""
    gone = set()
    names = set()

    def walk(blk):
        for b in blk.blocks:
            if b.name == 'ITEM':
                for f in _source_files(b):
                    p = media.resolve(f, srcdir)
                    if not os.path.isfile(p):
                        gone.add(id(b))
                        names.add(f)
            else:
                walk(b)
    walk(root)
    return gone, sorted(names)


def _abs_file(line):
    """A FILE line with a relative path, made absolute from the project.

    The path is the line's second token and not necessarily its last: an
    MP3 source writes `FILE "x.mp3" 1`. Taking everything after FILE as the
    path glued the quotes and the trailing 1 into the file name, REAPER
    could not find that and stopped to ask - one dialog per MP3 item, and a
    render started from the command line waited on the first of them."""
    s = line.strip()
    if not s.startswith('FILE'):
        return line
    from .rpp_read import split_tokens
    tok = split_tokens(s)
    if len(tok) < 2 or tok[0] != 'FILE':
        return line
    raw = tok[1]
    if raw and not os.path.isabs(raw) and SRCDIR[0]:
        raw = os.path.join(SRCDIR[0], raw)
    return ' '.join(['FILE', q(raw)] + [_tok(a) for a in tok[2:]])


def _emit(entry, indent):
    if isinstance(entry, str):
        return [indent + _abs_file(entry)]
    if id(entry) in SKIP:
        return []                       # its media is not on disk
    out = ['%s<%s%s' % (indent, entry.name,
                         ''.join(' ' + _tok(a) for a in entry.args))]
    for e in entry.raw:
        out += _emit(e, indent + '  ')
    out.append(indent + '>')
    return out


def chain_text(chain, printed, indent):
    out = [indent + '<FXCHAIN']
    for e in chain.raw:                     # window state, before the plug-ins
        if isinstance(e, str) and _first_tok(e) == 'BYPASS':
            break
        out += _emit(e, indent + '  ')
    for f in printed:
        for e in (getattr(f, 'raw_group', None) or []):
            out += _emit(e, indent + '  ')
    out.append(indent + '>')
    return out


def track_text(tb, info, indent):
    out = ['%s<TRACK%s' % (indent, ''.join(' ' + _tok(a) for a in tb.args))]
    ms = tb.get('MUTESOLO')
    mute = ms[0] if ms else '0'
    for e in tb.raw:
        if isinstance(e, str):
            key = _first_tok(e)
            if key in ('SEL', 'MUTESOLO'):
                continue
            if info and key in ('VOLPAN', 'NAME'):
                continue
            out.append(indent + '  ' + _abs_file(e))
            continue
        if info and e.name in STRIP:
            continue
        if info and e.name == 'FXCHAIN':
            out += chain_text(e, info['printed'], indent + '  ')
            continue
        out += _emit(e, indent + '  ')
    if info:
        out += [indent + '  NAME ' + q(info['name']),
                indent + '  VOLPAN 1 0 -1 -1 1',
                indent + '  MUTESOLO 0 0 0',
                indent + '  SEL 1']
    else:
        out += [indent + '  MUTESOLO %s 0 0' % mute, indent + '  SEL 0']
    out.append(indent + '>')
    return out


def project_text(root, infos, out_dir, end, rate, srcdir):
    SRCDIR[0] = srcdir
    pr = root.blocks[0]
    lines = ['<REAPER_PROJECT%s' % ''.join(' ' + _tok(a) for a in pr.args),
             '  RENDER_FILE %s' % q(out_dir),
             '  RENDER_PATTERN $track',
             '  RENDER_FMT 0 2 %d' % int(rate),
             '  RENDER_1X 0',
             '  RENDER_RANGE 3 0 0 0 0',
             '  RENDER_RESAMPLE 3 0 1',
             '  RENDER_ADDTOPROJ 0',
             '  RENDER_STEMS 3',
             '  RENDER_DITHER 0',
             '  <RENDER_CFG',
             '    ' + render_cfg(),
             '  >',
             '  MARKER 1 0 print 1 0 1 R',
             '  MARKER 1 %s "" 1' % fmt(end)]
    for e in pr.raw:
        if isinstance(e, str):
            key = _first_tok(e)
            if key.startswith('RENDER_') or key == 'MARKER':
                continue
            lines.append('  ' + _abs_file(e))
            continue
        if e.name == 'RENDER_CFG':
            continue
        if e.name == 'TRACK':
            lines += track_text(e, infos.get(id(e)), '  ')
            continue
        lines += _emit(e, '  ')
    lines.append('>')
    return lines


# ------------------------------------------------------------ the model
def apply(p, targets, results, log, printed_by):
    """Rewrite the tracks that were printed. `results` maps track index to
    (path, duration) for the prints that exist."""
    removed = set()
    replaced = set()
    spares = {}         # printed track index -> its muted MIDI copy
    for i, t, k in targets:
        if i in removed:
            continue                    # inside a folder that was printed
        full = chain_of(t)
        printed = [f for f in full if f.chain_pos <= k]
        keep = [f for f in full if f.chain_pos > k]
        got = results.get(i)
        if got is None:
            # No print: what REAPER alone could play is dropped and named,
            # and a plug-in that plays differently under Cubase stays as it
            # is - the notes and the patch cross, the sound is not the same.
            # Said as an instruction, since that is the one thing the owner
            # of the project can do about it before converting.
            dep = [f for f in full if host_dependent(f)]
            gone = drop_natives(t)
            parts = []
            if gone:
                parts.append('%d REAPER-only plug-in(s) were left out: %s%s'
                             % (len(gone), describe(gone), gain_note(gone)))
            if dep:
                parts.append('%s play(s) differently under Cubase with the '
                             'same settings (measured), so this track will '
                             'not sound identical' % describe(dep))
            log.append('%r could not be printed through REAPER (%s); %s. '
                       'For an identical result, freeze or render this '
                       'track in REAPER before converting'
                       % (t.name, printed_by, '; '.join(parts) or
                          'nothing was left out'))
            continue
        path, dur = got
        kids = p.folder_children(i)
        removed.update(kids)
        replaced.add(i)
        midi = [x for x in t.items if x.kind == 'midi']
        if midi and t.instrument is not None:
            # the notes and the instrument that played them, muted, right
            # below: the print is what sounds, this is what can be edited
            import copy
            m = copy.copy(t)
            m.name = '%s (MIDI)' % t.name
            m.items = midi
            m.fx = list(t.fx)
            m.sends = []
            m.mute = 1
            m.is_folder = False
            m.lane_names = list(getattr(t, 'lane_names', []) or [])
            spares[i] = m
        # The print runs from the project start to the end plus a tail, so
        # a release is never cut. The events Cubase shows are cut to where
        # the track's items were - one event per stretch of items, offset
        # into the print - so the track reads like the MIDI it came from
        # and the head or tail can be dragged out when wanted.
        spans = []
        for src in sorted((x for x in t.items if x.kind != 'empty'
                           and not x.mute and x.take_sel),
                          key=lambda x: x.pos):
            a, z = max(0.0, src.pos), min(dur, src.pos + src.length)
            if z <= a:
                continue
            if spans and a <= spans[-1][1] + 1e-6:
                spans[-1][1] = max(spans[-1][1], z)
            else:
                spans.append([a, z])
        if not spans:
            spans = [[0.0, dur]]
        events = []
        for a, z in spans:
            it = Item()
            it.kind = 'audio'
            it.pos = a
            it.length = z - a
            it.soffs = a
            it.file = path
            it.name = t.name
            it.lane = 0
            events.append(it)
        t.items = events
        t.kind = 'audio'
        t.instrument = None
        t.fx = keep
        t.is_folder = False
        t.lane_names = []
        t.active_lane = 0
        t.lanes_playing = 1
        t.versions = []
        what = [f.name for f in printed]
        why = [f for f in printed if active_native(f)]
        dep = [f for f in printed if host_dependent(f) and f not in why]
        reason = []
        if why:
            reason.append('%s exist(s) only in REAPER' % describe(why))
        if dep:
            reason.append('%s play(s) differently under Cubase with the same '
                          'settings (measured)' % describe(dep))
        log.append('%r: printed through REAPER into %r - %s - because %s; '
                   'the track is now that one audio event%s%s%s%s'
                   % (t.name, os.path.basename(path), ', '.join(what),
                      ' and '.join(reason) or 'it had to be',
                      ', its notes and instrument stay on a muted %r below it'
                      % ('%s (MIDI)' % t.name) if i in spares else '',
                      ', its %d insert(s) after them stay' % len(keep)
                      if keep else '',
                      ', and its %d folder child(ren) are inside the print'
                      % len(kids) if kids else '',
                      '' if not any(s.dest == i for u in p.tracks
                                    for s in u.sends)
                      else ', sends into it included'))
    if not removed and not replaced:
        return
    moved = {}
    out = []
    for old_i, t in enumerate(p.tracks):
        if old_i in removed:
            continue
        moved[old_i] = len(out)
        out.append(t)
        if old_i in spares:
            out.append(spares[old_i])
    p.tracks = out
    for t in p.tracks:
        t.sends = [s for s in t.sends
                   if s.dest is None or (s.dest not in removed
                                         and s.dest not in replaced)]
        for s in t.sends:
            if s.dest is not None:
                s.dest = moved.get(s.dest, s.dest)


def master_natives(p, log):
    if p.master is None:
        return
    gone = drop_natives(p.master)
    if gone:
        log.append('the master track carries %d REAPER-only plug-in(s) that '
                   'cannot be printed without flattening the whole mix; '
                   'they were left out: %s%s'
                   % (len(gone), describe(gone), gain_note(gone)))


def idle_natives(p, log):
    """Bypassed or offline REAPER-only plug-ins do nothing: just go."""
    n = 0
    for t in p.tracks:
        idle = [f for f in t.fx
                if getattr(f, 'native', False) and (f.bypass or f.offline)]
        if idle:
            t.fx = [f for f in t.fx if f not in idle]
            n += len(idle)
        if (t.instrument is not None and getattr(t.instrument, 'native', False)
                and (t.instrument.bypass or t.instrument.offline)):
            t.instrument = None
            n += 1
    if n:
        log.append('%d bypassed REAPER-only plug-in(s) were left out' % n)


# ------------------------------------------------------------ entry points
def printer(out_dir, dry=False):
    """A hook for rpp_read.read: prints (or, dry, predicts) the tracks that
    carry REAPER-only plug-ins, writing the WAVs into `out_dir`."""
    def run(p, root, path, log):
        master_natives(p, log)
        idle_natives(p, log)
        spare_midi(p, log)
        targets = plan(p)
        if not targets:
            return
        names = names_for(targets)
        results = {}
        if dry:
            for i, t, k in targets:
                f = os.path.join(out_dir, names[i] + '.wav')
                if os.path.isfile(f):
                    results[i] = (f, media.wav_duration(f))
            apply(p, targets, results, log, 'no print was found beside the '
                                             'Cubase project')
            return
        reaper = None if os.environ.get('CPR_NO_REAPER') else media.find_reaper()
        if not reaper:
            apply(p, targets, {}, log, 'reaper.exe was not found')
            return
        os.makedirs(out_dir, exist_ok=True)
        end = project_end(p) + TAIL
        srcdir = os.path.dirname(os.path.abspath(path))
        # REAPER stops to ask about every file it cannot find, and a render
        # started from the command line then waits on that dialog until the
        # caller gives up. Items whose media is missing are left out of the
        # throwaway project: nothing could have been printed from them.
        SKIP.clear()
        gone, missing = missing_media(root, srcdir)
        SKIP.update(gone)
        if missing:
            log.append('%d item(s) on %d missing file(s) were left out of the '
                       'REAPER print pass, since REAPER would otherwise stop '
                       'to ask where they are: %s'
                       % (len(gone), len(missing),
                          ', '.join(os.path.basename(m) for m in missing[:6])
                          + (' ...' if len(missing) > 6 else '')))
        infos = {}
        locked = []
        cached = []
        for i, t, k in targets:
            tb = getattr(t, 'raw', None)
            if tb is None:
                continue
            printed = [f for f in chain_of(t) if f.chain_pos <= k]
            info = {'name': names[i], 'printed': printed, 'index': i}
            f = os.path.join(out_dir, names[i] + '.wav')
            # A print is a function of the project text it was rendered
            # from - every track, the tempo, the media, the chain - so that
            # text's hash names it. The same hash beside an existing print
            # means the same audio: converting a project again renders
            # nothing REAPER has already rendered.
            text = '\n'.join(project_text(root, {id(tb): info}, out_dir, end,
                                          p.samplerate, srcdir))
            info['hash'] = hashlib.sha1(text.encode('utf-8')).hexdigest()
            if os.path.isfile(f) and _print_hash(f) == info['hash']:
                cached.append(names[i])
                continue
            if os.path.isfile(f):
                try:
                    os.remove(f)        # REAPER must not stop to ask
                except OSError:
                    # an earlier print, open in Cubase: REAPER could not
                    # write over it and would stop with an error box
                    locked.append(names[i])
                    continue
            infos[id(tb)] = info
        if locked:
            log.append('%d earlier print(s) are open in another program - the '
                       'Cubase project from the last run, probably - and could '
                       'not be replaced: %s. Close it and convert again.'
                       % (len(locked), ', '.join(locked)))
        if cached:
            log.append('%d print(s) were kept from the last run, nothing they '
                       'are made of having changed: %s'
                       % (len(cached), ', '.join(cached)))
        if not infos and not cached:
            apply(p, targets, {}, log, 'its earlier print is open elsewhere')
            return
        # A print is the track's output with its fader and pan reset, and
        # REAPER's sends are post-fader: a track that receives sends must
        # not be printed in the same pass as the tracks feeding it, or they
        # feed it at unity and a delay comes out 20 dB hot. Tracks that
        # feed nothing but the mix do not touch each other, so they all go
        # in one pass; each receiver gets a pass of its own with everything
        # else untouched. That is two or three REAPER launches for a
        # project, not one per track.
        receivers = set(sv.dest for u in p.tracks for sv in u.sends
                        if sv.dest is not None)
        passes = []
        shared = {k: v for k, v in infos.items() if v['index'] not in receivers}
        if shared:
            passes.append(shared)
        for k, v in infos.items():
            if v['index'] in receivers:
                passes.append({k: v})
        projs = []
        for n, batch in enumerate(passes):
            lines = project_text(root, batch, out_dir, end, p.samplerate, srcdir)
            proj = os.path.join(out_dir, '_print_pass_%02d.rpp' % n)
            with open(proj, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write('\n'.join(lines) + '\n')
            projs.append(proj)
            t0 = time.time()
            try:
                subprocess.run([reaper, '-nosplash', '-renderproject', proj],
                               timeout=3600)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.append('REAPER could not print %s: %s'
                           % (', '.join(v['name'] for v in batch.values()), e))
            print('  printed %d track(s) in %.0f s: %s'
                  % (len(batch), time.time() - t0,
                     ', '.join(v['name'] for v in batch.values())), flush=True)
        for info in infos.values():
            f = os.path.join(out_dir, info['name'] + '.wav')
            if os.path.isfile(f):
                _write_print_hash(f, info['hash'])
        hurt = []
        for i, t, k in targets:
            f = os.path.join(out_dir, names[i] + '.wav')
            if os.path.isfile(f):
                results[i] = (f, media.wav_duration(f))
                c = clipped(f)
                if c and c[0]:
                    hurt.append((t.name, c[0], c[1]))
        if hurt:
            log.append('%d print(s) came back with samples pinned at full '
                       'scale - the render clipped, and that is baked into '
                       'the audio Cubase will play: %s. A print is made with '
                       'the fader reset, so a track that sits well below 0 dB '
                       'in REAPER can be far above it before its fader. '
                       
                       'Prints are 32-bit float, which cannot clip, so CPR_PRINT_24BIT or '
                       'CPR_PRINT_CFG must be set'
                       % (len(hurt), ', '.join('%s (%d sample(s), peak %.2f)'
                                               % h for h in hurt[:4])))
        if not os.environ.get('CPR_KEEP_PRINT'):
            for proj in projs:
                try:
                    os.remove(proj)
                except OSError:
                    pass
        apply(p, targets, results, log, 'REAPER produced no file for it, or '
                                        'its earlier print is open elsewhere')
    return run
