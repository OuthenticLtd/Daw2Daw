"""Write a Cubase project (.cpr) by editing one in place.

A .cpr is not a format anything but Cubase writes. The container is fully
understood - a 16-byte header then ROOT/ARCH chunk pairs, which account for
every byte of the file - but inside an ARCH payload the objects are a stream
of class-specific records with no field names, and references between them
are absolute byte offsets into that payload. Only a fraction of those record
layouts have been worked out. Emitting a whole project from nothing would
mean inventing the ones that have not, and getting any of them wrong gives a
file Cubase refuses to open with nothing to debug from.

So this writes a .cpr the way it can be written safely: it starts from the
.cpr the project came from and changes the fields it knows, without moving a
single byte. Every offset in the file stays valid because nothing shifts, and
every byte that is not deliberately changed is still exactly what Cubase
wrote. What comes out opens as a normal Cubase project.

That buys the round trip - take a project to REAPER, work on it, bring the
work back - for everything that fits in a fixed-width field:

  * where an event sits, how long it is, and how far into the file it starts
  * whether an event is muted

It cannot add or remove tracks and events, rename anything, or add plug-ins:
all of those change the size of a record, which moves every byte after it and
invalidates every reference past that point. Those are reported instead of
being half-applied, so nothing is lost quietly.
"""
import math
import os
import struct

from . import cpr_read
from . import progress

MUTED = cpr_read.MUTED
PPQ = cpr_read.PPQ


class Patch:
    """The edits to make, collected before any byte is written."""

    def __init__(self):
        self.writes = []        # (file offset, bytes, description)
        self.skipped = []       # things that needed the file to change shape

    def f64(self, off, value, what):
        self.writes.append((off, struct.pack('>d', float(value)), what))

    def u16(self, off, value, what):
        self.writes.append((off, struct.pack('>H', int(value) & 0xFFFF), what))

    # Attribute values carry the type code Cubase wrote them with, and the
    # replacement has to be exactly as wide as what it replaces or every
    # offset after it moves.
    WIDTH = {1: ('>q', 8), 3: ('>f', 4), 4: ('>d', 8)}

    def num(self, slot, value, what):
        off, t = slot
        fmt = self.WIDTH.get(t)
        if fmt is None:
            self.skip('%s: stored in a form that cannot be replaced in place'
                      % what)
            return
        packer, _n = fmt
        raw = struct.pack(packer, int(value) if t == 1 else float(value))
        self.writes.append((off, raw, what))

    def f32le(self, off, value, what):
        self.writes.append((off, struct.pack('<f', float(value)), what))

    def skip(self, what):
        self.skipped.append(what)

    def apply(self, data):
        out = bytearray(data)
        for off, raw, _what in self.writes:
            if off < 0 or off + len(raw) > len(out):
                raise ValueError('patch at %#x is outside the file' % off)
            out[off:off + len(raw)] = raw
        return bytes(out)


def s2t(reader, seconds):
    """Seconds back to Cubase ticks, following the tempo map."""
    pts = reader.tempo_pts
    base_t, base_s, spq = pts[0]
    for t, s, sq in pts:
        if seconds + 1e-9 >= s:
            base_t, base_s, spq = t, s, sq
        else:
            break
    rate = PPQ / spq if spq > 0 else 960.0
    return base_t + (seconds - base_s) * rate


def match_tracks(new, old, log):
    """Line the two track lists up.

    Names repeat in a real project - this one has seven tracks called
    'Guitar (D)' - so position is the primary key and the name is only used
    to notice that the lists have drifted apart."""
    pairs = []
    if len(new.tracks) != len(old.tracks):
        log.append('the REAPER project has %d tracks and the Cubase project '
                   '%d: only tracks that still line up are written back'
                   % (len(new.tracks), len(old.tracks)))
    for i, ot in enumerate(old.tracks):
        if i >= len(new.tracks):
            break
        nt = new.tracks[i]
        if nt.name != ot.name:
            log.append('track %d is %r in REAPER and %r in Cubase; matched by '
                       'position' % (i + 1, nt.name, ot.name))
        pairs.append((nt, ot))
    return pairs


def plan(new, old_reader, old, log):
    """Work out every byte that has to change."""
    p = Patch()
    moved = muted = resized = levels = pans = 0
    for nt, ot in match_tracks(new, old, log):
        # REAPER holds a Cubase track's overlapping events cut down to what
        # Cubase plays (model.flatten_lanes), so its items are compared with
        # the original's events cut the same way. Unchanged, there is
        # nothing to write; changed, the pieces no longer map one to one
        # onto records in the file, and pairing them by list position wrote
        # other events' positions into them (Gradila's OLIVER track).
        import copy
        from .model import flatten_lanes
        of = copy.copy(ot)
        of.items = list(ot.items)
        n_cut, n_gone = flatten_lanes(of)
        if n_cut or n_gone:
            def sig(items):
                return sorted((round(i.pos, 4), round(i.length, 4),
                               round(i.soffs, 4), int(bool(i.mute)))
                              for i in items if i.kind != 'video')
            def place(items):
                return sorted((((round(i.pos, 4), round(i.length, 4),
                                 round(i.soffs, 4)), i)
                               for i in items if i.kind != 'video'),
                              key=lambda x: x[0])
            same_place = [k for k, _ in place(nt.items)] == \
                [k for k, _ in place(of.items)]
            if sig(nt.items) != sig(of.items) and same_place:
                # nothing moved, only mutes changed: each piece is a copy of
                # the Cubase event it was cut from (it shares its origin), so
                # the mute goes onto that event - when every piece of it says
                # the same. Refusing the whole track kept Gradila's VOCAL 1
                # muted after it was unmuted in REAPER.
                want = {}
                for (_k, ni), (_k2, pi) in zip(place(nt.items), place(of.items)):
                    org = pi.origin
                    if not org or 'flags' not in org:
                        continue
                    want.setdefault(id(org), (org, pi, set()))[2].add(bool(ni.mute))
                for org, pi, states in want.values():
                    if len(states) != 1:
                        p.skip('%s: some of its pieces were muted in REAPER and '
                               'some not; a Cubase event mutes as a whole, so it '
                               'was left as it was' % pi.name)
                        continue
                    m = states.pop()
                    if m != bool(pi.mute):
                        flags = struct.unpack_from(
                            '>H', old_reader.A.d, org['flags'] - old_reader.A.base)[0]
                        flags = (flags | MUTED) if m else (flags & ~MUTED)
                        p.u16(org['flags'], flags, '%s: %s' % (
                            pi.name, 'muted' if m else 'unmuted'))
                        muted += 1
            elif sig(nt.items) != sig(of.items):
                p.skip('%r: its overlapping events were edited in REAPER; '
                       'they cannot be matched to the records in the Cubase '
                       'file one to one, so they were left as they were'
                       % ot.name)
            nt_items_done = True
        else:
            nt_items_done = False
        if nt_items_done:
            pass
        elif len(nt.items) != len(ot.items):
            p.skip('%r: %d parts in REAPER, %d in Cubase - adding or removing '
                   'a part changes the size of the record and would move every '
                   'byte after it' % (ot.name, len(nt.items), len(ot.items)))
            continue
        # paired by lane and place, not position alone: two events starting
        # together on different lanes (Boryana's Kaval, versions v2 and
        # 'From Reference', both at 48 s) swapped lengths and offsets
        def _key(x):
            return (getattr(x, 'lane', 0) or 0, round(x.pos, 6),
                    round(x.length, 6), round(x.soffs, 6))
        n_items = sorted(nt.items, key=_key)
        o_items = sorted(ot.items, key=_key)
        if nt_items_done:
            n_items = o_items = []
        for ni, oi in zip(n_items, o_items):
            org = oi.origin
            if not org:
                continue
            if abs(ni.pos - oi.pos) > 1e-6:
                p.f64(org['start'], s2t(old_reader, ni.pos),
                      '%s: moved to %.3fs' % (oi.name, ni.pos))
                moved += 1
            if abs(ni.length - oi.length) > 1e-6:
                if oi.kind == 'midi' or oi.warped:
                    # musical length: ticks, same units as the start
                    a = s2t(old_reader, ni.pos)
                    b = s2t(old_reader, ni.pos + ni.length)
                    p.f64(org['length'], b - a,
                          '%s: length %.3fs' % (oi.name, ni.length))
                else:
                    p.f64(org['length'], ni.length * org['rate'],
                          '%s: length %.3fs' % (oi.name, ni.length))
                resized += 1
            if abs(ni.soffs - oi.soffs) > 1e-6:
                if oi.kind == 'audio' and not oi.warped:
                    p.f64(org['offset'], ni.soffs * org['rate'],
                          '%s: start offset %.3fs' % (oi.name, ni.soffs))
                elif oi.warped and org.get('clip_bpm'):
                    # a stretched clip counts its offset in ticks of the
                    # tempo the clip itself was cut at
                    p.f64(org['offset'],
                          ni.soffs * PPQ * org['clip_bpm'] / 60.0,
                          '%s: start offset %.3fs' % (oi.name, ni.soffs))
                else:
                    p.skip('%s: start offset changed, but the units a MIDI '
                           'part measures it in are not established, so it '
                           'was left alone rather than guessed at' % oi.name)
            if bool(ni.mute) != bool(oi.mute):
                flags = struct.unpack_from('>H', old_reader.A.d,
                                           org['flags'] - old_reader.A.base)[0]
                flags = (flags | MUTED) if ni.mute else (flags & ~MUTED)
                p.u16(org['flags'], flags,
                      '%s: %s' % (oi.name, 'muted' if ni.mute else 'unmuted'))
                muted += 1
        org = ot.origin or {}
        # REAPER's fader and pan are not Cubase's: the REAPER writer put the
        # pan with the same L/R ratio and the level difference on the fader
        # (rpp_write.reaper_mix). Copied back as they stand, the fader kept
        # that difference on top of Cubase's own panner and a pan-sweep
        # track came back 1.3-1.8 dB quiet (TB2's round trip). So the pair
        # is mapped to Cubase's panner first, as a fresh build does
        # (cpr_build.cubase_mix), and compared in Cubase's own terms.
        c_vol, c_pan = nt.vol, nt.pan
        if getattr(new, 'pan_law_of', None) == 'reaper':
            from . import panlaw
            law = new.panlaw if getattr(new, 'panlaw', None) is not None else 1.0
            mode = nt.panmode if getattr(nt, 'panmode', None) is not None                 else getattr(new, 'panmode', 3)
            c_pan, gain = panlaw.reaper_to_cubase(
                nt.pan, law, mode, mono=bool(getattr(ot, 'mono', False)),
                law_code=getattr(old, 'panlaw_code', None) or 6)
            c_vol = nt.vol * gain
        if abs(c_vol - ot.vol) > 1e-4 * max(1.0, ot.vol):
            # the fader keeps its level twice - as dB and as the raw slider
            # position - and Cubase trusts them to agree, so both are set
            db = -144.0 if c_vol <= 0 else 20.0 * math.log10(c_vol)
            if 'vol_db' in org:
                p.num(org['vol_db'], db, '%s: level %.2f dB' % (ot.name, db))
            if 'vol_raw' in org:
                raw = old_reader.gain_to_norm(c_vol) * 32768.0
                p.num(org['vol_raw'], raw,
                      '%s: fader position' % ot.name)
            if 'vol_db' in org or 'vol_raw' in org:
                levels += 1
            else:
                p.skip('%r: level changed but the fader was not found in the '
                       'Cubase project' % ot.name)
        if abs(c_pan - ot.pan) > 1e-4:
            # the position is the first f32 (little-endian, 0..1) of the
            # panner's state, where cpr_read found it (origin 'pan')
            if org.get('pan'):
                off, _ty = org['pan']
                p.f32le(off, min(1.0, max(0.0, c_pan / 2.0 + 0.5)),
                        '%s: pan %+.3f' % (ot.name, c_pan))
                pans += 1
            else:
                p.skip('%r: pan changed to %+.2f, but the panner was not '
                       'found in the Cubase project' % (ot.name, c_pan))
        nd = float(getattr(nt, 'delay', 0.0) or 0.0)
        if abs(nd - float(getattr(ot, 'delay', 0.0) or 0.0)) > 1e-7:
            if org.get('delay'):
                p.f64(org['delay'][0], nd, '%s: delay %+.2f ms' % (ot.name, nd * 1000))
            else:
                p.skip('%r: track delay changed to %+.2f ms, but the Cubase '
                       'channel has no delay field' % (ot.name, nd * 1000))
        if len(nt.fx) != len(ot.fx):
            p.skip('%r: the effect chain differs (%d in REAPER, %d in Cubase); '
                   'plug-ins cannot be added to a .cpr in place'
                   % (ot.name, len(nt.fx), len(ot.fx)))
    if len(new.markers) != len(old.markers):
        p.skip('markers differ (%d in REAPER, %d in Cubase); they are not '
               'written back yet' % (len(new.markers), len(old.markers)))
    return p, {'moved': moved, 'resized': resized, 'muted': muted,
               'levels': levels, 'pans': pans}


ORIGIN_SUFFIX = '.cubase-origin'


def note_origin(rpp_path, cpr_path):
    """Remember which Cubase project a REAPER project was made from.

    Written beside the .rpp when converting out, read back when converting
    in, so the return trip needs no --template. A file of its own rather
    than a line inside the .rpp, because REAPER rewrites the .rpp whenever
    you save and will not keep a token it does not recognise."""
    try:
        with open(rpp_path + ORIGIN_SUFFIX, 'w', encoding='utf-8') as f:
            f.write(os.path.abspath(cpr_path) + '\n')
    except OSError:
        pass


def find_template(rpp_path, explicit=None):
    """Work out which .cpr to update, and say how it was found."""
    if explicit:
        return explicit, 'given with --template'
    stem = os.path.splitext(rpp_path)[0]
    marker = rpp_path + ORIGIN_SUFFIX
    if os.path.exists(marker):
        try:
            p = open(marker, encoding='utf-8').read().strip()
            if p and os.path.exists(p):
                return p, 'recorded when this project was converted out'
        except OSError:
            pass
    beside = stem + '.cpr'
    if os.path.exists(beside):
        return beside, 'found beside the REAPER project'
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.pardir, 'templates')
    cand = os.path.join(os.path.abspath(here),
                        os.path.basename(stem) + '.cpr')
    if os.path.exists(cand):
        return cand, 'found in the templates folder'
    return None, None


def write(new, path, template, log=None):
    """Apply `new` to a copy of `template` and save it as `path`."""
    log = log if log is not None else []
    if not template or not os.path.exists(template):
        raise SystemExit(
            'writing a .cpr needs the Cubase project this work started from, '
            'and none was found.\n'
            'A .cpr can only be written by editing one - a blank or unrelated '
            'project has none of your tracks in it to edit, so it would come '
            'back unchanged.\n'
            'Point at the original with --template, or put it beside the '
            '.rpp under the same name.')
    progress.stage('reading the Cubase project to update: %s'
                   % os.path.basename(template))
    reader = cpr_read.CprReader(template)
    old = reader.read()
    log.extend(reader.log)

    progress.stage('working out what changed')
    p, counts = plan(new, reader, old, log)

    data = open(template, 'rb').read()
    out = p.apply(data)
    progress.stage('saving %s (%s, %d field(s) changed)'
                   % (os.path.basename(path), progress.fmt_bytes(len(out)),
                      len(p.writes)))
    with open(path, 'wb') as f:
        f.write(out)
    counts['media copied'] = carry_media(old, template, path, log)
    for s in p.skipped:
        log.append(s)
    counts['fields'] = len(p.writes)
    counts['skipped'] = len(p.skipped)
    counts['bytes'] = len(out)
    return counts


def carry_media(old, template, path, log):
    """Copy the files the template keeps in its own project folder next to
    the new .cpr, at the same place relative to it.

    Cubase finds a file inside a project's folder (its Audio pool) relative
    to where the project is now, whatever absolute path the clip also
    stores. The updated project is saved somewhere else - drop.py puts it in
    a folder of its own - so a file in the template's Audio folder was
    looked for in the new folder, Cubase asked to resolve missing files and
    the events played silence (TB2's mono tone on the way back from
    REAPER, 2026-09-29). Files outside the template's folder are left where
    they are; they are found by their absolute path."""
    import shutil
    src_root = os.path.dirname(os.path.abspath(template))
    dst_root = os.path.dirname(os.path.abspath(path))
    if os.path.normcase(src_root) == os.path.normcase(dst_root):
        return 0
    n = 0
    seen = set()
    for t in old.tracks:
        for it in t.items:
            f = getattr(it, 'file', None)
            if not f or it.kind not in ('audio', 'video'):
                continue
            if not os.path.isfile(f):
                # a path from another machine (Gradila's E:\OneDrive\...):
                # Cubase looks for the file by name in the project folder
                from . import media
                hit = media.locate(f, [src_root])
                if not hit:
                    continue
                f = hit
            f = os.path.abspath(f)
            key = os.path.normcase(f)
            if key in seen:
                continue
            seen.add(key)
            rel = os.path.relpath(f, src_root) if os.path.splitdrive(f)[0].lower()                 == os.path.splitdrive(src_root)[0].lower() else None
            if not rel or rel.startswith(os.pardir) or not os.path.isfile(f):
                continue
            dst = os.path.join(dst_root, rel)
            if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(f):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(f, dst)
                n += 1
            except OSError as e:
                log.append('%s could not be copied next to the new project '
                           '(%s); Cubase will ask where it is' % (rel, e))
    if n:
        log.append('%d audio file(s) from the original project folder were '
                   'copied next to the new one, where Cubase looks for them'
                   % n)
    return n
