"""Write the model as an Ableton Live 11 Set (.als).

A Live Set is gzipped XML. Live refuses a Set with elements missing, so
nothing here is invented: every track and clip is a copy of the matching
record in templates/live-donor.als (tools/make_live_donor.py builds it from
Live's own default tracks) with its fields filled in.

Everything goes into the Arrangement. Times there are in beats (quarter
notes) from the start of the song, so seconds are mapped through the tempo
map. Audio clips are written with Warp OFF: an unwarped clip plays its file
at its own speed whatever the tempo does, which is how REAPER and Cubase
play an ordinary item or event. A clip with Warp on follows the tempo and
would stretch - Live's default for a dragged-in file, and the one thing
this writer never lets happen to audio that was not stretched.

What Live has no place for is said in the log: plug-ins, sends, MIDI CC,
solo, regions (a locator marks where each one starts).
"""
import copy
import gzip
import math
import os
import struct
import xml.etree.ElementTree as ET

from . import media
from .model import _qn_upto

DONOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'templates', 'live-donor.als')

# Live's mixer: -inf .. +6 dB, as linear gain
LIVE_VOL_MIN = 0.0003162277571
LIVE_VOL_MAX = 1.99526238
START_TIME = '-63072000'        # the time of an envelope's first event

# Live 11's clip and track colours, index 0..69 (five rows of fourteen)
PALETTE = [
    0xFF94A6, 0xFFA529, 0xCC9927, 0xF7F47C, 0xBFFB00, 0x1AFF2F, 0x25FFA8,
    0x5CFFE8, 0x8BC5FF, 0x5480E4, 0x92A7FF, 0xD86CE4, 0xE553A0, 0xFFFFFF,
    0xFF3636, 0xF66C03, 0x99724B, 0xFFF034, 0x87FF67, 0x3DC300, 0x00BFAF,
    0x19E9FF, 0x10A4EE, 0x007DC0, 0x886CE4, 0xB677C6, 0xFF39D4, 0xD0D0D0,
    0xE2675A, 0xFFA374, 0xD3AD71, 0xEDFFAE, 0xD2E498, 0xBAD074, 0x9BC48D,
    0xD4FDE1, 0xCDF1F8, 0xB9C1E3, 0xCDBBE4, 0xAE98E5, 0xE5DCE1, 0xA9A9A9,
    0xC6928B, 0xB78256, 0x99836A, 0xBFBA69, 0xA6BE00, 0x7DB04D, 0x88C2BA,
    0x9BB3C4, 0x85A5C2, 0x8393CC, 0xA595B5, 0xBF9FBE, 0xBC7196, 0x7B7B7B,
    0xAF3333, 0xA95131, 0x724F41, 0xDBC300, 0x85961F, 0x539F31, 0x0A9C8E,
    0x236384, 0x1A2F96, 0x2F52A2, 0x624BAD, 0xA34BAD, 0xCC2E6E, 0x3C3C3C,
]
DEFAULT_COLOR = 69


def live_color(rgb):
    """The palette entry nearest an (r, g, b), or the default grey."""
    if not rgb:
        return DEFAULT_COLOR
    r, g, b = rgb
    best, bd = DEFAULT_COLOR, None
    for k, c in enumerate(PALETTE):
        d = ((((c >> 16) & 255) - r) ** 2 + (((c >> 8) & 255) - g) ** 2
             + ((c & 255) - b) ** 2)
        if bd is None or d < bd:
            best, bd = k, d
    return best


def tsig_code(num, den):
    """Live's time signature enum: numerator - 1 + 99 * log2(denominator)."""
    return int(num) - 1 + 99 * int(round(math.log(int(den), 2)))


def fmt(v):
    """A number the way Live writes one: no trailing zeros."""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    s = repr(float(v))
    if s.endswith('.0'):
        s = s[:-2]
    return s


def setv(elem, path, value):
    node = elem.find(path)
    if node is None:
        raise KeyError('%s has no %s' % (elem.tag, path))
    node.set('Value', fmt(value) if not isinstance(value, str) else value)


# ------------------------------------------------------------------ ids
class Ids:
    """Pointee ids are one namespace across the whole Set: every automation
    and modulation target, and what envelopes point at."""

    TAGS = ('AutomationTarget', 'ModulationTarget', 'Pointee')

    def __init__(self, start):
        self.next = start

    def take(self):
        n = self.next
        self.next += 1
        return n

    @classmethod
    def is_pointee(cls, e):
        t = e.tag
        return (t in cls.TAGS or t.endswith('ModulationTarget')
                or t.startswith('ControllerTargets.'))

    def renumber(self, elem):
        remap = {}
        for e in elem.iter():
            if self.is_pointee(e) and e.get('Id') is not None:
                n = str(self.take())
                remap[e.get('Id')] = n
                e.set('Id', n)
        for e in elem.iter('PointeeId'):
            v = e.get('Value')
            if v in remap:
                e.set('Value', remap[v])


# ---------------------------------------------------------------- media
def sample_info(path):
    """(frames, sample rate) of an audio file, or (None, None)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(12)
            if head[:4] in (b'RIFF', b'RF64') and head[8:12] == b'WAVE':
                rate = ch = bits = None
                while True:
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        break
                    cid, size = hdr[:4], struct.unpack('<I', hdr[4:])[0]
                    if cid == b'fmt ':
                        body = f.read(size)
                        ch = struct.unpack_from('<H', body, 2)[0]
                        rate = struct.unpack_from('<I', body, 4)[0]
                        bits = struct.unpack_from('<H', body, 14)[0]
                    elif cid == b'data':
                        if rate:
                            frame = max(1, (bits + 7) // 8) * max(1, ch)
                            return size // frame, rate
                        break
                    else:
                        f.seek(size + (size & 1), 1)
    except OSError:
        return None, None
    # anything else (MP3, FLAC, a video's sound): ffmpeg's description -
    # the duration and the audio stream's own rate
    ff = media.find_ffmpeg()
    if ff and os.path.isfile(path):
        import re
        import subprocess
        try:
            r = subprocess.run([ff, '-hide_banner', '-i', path], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=120)
            txt = r.stderr.decode('utf-8', 'replace')
        except (OSError, subprocess.TimeoutExpired):
            txt = ''
        m = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', txt)
        a = re.search(r'Audio:[^\n]*?(\d+) Hz', txt)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            rate = int(a.group(1)) if a else 48000
            return int(round(dur * rate)), rate
    return None, None


def file_ref(fr, path, set_dir):
    """Point a <FileRef> at `path`: absolute, plus relative to the Set when
    the two are on one drive.

    The relative path is type 1, relative to the Set file. Type 3 (relative
    to the Live Project) found nothing in a converted folder that had been
    moved - Live has no project root for a folder without its Ableton
    Project Info - while type 1 found every file (2026-10-01). On the
    website the absolute path is the browser's own (/work/...), so the
    relative one is the only one that works there."""
    path = os.path.abspath(path)
    rel, kind = '', 0
    try:
        rel, kind = os.path.relpath(path, set_dir), 1
    except ValueError:
        pass                        # another drive: the absolute path only
    setv(fr, 'RelativePathType', str(kind))
    setv(fr, 'RelativePath', rel.replace('\\', '/'))
    setv(fr, 'Path', path.replace('\\', '/'))
    try:
        setv(fr, 'OriginalFileSize', str(os.path.getsize(path)))
    except OSError:
        setv(fr, 'OriginalFileSize', '0')
    setv(fr, 'OriginalCrc', '0')


def is_vst2(f):
    """A VST2 written as Live's VST2 device: one REAPER said is a VST2
    ('VST:' / 'VSTi:') or saved as its parameter dump. A VST2 from Cubase
    goes to its VST3 build instead (plugin_device)."""
    fmt_ = getattr(f, 'format', '') or ''
    if fmt_:
        return fmt_ == 'VST'
    return bool(getattr(f, 'param_dump', False))


# ------------------------------------------------------------ envelopes
def envelope(env_list, pointee, points, idx):
    """Append an AutomationEnvelope on `pointee` holding (beats, value)
    points; the first value also stands before the first point."""
    e = ET.SubElement(env_list, 'AutomationEnvelope', Id=str(idx))
    ET.SubElement(ET.SubElement(e, 'EnvelopeTarget'), 'PointeeId',
                  Value=str(pointee))
    auto = ET.SubElement(e, 'Automation')
    evs = ET.SubElement(auto, 'Events')
    pts = sorted(points)
    ET.SubElement(evs, 'FloatEvent', Id='0', Time=START_TIME,
                  Value=fmt(pts[0][1]))
    for k, (t, v) in enumerate(pts, 1):
        ET.SubElement(evs, 'FloatEvent', Id=str(k), Time=fmt(max(0.0, t)),
                      Value=fmt(v))
    tv = ET.SubElement(auto, 'AutomationTransformViewState')
    ET.SubElement(tv, 'IsTransformPending', Value='false')
    ET.SubElement(tv, 'TimeAndValueTransforms')
    return e


# ------------------------------------------------------------ preparing
def spill_overlaps(project, log):
    """REAPER plays every item on a track, overlapping or not; a Live track
    plays one clip at a time - a clip laid over another cuts it. So items
    that overlap go to a copy of the track right below, as many copies as
    the deepest pile needs."""
    out, moved, n = [], {}, 0
    for old_i, t in enumerate(project.tracks):
        moved[old_i] = len(out)
        out.append(t)
        if t.is_folder or len(t.items) < 2:
            continue
        lanes = []                       # end time of each lane's last item
        placed = []                      # (lane, item)
        for it in sorted(t.items, key=lambda i: (i.pos, i.length)):
            for k, end in enumerate(lanes):
                if it.pos >= end - 1e-6:
                    lanes[k] = it.pos + it.length
                    placed.append((k, it))
                    break
            else:
                lanes.append(it.pos + it.length)
                placed.append((len(lanes) - 1, it))
        if len(lanes) < 2:
            continue
        t.items = [i for k, i in placed if k == 0]
        for k in range(1, len(lanes)):
            a = copy.copy(t)
            a.name = '%s (overlap %d)' % (t.name, k + 1)
            a.items = [i for kk, i in placed if kk == k]
            # the copy plays the same track's audio: same chain, same sends
            a.sends = list(t.sends)
            out.append(a)
            n += 1
        log.append('%r: items overlap and REAPER plays them all; a Live track '
                   'plays one clip at a time, so the overlapping ones went on '
                   '%d copy track(s) right below' % (t.name, len(lanes) - 1))
    if n:
        project.tracks = out
        for t in project.tracks:
            for s in t.sends:
                if s.dest is not None:
                    s.dest = moved.get(s.dest, s.dest)
    return n


# ---------------------------------------------------------------- fades
_LIVE_FADES = None


def live_fades():
    """Live's fade curves, measured (tools/live_calibrate.py table): for
    each (skew, slope) on a 5 x 5 grid over -1..1, the fade-in and the
    fade-out gain at x = 0, 0.05 .. 1. Live's fade-out is not its fade-in
    run backwards: it is 1 - fadein(x), the same S flipped over."""
    global _LIVE_FADES
    if _LIVE_FADES is None:
        import json
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'live_fades.json')) as f:
            _LIVE_FADES = json.load(f)
    return _LIVE_FADES


def _live_curve(sk, sl, key):
    """The 21 gains of Live's curve at (skew, slope), interpolated between
    the measured grid points."""
    t = live_fades()
    g = t['grid']

    def at(v):
        v = min(g[-1], max(g[0], v))
        i = min(len(g) - 2, max(0, int((v - g[0]) / (g[1] - g[0]))))
        return i, (v - g[i]) / (g[i + 1] - g[i])
    i, fi = at(sk)
    j, fj = at(sl)

    def row(a, b):
        return t['curves']['%g %g' % (g[a], g[b])][key]
    r00, r01, r10, r11 = row(i, j), row(i, j + 1), row(i + 1, j), row(i + 1, j + 1)
    return [(1 - fi) * ((1 - fj) * a + fj * b) + fi * ((1 - fj) * c + fj * d)
            for a, b, c, d in zip(r00, r01, r10, r11)]


def _fade_err(want, got):
    """The worst gap between two curves in dB, over the part of the fade
    that is within 12 dB of full level - where a difference is heard. (In
    the first few percent both are near silence and any ratio is huge.)"""
    worst = 0.0
    for w, g in zip(want, got):
        if w >= 0.25:
            worst = max(worst, abs(20.0 * math.log10(max(g, 1e-6) / w)))
    return worst


def _fade_dist(want, got):
    """How far apart two curves are: RMS of the amplitude difference, what
    a null test of the two fades measures."""
    return math.sqrt(sum((w - g) ** 2 for w, g in zip(want, got)) / len(want))


def source_fade(it, key):
    """The item's fade as 21 gains at x = 0, 0.05 .. 1 from the fade's start
    (`key` 'in' or 'out'): REAPER's shape and curve (fades.py, measured),
    a Cubase fade's own points, else a straight line."""
    from . import fades
    xs = [k / 20.0 for k in range(21)]
    line = (it.fade_lines or {}).get('FADEIN' if key == 'in' else 'FADEOUT')
    if line:
        from .rpp_read import fade_shape
        shape, curve = fade_shape(line)
        f = fades.fadein_gain if key == 'in' else fades.fadeout_gain
        return [f(shape, curve, x) for x in xs]
    pts = (getattr(it, 'fade_points', None) or {}).get(key)
    if pts and len(pts) > 2:
        from .rpp_write import _interp_pts
        return [_interp_pts(sorted(pts), x) for x in xs]
    return list(xs) if key == 'in' else [1.0 - x for x in xs]


def fit_fade(want, key):
    """(skew, slope, worst dB) of Live's curve nearest `want` in amplitude,
    searched in steps of 0.1 over the measured range. Fitted on amplitude,
    not dB: a dB fit chases the first few percent, where both curves are
    near silence, and came out three times further off overall for a
    straight fade (RMS 0.18 against 0.06)."""
    best = None
    steps = [k / 10.0 for k in range(-10, 11)]
    for sk in steps:
        for sl in steps:
            d = _fade_dist(want, _live_curve(sk, sl, key))
            if best is None or d < best[2] - 1e-12:
                best = (sk, sl, d)
    sk, sl, _d = best
    return sk, sl, _fade_err(want, _live_curve(sk, sl, key))


def warp_markers(it, beats, tempo):
    """(source seconds, clip beats) pairs that play the item exactly as the
    source does: at every tempo change inside it and at every REAPER
    stretch marker, so the clip follows the music the way the source item
    did and nothing else moves. Clip beat 0 is the item's start."""
    r = it.playrate or 1.0
    s0 = it.soffs or 0.0
    sm = sorted(getattr(it, 'stretch_markers', None) or [])

    def src_at(x):
        """Source seconds playing at x seconds into the item."""
        if len(sm) >= 2:
            # REAPER stretch markers: (source s, item s) pairs, straight
            # lines between them, the outer slopes carried on
            pts = sorted((b, a) for a, b in sm)
            if x <= pts[0][0]:
                (x0, y0), (x1, y1) = pts[0], pts[1]
            elif x >= pts[-1][0]:
                (x0, y0), (x1, y1) = pts[-2], pts[-1]
            else:
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                    if x <= x1:
                        break
            return y0 if x1 <= x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return s0 + x * r
    xs = {0.0, it.length}
    xs.update(b for _a, b in sm if 0.0 < b < it.length)
    xs.update(s - it.pos for s, _bpm in tempo if it.pos < s < it.pos + it.length)
    b0 = beats(it.pos)
    return [(src_at(x), beats(it.pos + x) - b0) for x in sorted(xs)]


def item_curves_to_track(p, log):
    """A volume curve drawn inside an item, as the track's own volume
    automation over the item's span: a Live clip has no volume curve of its
    own that plays, and after spill_overlaps no two clips on a track
    overlap, so over that span the track's lane belongs to the item alone.
    The track's level (its fader, or its own lane) is multiplied by the
    curve; outside the item the lane is what it was."""
    from . import envelope
    n = 0
    for t in p.tracks:
        its = [i for i in t.items if i.kind == 'audio' and i.volenv
               and (len(i.volenv) > 1 or abs(i.volenv[0][1] - 1.0) > 1e-4)
               and not getattr(i, '_print', False)]
        if not its:
            continue
        base = t.volenv or [(0.0, t.vol)]
        spans = sorted((i.pos, i.pos + i.length, sorted(i.volenv)) for i in its)

        def level(x):
            g = envelope.interp(base, x)
            for a, b, env in spans:
                if a <= x <= b:
                    return g * envelope.interp(env, x - a)
            return g
        eps = 1e-4
        knots = [x for x, _ in base]
        for a, b, env in spans:
            knots += [a, b] + [a + s for s, _ in env if 0 < s < b - a]
        pts = envelope.sample(level, knots, envelope.db_err, envelope.DB_TOL)
        # the edges are steps: the level just outside each item is the
        # track's own, just inside it the item's
        for a, b, env in spans:
            pts += [(a - eps, envelope.interp(base, a - eps)), (b + eps, envelope.interp(base, b + eps))]
        t.volenv = sorted(pts)
        for i in its:
            i.volenv = []
        n += len(its)
    if n:
        log.append('%d item volume curve(s) written as track volume automation '
                   'over the item - a Live clip has no volume curve that plays'
                   % n)
    return n


def print_last_resort(p, path, log):
    """The items no Live setting can play as the source does - a reversed
    or cut source section, a pitch curve drawn inside the item, a loop
    still running past its file, item FX with no track to go to - printed
    to their own WAV with the converter's own ffmpeg (no other program,
    and the website has it too). Only these: everything else stays as
    clip settings."""
    todo = []

    def past_end(it):
        """A loop that still runs past its file's end (expand_loops cut the
        rest into back-to-back clips). REAPER switches looping on for every
        item by default; a loop that stays inside its file plays exactly
        as an unlooped clip - 270 of ZITRO's items were printed for it."""
        if not it.loop:
            return False
        f = media.resolve(it.file, p.srcdir)
        frames, sr = sample_info(f) if os.path.isfile(f) else (None, None)
        if not frames or not sr:
            return False
        return (it.soffs or 0.0) + it.length * (it.playrate or 1.0) > frames / float(sr) + 1e-3
    for t in p.tracks:
        for it in t.items:
            if it.kind != 'audio' or not it.file:
                continue
            if (getattr(it, 'section', None) or past_end(it) or getattr(it, 'takefx', None)
                    or (it.pitchenv and (len(it.pitchenv) > 1
                                         or abs(it.pitchenv[0][1]) > 1e-4))):
                it._print = True
                todo.append(it)
    if not todo:
        return []
    from .cpr_build import wav_info
    had = os.environ.get('CPR_NO_REAPER')
    os.environ['CPR_NO_REAPER'] = '1'       # ffmpeg only, never another DAW
    own = []                                # render's notes speak of Cubase
    try:
        done = media.render(p, path, p.samplerate or 48000, wav_info, own, items=todo)
    finally:
        if had is None:
            os.environ.pop('CPR_NO_REAPER', None)
    # keep what went wrong (no ffmpeg, a file that would not render)
    log.extend(m for m in own if 'could not' in m or 'not found' in m)
    if done:
        log.append('%d item(s) printed because Live has no way to play them as '
                   'the source does (a reversed or cut section, a pitch curve, '
                   'a loop past the file\'s end, item FX); everything else is '
                   'Live\'s own clip settings' % len(done))
    return done


def _wav_int32(path):
    """True for a WAV of 32-bit integer samples (Live reads 8/16/24-bit
    integer and 32-bit float only - 'Nymphs (video audio).wav' from the
    Media Foundation decode would not load)."""
    import struct
    try:
        with open(path, 'rb') as f:
            head = f.read(12)
            if head[:4] not in (b'RIFF', b'RF64') or head[8:12] != b'WAVE':
                return False
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return False
                cid, size = hdr[:4], struct.unpack('<I', hdr[4:])[0]
                if cid == b'fmt ':
                    body = f.read(size)
                    tag, bits = struct.unpack_from('<H', body, 0)[0], struct.unpack_from('<H', body, 14)[0]
                    if tag == 0xFFFE and len(body) >= 26:
                        tag = struct.unpack_from('<H', body, 24)[0]
                    return tag == 1 and bits == 32
                f.seek(size + (size & 1), 1)
    except OSError:
        return False


def live_readable(p, path, log):
    """Every WAV the Set plays in a form Live reads: a 32-bit integer file
    is rewritten as 32-bit float beside it (float holds 24 bits exactly,
    so nothing audible changes), with ffmpeg."""
    seen, n = {}, 0
    ff = media.find_ffmpeg()
    for t in p.tracks:
        for it in t.items:
            if it.kind != 'audio' or not it.file:
                continue
            f = media.resolve(it.file, p.srcdir)
            if f in seen:
                if seen[f]:
                    it.file = seen[f]
                continue
            seen[f] = None
            if not (os.path.isfile(f) and _wav_int32(f)) or not ff:
                continue
            import subprocess
            dst = os.path.splitext(f)[0] + ' (f32).wav'
            if os.path.dirname(os.path.abspath(dst)) != os.path.join(
                    os.path.dirname(os.path.abspath(path)), 'Audio'):
                d = os.path.join(os.path.dirname(os.path.abspath(path)), 'Audio')
                os.makedirs(d, exist_ok=True)
                dst = os.path.join(d, os.path.basename(dst))
            r = subprocess.run([ff, '-v', 'error', '-y', '-i', f, '-c:a', 'pcm_f32le',
                                '-rf64', 'auto', dst], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode == 0 and os.path.isfile(dst):
                seen[f] = dst
                it.file = dst
                n += 1
    if n:
        log.append('%d 32-bit integer WAV file(s) rewritten as 32-bit float, which '
                   'Live reads (it does not read 32-bit integer)' % n)


def group_inputs(p, log):
    """A Cubase channel can output to a group channel; in Live a group
    track takes no input - what another track sends to its Track In is
    never heard (SuperThunderCrown's 'CROWN STOP' 4 dB short of Cubase).
    So a group that gets outputs from outside its folder gets an empty
    audio track as its first child, '<group> (in)', that takes them in
    and plays them through the group's effects as Cubase does."""
    from .model import Track, output_routes, _repoint_sends
    routes = output_routes(p)
    into = {}
    for i, g in routes:
        if p.tracks[g].is_folder:
            into.setdefault(g, []).append(i)
    if not into:
        return
    out, moved = [], {}
    for old_i, t in enumerate(p.tracks):
        moved[old_i] = len(out)
        out.append(t)
        if old_i not in into:
            continue
        a = Track('%s (in)' % t.name, t.depth + 1)
        a.color = t.color
        a.bus_id = ('live group in', old_i)
        for i in into[old_i]:
            p.tracks[i].out_bus_id = a.bus_id
        out.append(a)
        log.append('%r takes the output of %d track(s) from outside it; a Live '
                   'group takes no input, so %r inside it does'
                   % (t.name, len(into[old_i]), a.name))
    p.tracks = out
    _repoint_sends(p, moved)


def prepare_returns(p, log):
    """Which tracks become Live return tracks, and what they carry.

    REAPER can send to any track; Live sends only to return tracks, which
    hold no clips. So every send target without clips of its own becomes a
    return. A return outputs straight to the master, so the folders it sat
    in no longer pass it through: their level is multiplied into the
    return's fader (what REAPER applied on the way out), and what else they
    do to it - plug-ins, pan, a mute - is named. A folder left with nothing
    in it goes. Returns list in send-target order, after the other tracks.
    Sets t.live_return on each, and returns their indices."""
    targets = sorted({s.dest for t in p.tracks for s in t.sends
                      if s.dest is not None and 0 <= s.dest < len(p.tracks)})
    rets = []
    for k in targets:
        t = p.tracks[k]
        if t.items:
            log.append('%r receives sends but also plays clips of its own; a '
                       'Live return holds no clips, so it stays a track and the '
                       'sends to it are not written' % t.name)
            continue
        if t.is_folder:
            log.append('%r is a folder that receives sends; its children stay '
                       'in a group and the sends to it are not written' % t.name)
            continue
        rets.append(k)
    for k in rets:
        t = p.tracks[k]
        t.live_return = True
        # the folders above it, innermost first
        ups, d = [], t.depth
        for j in range(k - 1, -1, -1):
            u = p.tracks[j]
            if u.is_folder and u.depth < d:
                ups.append(u)
                d = u.depth
                if d == 0:
                    break
        for u in ups:
            t.vol *= (u.vol if u.vol is not None else 1.0)
            if u.fx or abs(u.pan or 0.0) > 1e-6 or u.mute or u.volenv:
                log.append('%r sat in folder %r, whose plug-ins, pan, mute or '
                           'automation a Live return does not pass through - '
                           'only its level (%+.2f dB) is carried, on the return'
                           % (t.name, u.name, 20 * math.log10(max(u.vol or 1.0, 1e-9))))
    if rets:
        # folders that held only returns are empty once the returns leave
        # (judged on the folder structure as it was)
        keep = []
        for i, t in enumerate(p.tracks):
            if t.is_folder and not t.items and not getattr(t, 'live_return', False):
                kids = p.folder_children(i)
                if kids and all(getattr(p.tracks[j], 'live_return', False)
                                or p.tracks[j].is_folder for j in kids):
                    log.append('folder %r held only send targets, which are '
                               'Live returns now; it is left out' % t.name)
                    continue
            keep.append(i)
        for k in rets:
            p.tracks[k].depth = 0
        moved = {old: new for new, old in enumerate(keep)}
        p.tracks = [p.tracks[i] for i in keep]
        for t in p.tracks:
            for s in t.sends:
                s.dest = moved.get(s.dest)
        log.append('%d send target(s) are Live return tracks: %s'
                   % (len(rets), ', '.join(repr(t.name) for t in p.tracks
                                           if getattr(t, 'live_return', False))))
    return [i for i, t in enumerate(p.tracks) if getattr(t, 'live_return', False)]


def prepare(p, path, log):
    """Reshape the model into tracks Live can hold. The same splits the
    Cubase builder makes, for the same reasons: a Live track is audio or
    MIDI, never both; a group holds no clips; a clip carries no plug-ins.

    Nothing is printed and no other program is started: what the source
    did to an item is written as Live's own clip settings (warp, pitch,
    fades), so it stays editable, and what Live has no setting for is
    named in the log rather than baked into a new file."""
    from . import model
    model.window_midi(p, log)
    # Video: Live plays a video file as a clip in the Arrangement, on an
    # audio track - the picture in its video window, the file's sound on
    # the track. So a video track is an audio track whose clips are the
    # video files themselves, at the item's place, gain, mute and fades;
    # nothing is extracted or printed. A video item turned down to nothing
    # (REAPER plays no sound from it) keeps its silence as clip gain 0.
    # Its sound: a video's audio is AAC as a rule, which Live on Windows does
    # not decode (its own AAC reference sample fails to load here; Cherry
    # Link's video tracks and an .mp4 on an audio track were silent). So,
    # as for Cubase (split_video_audio), the sound is decoded the way REAPER
    # decodes it into the project's Audio folder and plays on a track of
    # its own; the video clip keeps the picture at gain 0, so a Live that
    # can decode it does not play the sound twice.
    model.split_video_audio(p, path, log)
    nv = 0
    for t in p.tracks:
        if t.kind != 'video':
            continue
        t.kind = 'audio'
        for it in t.items:
            if it.kind == 'video':
                it.kind = 'audio'
                it.is_video = True
                it.gain = 0.0
                nv += 1
    if nv:
        log.append('%d video item(s) are video clips in Live\'s Arrangement, on '
                   'audio tracks: the picture plays in Live\'s video window, the '
                   'sound on the track' % nv)
    # an item with FX of its own becomes a track with that chain, rather
    # than an item printed through REAPER (split_item_fx defers to REAPER
    # when it is installed)
    had = os.environ.get('CPR_NO_REAPER')
    os.environ['CPR_NO_REAPER'] = '1'
    try:
        # what a Live track cannot hold either (expand_for_cubase without
        # split_instruments: several instruments on one track are an
        # Instrument Rack in Live, the track stays whole - write_chain)
        model.split_folder_items(p, log)
        model.split_item_fx(p, log)
        model.split_item_pan(p, log)
        model.split_mixed(p, log)
    finally:
        if had is None:
            os.environ.pop('CPR_NO_REAPER', None)
    media.relink(p, path, log)

    def dur(f):
        f = media.resolve(f, p.srcdir)
        if not os.path.isfile(f):
            return None
        frames, sr = sample_info(f)
        return frames / float(sr) if frames and sr else None
    # Compressed audio the two hosts do not decode alike, decoded to WAV
    # the way REAPER plays it (media.prepare, as for Cubase): MP3 - Live and
    # REAPER trim its encoder delay differently (bonus_tb_counter 2 dB off,
    # its loops a few ms apart) - and AAC/MP4, which Live does not decode
    # here at all. Decoding is not a print: the samples are the file's own.
    from .cpr_build import wav_info
    decode = [i for t in p.tracks for i in t.items
              if i.kind == 'audio' and i.file and not getattr(i, 'is_video', False)
              and os.path.splitext(i.file)[1].lower() in ('.mp3', '.mp4', '.m4a', '.aac', '.mov', '.m4v', '.wma')]
    if decode:
        had = os.environ.get('CPR_NO_REAPER')
        os.environ['CPR_NO_REAPER'] = '1'
        try:
            done, failed = media.prepare(p, path, p.samplerate or 48000, wav_info, log,
                                         items=decode, skip_printable=False)
        finally:
            if had is None:
                os.environ.pop('CPR_NO_REAPER', None)
        if done:
            log.append('%d compressed file(s) (MP3/AAC) decoded to WAV the way REAPER '
                       'plays them, for Live to play the same samples' % len(done))
    model.expand_loops(p, log, dur)
    if getattr(p, 'pan_law_of', None) == 'cubase':
        model.flatten_project_lanes(p, log)
    else:
        for t in p.tracks:
            t.items = [i for i in model.playing_items(t)]
    print_last_resort(p, path, log)
    spill_overlaps(p, log)
    item_curves_to_track(p, log)


# --------------------------------------------------------------- writer
class Writer:
    def __init__(self, project, path, log, donor=DONOR):
        self.p = project
        self.path = path
        self.set_dir = os.path.dirname(os.path.abspath(path))
        self.log = log
        self.root = ET.fromstring(gzip.open(donor).read())
        self.ls = self.root.find('LiveSet')
        self.ids = Ids(int(self.ls.find('NextPointeeId').get('Value')))
        tracks = self.ls.find('Tracks')
        self.proto = {t.tag: t for t in tracks}
        aev = self.proto['AudioTrack'].find(
            'DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events')
        mev = self.proto['MidiTrack'].find(
            'DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events')
        self.proto_aclip = aev[0]
        self.proto_mclip = mev[0]
        aev.remove(self.proto_aclip)
        mev.remove(self.proto_mclip)
        for t in list(tracks):
            tracks.remove(t)
        self.tracks = tracks
        protos = self.root.find('ConverterProtos')
        self.root.remove(protos)            # the writer's alone; never saved
        self.proto_fx = protos.find("PluginDevice[@role='effect']")
        self.proto_inst = protos.find("PluginDevice[@role='instrument']")
        self.proto_gain = protos.find('StereoGain')
        self.proto_rack = protos.find('InstrumentGroupDevice')
        self.proto_vst2 = protos.find("PluginDevice[@role='vst2']")
        self.protos = protos
        self.stock_notes = []
        self.proto['ReturnTrack'] = protos.find('ReturnTrack')
        self.proto_send = protos.find('TrackSendHolder')
        self.returns = []                   # model indices, in Live's order
        self.dev_id = 0
        self.tempo = sorted(project.tempo or [(0.0, 120.0)])
        self.clip_id = 0
        self.stats = dict(tracks=0, groups=0, audio_clips=0, midi_clips=0,
                          notes=0, missing=0)

    # seconds -> beats on the song's tempo map
    def beats(self, s):
        return _qn_upto(self.tempo, max(0.0, s))

    def bpm_at(self, s):
        bpm = self.tempo[0][1]
        for t, b in self.tempo:
            if t <= s + 1e-9:
                bpm = b
        return bpm

    # ------------------------------------------------------------ song
    def write_song(self):
        p = self.p
        mixer = self.ls.find('MasterTrack/DeviceChain/Mixer')
        bpm0 = self.tempo[0][1]
        setv(mixer, 'Tempo/Manual', bpm0)
        num, den = p.tsig or (4, 4)
        code = tsig_code(num, den)
        setv(mixer, 'TimeSignature/Manual', str(code))
        tempo_id = mixer.find('Tempo/AutomationTarget').get('Id')
        tsig_id = mixer.find('TimeSignature/AutomationTarget').get('Id')
        for env in self.ls.find('MasterTrack/AutomationEnvelopes/Envelopes'):
            pid = env.find('EnvelopeTarget/PointeeId').get('Value')
            evs = env.find('Automation/Events')
            if pid == tsig_id:
                evs[0].set('Value', str(code))
            elif pid == tempo_id:
                evs[0].set('Value', fmt(bpm0))
                # REAPER and Cubase tempo changes are steps: the old tempo
                # up to the change, the new one from it
                k = len(evs)
                prev = bpm0
                for s, bpm in self.tempo[1:]:
                    b = fmt(self.beats(s))
                    ET.SubElement(evs, 'FloatEvent', Id=str(k), Time=b, Value=fmt(prev))
                    ET.SubElement(evs, 'FloatEvent', Id=str(k + 1), Time=b, Value=fmt(bpm))
                    k += 2
                    prev = bpm
        if len(self.tempo) > 1:
            self.log.append('%d tempo change(s) written as tempo automation'
                            % (len(self.tempo) - 1))
        # markers: Live has locators, points only
        locs = self.ls.find('Locators/Locators')
        regions = 0
        points = []
        for mk in p.markers:
            points.append((mk.start, mk.name or ''))
            if mk.end is not None and mk.end > mk.start + 1e-6:
                # Live has locators, no regions (cycle markers): a region is
                # a locator at each edge, the end one named after it, which
                # is also how als_read finds the region again
                points.append((mk.end, '%s end' % (mk.name or 'Region')))
                regions += 1
        for t0, name in sorted(points, key=lambda x: x[0]):
            loc = ET.SubElement(locs, 'Locator', Id=str(len(locs)))
            ET.SubElement(loc, 'LomId', Value='0')
            ET.SubElement(loc, 'Time', Value=fmt(self.beats(t0)))
            ET.SubElement(loc, 'Name', Value=name)
            ET.SubElement(loc, 'Annotation', Value='')
            ET.SubElement(loc, 'IsSongStart', Value='false')
        if regions:
            self.log.append('%d region(s) / cycle marker(s): Live has no regions, so '
                            'each is a pair of locators, "<name>" at its start and '
                            '"<name> end" at its end' % regions)
        # the loop brace (switched off) over the whole song: it is what
        # Live's Export Audio/Video offers as the range to render
        end = max([i.pos + i.length for t in p.tracks for i in t.items]
                  + [m.end or m.start for m in p.markers] + [0.0])
        if end > 0:
            tr = self.ls.find('Transport')
            setv(tr, 'LoopStart', '0')
            setv(tr, 'LoopLength', fmt(math.ceil(self.beats(end) / 4.0) * 4.0))
            setv(tr, 'LoopOn', 'false')
        if abs(getattr(p, 'start', 0.0) or 0.0) > 1e-9:
            self.log.append('the project starts %.3f s before bar 1; Live\'s '
                            'song starts at bar 1, so that pre-roll is not '
                            'there' % -p.start)

    def mix(self, t):
        """(fader gain, pan, volume points, pan points) for the Live track
        that plays what the source channel played (panlaw.live_gains):
        the pan with the same L/R ratio, and the level difference on the
        fader - or, under a pan lane, riding on the volume lane."""
        from . import envelope, panlaw
        p = self.p
        source = getattr(p, 'pan_law_of', None)
        if source not in ('reaper', 'cubase'):
            return t.vol, t.pan or 0.0, t.volenv, t.panenv
        law = p.panlaw if p.panlaw is not None else 1.0
        mode = t.panmode if t.panmode is not None else p.panmode
        mono = bool(getattr(t, 'mono', False))
        code = getattr(p, 'panlaw_code', None) or 6
        if getattr(t, 'panenv_law', None) == 'balance':
            # an item's own pan curve, moved onto the track: REAPER's take
            # pan is a plain balance - the law of Cubase's stereo panner
            source, mono = 'cubase', False

        def to_live(x):
            return panlaw.to_live(x, source, law, mode, mono, code)
        if not t.panenv:
            lp, g = to_live(t.pan or 0.0)
            return (t.vol * g, lp, [(s, v * g) for s, v in t.volenv],
                    t.panenv)
        volenv = envelope.volume_with_pan_gain(t.volenv, t.vol, t.panenv,
                                               lambda x: to_live(x)[1])
        if source == 'reaper':
            # the same angle: the values carry over as they are
            panenv = list(t.panenv)
        else:
            if mono:
                back = lambda q: panlaw._pan_from_gains_cubase_mono(
                    *panlaw.live_gains(q), law_code=code)[0]
            else:
                back = lambda q: panlaw._pan_from_gains_cubase(*panlaw.live_gains(q))[0]
            pts = envelope.pan_curve(t.panenv, lambda x: to_live(x)[0], back)
            panenv = [(s, to_live(v)[0]) for s, v in pts]
        return t.vol, to_live(t.panenv[0][1])[0], volenv, panenv

    def vol(self, g, who):
        g = float(g if g is not None else 1.0)
        if g > LIVE_VOL_MAX + 1e-9:
            self.log.append('%s: level %+.2f dB is past Live\'s fader (+6.02 '
                            'dB) and is set to +6.02' % (who, 20 * math.log10(g)))
            return LIVE_VOL_MAX
        return max(LIVE_VOL_MIN, g) if g > 1e-9 else LIVE_VOL_MIN

    # --------------------------------------------------------- devices
    def plugin_device(self, f, instrument):
        """A VST3 plug-in as Live's PluginDevice, carrying the plug-in's own
        saved state - the IComponent and IEditController blobs REAPER and
        Cubase keep (Fx.component / Fx.controller), which Live keeps as
        ProcessorState / ControllerState, hex. The class id goes in as the
        four big-endian signed 32-bit words Live writes (the 32-hex uid read
        straight through: ED57BD72... -> -313016974, measured off a Set Live
        saved with Pro-Q 4). None for anything that is not a VST3 with an
        id."""
        import struct
        uid = (f.uid or '').strip()
        if len(uid) != 32 or getattr(f, 'native', False):
            return None
        if is_vst2(f) and getattr(f, 'format', ''):
            return None                     # REAPER's VST2 (vst2_device)
        if f.is_vst2 and not uid.upper().startswith('565354'):
            return None
        # A VST2 Cubase saved is named by the class id of its VST3 build
        # ('VST' + VST2 id + name) and its state is what that build reads
        # (Valhalla's 'VstW' wrapper, Kontakt's own) - the VST3 is the one
        # that loads, as REAPER loads ValhallaRoom and Kontakt 8 from it
        if getattr(f, 'param_dump', False) or not f.component:
            # a REAPER VST2 kept only as its parameter values: its id is the
            # converter's VST2 stand-in ('VST' + 4cc + name), not a VST3
            # class, and there is no VST3 state to give it. Written as a
            # VST3 it made Live fail to restore it and then crash
            return None
        try:
            words = struct.unpack('>4i', bytes.fromhex(uid))
        except ValueError:
            return None
        d = copy.deepcopy(self.proto_inst if instrument else self.proto_fx)
        self.ids.renumber(d)
        d.attrib.pop('role', None)
        self.dev_id += 1
        d.set('Id', str(self.dev_id))
        info = d.find('PluginDesc/Vst3PluginInfo')
        setv(info, 'Name', f.name or '')
        for k, w in enumerate(words):
            setv(info, 'Uid/Fields.%d' % k, str(w))
        pre = info.find('Preset/Vst3Preset')
        for k, w in enumerate(words):
            setv(pre, 'Uid/Fields.%d' % k, str(w))
        pre.find('ProcessorState').text = (f.component or b'').hex().upper()
        pre.find('ControllerState').text = (f.controller or b'').hex().upper()
        on = 'false' if f.bypass else 'true'
        setv(d, 'On/Manual', on)
        setv(pre, 'IsOn', on)
        setv(d, 'UserName', '')
        if not f.component:
            self.stats['stateless'] = self.stats.get('stateless', []) + [f.name]
        return d

    CUBASE_STOCK = ('Chorus', 'WahWah', 'Compressor', 'StereoDelay', 'Limiter', 'Frequency',
                    'Brickwall Limiter', 'Gate', 'RoomWorks', 'Octaver', 'Volume',
                    'StereoEnhancer', 'MonoToStereo', 'EnvelopeShaper', 'REVerence')

    def stock_device(self, role):
        """A fresh copy of one of Live's own effects (live_stock)."""
        proto = self.protos.find("*[@role='%s']" % role)
        d = copy.deepcopy(proto)
        d.attrib.pop('role', None)
        self.ids.renumber(d)
        return d

    def stock_for(self, f):
        """[(device, how close)] when `f` is a stock effect of REAPER or
        Cubase that one of Live's own plays, else None."""
        from . import live_stock
        orig = getattr(f, 'reaper_stock', None)
        if orig:
            return live_stock.from_reaper(self, orig[0], orig[1])
        js = getattr(f, 'reaper_js', None)
        if js:
            got = live_stock.from_js(self, js[0], js[1])
            if got:
                return got if isinstance(got, list) else [got]
        if getattr(f, 'native', False) and (f.name or '') in live_stock.REAPER_MAP:
            return live_stock.from_reaper(self, f.name,
                                          getattr(f, 'raw_state', None) or f.component)
        if getattr(f, 'native', False) and (f.name or '').startswith('ReaEQ'):
            # a ReaEQ anywhere in the chain (one last in it is the channel
            # EQ, written apart): EQ Eight, band for band
            from . import chan_eq
            rb = chan_eq.reaeq_state_bands(getattr(f, 'raw_state', None) or f.component)
            return [live_stock.eq8(self, rb)] if rb else None
        if getattr(f, 'native', False) and (f.name or '').startswith('JS: '):
            # REAPER's volume / width JS effects: Live's Utility
            got = live_stock.from_js(self, f.name[4:].strip(), self.js_sliders(f))
            return (got if isinstance(got, list) else [got]) if got else None
        # one of Cubase's own effects - from a Cubase project, or what the
        # REAPER reader made of REAPER's own (its JS volume/width, ReaComp...)
        if ((f.name or '') in self.CUBASE_STOCK or (f.uid or '').upper() in self.cubase_uids()) and (
                getattr(self.p, 'pan_law_of', None) == 'cubase'
                or (f.uid or '').upper() in self.cubase_uids()):
            return live_stock.from_cubase(self, f, self.p)
        return None

    @staticmethod
    def js_sliders(f):
        """A JS effect's slider values from the block the REAPER reader kept."""
        for e in (getattr(f, 'raw_group', None) or []):
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

    _CUBASE_UIDS = None

    @classmethod
    def cubase_uids(cls):
        """The class ids of Cubase's own effects the converter knows."""
        if cls._CUBASE_UIDS is None:
            import json
            from . import builtins, stock
            ids = {k.upper() for k in builtins.TABLE}
            ids |= {builtins.VOLUME_UID, builtins.STEREO_ENHANCER_UID}
            ids |= {getattr(stock, n) for n in dir(stock) if n.endswith('_UID')}
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cubase_templates.json')
            ids |= {(v.get('uid') or '').upper() for v in json.load(open(p)).values()}
            ids |= {(v.get('uid') or '').upper() for v in builtins.defaults().values()}
            cls._CUBASE_UIDS = {i for i in ids if i}
        return cls._CUBASE_UIDS

    VST2_DIRS = (r'C:\Program Files\Steinberg\VSTPlugins', r'C:\Program Files\VSTPlugins',
                 r'C:\Program Files\Common Files\VST2', r'C:\Program Files\Common Files\Steinberg\VST2')

    def vst2_path(self, name):
        """Where the VST2 build of `name` is installed, if anywhere (Live
        finds it again by its unique id; the path is what it tries first)."""
        want = (name or '').lower() + '.dll'
        for d in self.VST2_DIRS:
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                for f in files:
                    if f.lower() == want:
                        return os.path.join(root, f).replace('\\', '/')
        return None

    def vst2_device(self, f):
        """A VST2 that REAPER saved as its parameter list (DEADBEEF DEADF00D
        then every parameter as a normalised f32) as Live's own VST2
        device. Live keeps such a plug-in's state as the VST program
        itself - a 28-byte name, then the parameter values (read off a Set
        Live saved with Pro-C 2's VST2 build: 212 bytes, 46 parameters) -
        so the values go across exactly, nothing re-hosted. The plug-in's
        id is the four characters in the converter's VST2 stand-in id
        ('VST' + id + name)."""
        import struct
        raw = getattr(f, 'raw_state', None) or b''
        dump = raw[:8] == bytes.fromhex('EFBEADDE0DF0ADDE')
        if dump and len(raw) < 12:
            return None
        if not dump and not raw:
            return None
        vals = raw[8:8 + (len(raw) - 8) // 4 * 4] if dump else b''
        uid = (f.uid or '').strip()
        try:
            cc = bytes.fromhex(uid)[3:7]
        except ValueError:
            return None
        if len(cc) != 4:
            return None
        unique = struct.unpack('>i', cc)[0]
        name = f.name or ''
        d = copy.deepcopy(self.proto_vst2)
        d.attrib.pop('role', None)
        self.ids.renumber(d)
        self.dev_id += 1
        d.set('Id', str(self.dev_id))
        info = d.find('PluginDesc/VstPluginInfo')
        path = self.vst2_path(name)
        if path is None:
            self.log.append('%r: its VST2 build is not installed here; Live will '
                            'look for it by its id' % name)
        setv(info, 'Path', path or ('%s.dll' % name))
        setv(info, 'PlugName', name)
        setv(info, 'UniqueId', str(unique))
        pre = info.find('Preset/VstPreset')
        setv(pre, 'UniqueId', str(unique))
        setv(pre, 'IsOn', 'false' if f.bypass else 'true')
        if dump:
            setv(info, 'NumberOfParameters', str(len(vals) // 4))
            setv(pre, 'ParameterCount', str(len(vals) // 4))
            pre.find('Buffer').text = (b'\0' * 28 + vals).hex().upper()
        else:
            # the plug-in's own chunk, as REAPER keeps it, is what Live
            # keeps too: type 'FBCh', the chunk as the buffer (Saturn 2's
            # VST2: Live's buffer and REAPER's were the same 3871 bytes)
            setv(pre, 'Type', str(struct.unpack('>i', b'FBCh')[0]))
            setv(info, 'Flags', '1284')
            pre.find('Buffer').text = raw.hex().upper()
        setv(d, 'On/Manual', 'false' if f.bypass else 'true')
        return d

    def gain_device(self, gain):
        """Live's Utility at `gain` (linear, up to +35 dB): what a level past
        Live's fader needs, after the plug-ins, before the fader - where
        REAPER's fader gain sits."""
        d = copy.deepcopy(self.proto_gain)
        self.ids.renumber(d)
        self.dev_id += 1
        d.set('Id', str(self.dev_id))
        setv(d, 'Gain/Manual', fmt(min(56.2341309, gain)))
        return d

    def automate_plugin(self, d, f, envs):
        """A plug-in's parameter lanes. REAPER keeps a lane by the
        parameter's place in the plug-in's list, Live by its VST3 id, in one
        of the device's 128 parameter slots; vst3params reads the list
        from the installed plug-in (as for Cubase). Values are the
        normalised 0..1 both hosts draw straight lines in."""
        from . import vst3params
        lanes = list(f.envelopes or [])
        if not lanes:
            return 0
        slots = list(d.find('ParameterList'))
        n = 0
        for k, (pidx, pts) in enumerate(lanes):
            if k >= len(slots) or not pts:
                break
            try:
                pid = vst3params.index_to_id(f.uid, int(pidx), f.name, self.log)
            except Exception:
                pid = None
            if pid is None:
                self.log.append('%r: parameter %d has no VST3 id here (the '
                                'plug-in could not be read), its lane is left out'
                                % (f.name, pidx))
                continue
            s = slots[k]
            setv(s, 'ParameterId', str(int(pid)))
            setv(s, 'VisualIndex', str(k))
            setv(s, 'ParameterValue/Manual', fmt(min(1.0, max(0.0, pts[0][1]))))
            target = s.find('ParameterValue/AutomationTarget').get('Id')
            envelope(envs, target, [(self.beats(t), min(1.0, max(0.0, v)))
                                    for t, v in pts], len(envs))
            n += 1
        return n

    def write_chain(self, t, dc, envs, fader_gain):
        """The track's device chain: its instrument first, then its
        inserts in order, then a Utility for any level past the fader.
        Returns what could not be placed, by kind."""
        devs = dc.find('DeviceChain/Devices')
        miss = []
        chain = ([(t.instrument, True)] if t.instrument is not None else []) \
            + [(f, getattr(f, 'is_instrument', False)) for f in t.fx]
        # the source's own order (REAPER's chain position)
        chain.sort(key=lambda fi: getattr(fi[0], 'chain_pos', 0) or 0)
        n_inst = sum(1 for _f, i in chain if i)

        def place(target, f, inst):
            if getattr(f, 'cubase_only', False):
                return
            native = self.stock_for(f)
            if native is not None:
                for dev, how in native:
                    self.dev_id += 1
                    dev.set('Id', str(self.dev_id))
                    if f.bypass:
                        setv(dev, 'On/Manual', 'false')
                    target.append(dev)
                    self.stock_notes.append('%s -> %s' % (f.name, how))
                return
            from . import builtins
            if builtins.name_of_uid(f.uid) and not getattr(f, 'native', False):
                # one of Cubase's own effects with nothing like it in Live:
                # a VST3 of that id would only stand there missing
                self.log.append("%s on %r is one of Cubase's own effects and Live has nothing like "
                                "it; left out - render the track in place in Cubase before converting "
                                "to keep its sound" % (f.name, t.name))
                return
            d = self.plugin_device(f, inst)
            if d is None and is_vst2(f):
                d = self.vst2_device(f)
                if d is not None:
                    self.stats['vst2'] = self.stats.get('vst2', 0) + 1
                    target.append(d)
                    return
            if d is None:
                miss.append(f)
                return
            target.append(d)
            self.stats['plugins'] = self.stats.get('plugins', 0) + 1
            self.stats['plugin_lanes'] = self.stats.get('plugin_lanes', 0) + \
                self.automate_plugin(d, f, envs)
        if n_inst > 1:
            # Several instruments in one chain: REAPER sums them (measured,
            # model.last_synth_only), and what sits after one processes it
            # and everything before it. Live's Instrument Rack is that: one
            # chain per instrument, holding the effects that follow it up to
            # the next instrument, and the effects after the last one after
            # the rack. The track stays whole - its inserts, fader and sends
            # act on the sum, as they did.
            first = next(k for k, (_f, i) in enumerate(chain) if i)
            if first:
                self.log.append('%r: %d effect(s) sit before the first instrument, '
                                'where REAPER feeds them nothing; Live has no '
                                'place for an audio effect there, so they are '
                                'left out' % (t.name, first))
            last = max(k for k, (_f, i) in enumerate(chain) if i)
            rack = copy.deepcopy(self.proto_rack)
            self.ids.renumber(rack)
            self.dev_id += 1
            rack.set('Id', str(self.dev_id))
            branches = rack.find('Branches')
            proto_br = branches[0]
            branches.remove(proto_br)
            groups, cur = [], None
            for f, inst in chain[first:last + 1]:
                if inst:
                    cur = [f]
                    groups.append(cur)
                else:
                    cur.append(f)
            # the effects between the last instrument and the end are after
            # the rack; the ones that follow the last instrument directly are
            # part of 'after' too, so the last chain holds only its synth
            groups[-1] = groups[-1][:1]
            for k, g in enumerate(groups):
                br = copy.deepcopy(proto_br)
                self.ids.renumber(br)
                br.set('Id', str(k))
                # a chain's Name holds EffectiveName/UserName, like a track's
                setv(br, 'Name/EffectiveName', g[0].name or 'Chain %d' % (k + 1))
                setv(br, 'Name/UserName', g[0].name or '')
                setv(br, 'IsSelected', 'true' if k == 0 else 'false')
                inner = br.find('DeviceChain/MidiToAudioDeviceChain/Devices')
                for j, f in enumerate(g):
                    place(inner, f, j == 0)
                branches.append(br)
            devs.append(rack)
            self.stats['racks'] = self.stats.get('racks', 0) + 1
            for f, inst in chain[last + 1:]:
                place(devs, f, False)
        else:
            for f, inst in chain:
                place(devs, f, inst)
        # an EQ at the end of the chain, where both hosts play it: REAPER's
        # last ReaEQ (its own bands, kept by the reader) or Cubase's
        # channel EQ (its four bands, as ReaEQ bands - chan_eq)
        bands = getattr(t, 'reaeq_bands', None)
        if not bands and getattr(t, 'chan_eq', None):
            from . import chan_eq
            bands = chan_eq.reaeq_bands(t.chan_eq)
        if bands:
            from . import live_stock
            dev, how = live_stock.eq8(self, bands)
            self.dev_id += 1
            dev.set('Id', str(self.dev_id))
            devs.append(dev)
            self.stock_notes.append('EQ -> %s' % how)
        if fader_gain > LIVE_VOL_MAX + 1e-9:
            devs.append(self.gain_device(fader_gain / LIVE_VOL_MAX))
            self.log.append('%r: its level (%+.2f dB) is past Live\'s fader (+6.02 '
                            'dB); the fader is at +6.02 and a Utility at the end '
                            'of the chain adds the other %+.2f dB'
                            % (t.name, 20 * math.log10(fader_gain),
                               20 * math.log10(fader_gain / LIVE_VOL_MAX)))
        return miss

    def write_sends(self, t, mixer, envs, own_return=None):
        """One send slot per return track on every track, as Live keeps
        them: the level of the source's send to that return (linear, Live's
        send stops at 0 dB), off where there is none."""
        sends = mixer.find('Sends')
        level = {}
        for s in t.sends:
            if getattr(s, 'mute', False):
                continue                    # a muted send sends nothing
            if s.dest in self.return_pos:
                k = self.return_pos[s.dest]
                level[k] = level.get(k, 0.0) + (s.vol if s.vol is not None else 1.0)
                if abs(getattr(s, 'pan', 0.0) or 0.0) > 1e-6:
                    self.log.append('%r: a send pan has no place in Live; the '
                                    'send is centred' % t.name)
        for k in range(len(self.returns)):
            h = copy.deepcopy(self.proto_send)
            self.ids.renumber(h)
            h.set('Id', str(k))
            g = level.get(k, 0.0)
            if g > 1.0 + 1e-9:
                self.log.append('%r: a send at %+.2f dB is past Live\'s send '
                                '(0 dB) and is set to 0 dB'
                                % (t.name, 20 * math.log10(g)))
                g = 1.0
            setv(h, 'Send/Manual', fmt(max(LIVE_VOL_MIN, g)))
            setv(h, 'Active', 'false' if own_return == k else 'true')
            sends.append(h)

    # ---------------------------------------------------------- tracks
    def route_outputs(self, written):
        """A Cubase channel whose output is a group channel other than the
        folder it sits in: Live's 'Audio To' that track, into its Track In,
        as Cubase does - not on to Master past the group's effects
        (SuperThunderCrown: five groups silent). Live hears what arrives at
        a track's input only while it monitors In, so the group does; it
        has no clips of its own to lose by that."""
        from .model import output_routes
        for i, g in output_routes(self.p):
            if i not in written or g not in written:
                continue
            gt = self.p.tracks[g]
            if getattr(gt, 'live_return', False):
                self.log.append('%r outputs to %r, a return in Live, which no '
                                'track can output to; it goes to its own group '
                                'or Master' % (self.p.tracks[i].name, gt.name))
                continue
            src, _tid = written[i]
            dst, gid = written[g]
            out = src.find('DeviceChain/AudioOutputRouting')
            setv(out, 'Target', 'AudioOut/Track.%d/TrackIn' % gid)
            setv(out, 'UpperDisplayString', gt.name or '')
            setv(out, 'LowerDisplayString', 'Track In')
            mon = dst.find('.//MonitoringEnum')
            if any(it.kind == 'audio' for it in gt.items):
                self.log.append('%r gets the output of other tracks and has '
                                'clips of its own; Live plays only one of the '
                                'two - the clips' % gt.name)
            elif mon is not None:
                mon.set('Value', '0')
            self.stats['routed'] = self.stats.get('routed', 0) + 1

    def write_tracks(self):
        from . import envelope as _env
        p = self.p
        self.returns = [i for i, t in enumerate(p.tracks) if getattr(t, 'live_return', False)]
        self.return_pos = {i: k for k, i in enumerate(self.returns)}
        order = [i for i in range(len(p.tracks)) if i not in self.return_pos] + self.returns
        parents = []                     # stack of (depth, group id)
        soloed = ccs = 0
        missing = []
        written = {}                     # model index -> (element, Live id)
        for i in order:
            t = p.tracks[i]
            is_ret = i in self.return_pos
            while parents and parents[-1][0] >= t.depth:
                parents.pop()
            parent = -1 if is_ret else (parents[-1][1] if parents else -1)
            if is_ret:
                tag = 'ReturnTrack'
            elif t.is_folder:
                tag = 'GroupTrack'
            elif t.kind == 'midi' or t.instrument is not None \
                    or any(it.kind == 'midi' for it in t.items):
                tag = 'MidiTrack'
            else:
                tag = 'AudioTrack'
            tr = copy.deepcopy(self.proto[tag])
            self.ids.renumber(tr)
            tid = self.ids.take()
            tr.set('Id', str(tid))
            setv(tr, 'Name/EffectiveName', t.name or '')
            setv(tr, 'Name/UserName', t.name or '')
            color = live_color(t.color)
            setv(tr, 'Color', str(color))
            setv(tr, 'TrackGroupId', str(parent))
            setv(tr, 'TrackUnfolded', 'true')
            if abs(t.delay or 0.0) > 1e-9:
                setv(tr, 'TrackDelay/Value', fmt(t.delay * 1000.0))
                setv(tr, 'TrackDelay/IsValueSampleBased', 'false')
            dc = tr.find('DeviceChain')
            # always written: a track out of no group routed 'to its group'
            # (what a record copied from a grouped track says) crashed Live
            out = dc.find('AudioOutputRouting')
            if parent != -1:
                setv(out, 'Target', 'AudioOut/GroupTrack')
                setv(out, 'UpperDisplayString', 'Group')
            else:
                setv(out, 'Target', 'AudioOut/Master')
                setv(out, 'UpperDisplayString', 'Master')
            setv(out, 'LowerDisplayString', '')
            mixer = dc.find('Mixer')
            vol, pan, volenv, panenv = self.mix(t)
            envs = tr.find('AutomationEnvelopes/Envelopes')
            # a level past Live's fader: the fader at +6.02, the rest on a
            # Utility; a lane that goes past it is scaled down by as much
            peak = max([vol] + [v for _s, v in (volenv or [])])
            over = peak / LIVE_VOL_MAX if peak > LIVE_VOL_MAX + 1e-9 else 1.0
            missing += [(t.name, f) for f in
                        self.write_chain(t, dc, envs, vol if over == 1.0 else peak)]
            vol = vol / over
            volenv = [(s_, v / over) for s_, v in (volenv or [])]
            if volenv:
                # the model's ramps are straight in gain, Live's in dB
                volenv = _env.volume_for_live(volenv)
            setv(mixer, 'Volume/Manual', self.vol(vol, repr(t.name)))
            setv(mixer, 'Pan/Manual', max(-1.0, min(1.0, pan)))
            setv(mixer, 'Speaker/Manual', 'false' if t.mute else 'true')
            if volenv:
                envelope(envs, mixer.find('Volume/AutomationTarget').get('Id'),
                         [(self.beats(s_), self.vol(v, repr(t.name)))
                          for s_, v in volenv], len(envs))
            if panenv:
                envelope(envs, mixer.find('Pan/AutomationTarget').get('Id'),
                         [(self.beats(s_), max(-1.0, min(1.0, v)))
                          for s_, v in panenv], len(envs))
            self.write_sends(t, mixer, envs, self.return_pos.get(i))
            soloed += 1 if t.solo else 0
            if tag == 'AudioTrack':
                evs = dc.find('MainSequencer/Sample/ArrangerAutomation/Events')
                for it in t.items:
                    c = self.audio_clip(it, color)
                    if c is not None:
                        evs.append(c)
            elif tag == 'MidiTrack':
                evs = dc.find('MainSequencer/ClipTimeable/ArrangerAutomation/Events')
                for it in t.items:
                    if it.kind == 'midi':
                        evs.append(self.midi_clip(it, color))
                        ccs += len(it.ccs or ())
            self.tracks.append(tr)
            written[i] = (tr, tid)
            if is_ret:
                self.stats['returns'] = self.stats.get('returns', 0) + 1
            elif t.is_folder:
                parents.append((t.depth, tid))
                self.stats['groups'] += 1
            else:
                self.stats['tracks'] += 1
        self.route_outputs(written)
        # pre or post is set per return in Live, not per send
        pre = self.ls.find('SendsPre')
        for k, ri in enumerate(self.returns):
            modes = {s.mode for t in p.tracks for s in t.sends if s.dest == ri}
            is_pre = bool(modes) and all(m in (1, 3) for m in modes)
            if len(modes) > 1 and not is_pre and any(m in (1, 3) for m in modes):
                self.log.append('%r gets pre- and post-fader sends; Live sets '
                                'that per return, so all are post-fader'
                                % p.tracks[ri].name)
            ET.SubElement(pre, 'SendPreBool', Id=str(k),
                          Value='true' if is_pre else 'false')
        self.write_master(missing)
        if missing:
            names = sorted({'%s (%s)' % (f.name or '?', n) for n, f in missing})
            self.log.append('%d plug-in(s) are not VST3 with saved settings Live '
                            'can load, and are left out: %s'
                            % (len(missing), ', '.join(names)[:300]))
        if self.stats.get('stateless'):
            self.log.append('%d plug-in(s) had no saved settings and arrive on '
                            'their defaults: %s'
                            % (len(self.stats['stateless']),
                               ', '.join(self.stats['stateless'])[:200]))
        if soloed:
            self.log.append('%d soloed track(s): solo is not written' % soloed)
        if ccs:
            self.log.append('%d MIDI CC / pitch bend event(s) are not carried '
                            'over yet; the notes are' % ccs)

    def write_master(self, missing):
        """The master: its plug-ins, fader, pan and volume lane."""
        from . import envelope as _env
        m = getattr(self.p, 'master', None)
        if m is None:
            return
        mt = self.ls.find('MasterTrack')
        dc = mt.find('DeviceChain')
        mixer = dc.find('Mixer')
        envs = mt.find('AutomationEnvelopes/Envelopes')
        peak = max([m.vol] + [v for _s, v in (m.volenv or [])])
        over = peak / LIVE_VOL_MAX if peak > LIVE_VOL_MAX + 1e-9 else 1.0
        m.instrument = None
        missing += [('master', f) for f in
                    self.write_chain(m, dc, envs, m.vol if over == 1.0 else peak)]
        g = 1.0
        if abs(m.pan or 0.0) > 1e-6:
            from . import panlaw
            lp, g = panlaw.to_live(m.pan, getattr(self.p, 'pan_law_of', None) or 'reaper',
                                   self.p.panlaw if self.p.panlaw is not None else 1.0,
                                   self.p.panmode)
            setv(mixer, 'Pan/Manual', fmt(lp))
        setv(mixer, 'Volume/Manual', self.vol(m.vol / over * g, 'the master'))
        if m.volenv:
            pts = _env.volume_for_live([(s_, v / over) for s_, v in m.volenv])
            envelope(envs, mixer.find('Volume/AutomationTarget').get('Id'),
                     [(self.beats(s_), self.vol(v, 'the master')) for s_, v in pts],
                     len(envs) + 10)

    # ----------------------------------------------------------- clips
    def _clip_common(self, c, it, b0, b1, color):
        self.clip_id += 1
        c.set('Id', str(self.clip_id))
        c.set('Time', fmt(b0))
        setv(c, 'CurrentStart', fmt(b0))
        setv(c, 'CurrentEnd', fmt(b1))
        setv(c, 'Name', it.name or '')
        # the item's own colour when it has one, else the track's
        setv(c, 'Color', str(live_color(it.color) if getattr(it, 'color', None) else color))
        setv(c, 'Disabled', 'true' if it.mute else 'false')
        setv(c, 'Loop/LoopOn', 'false')
        setv(c, 'Loop/StartRelative', '0')

    def audio_clip(self, it, color):
        if it.kind != 'audio':
            return None
        f = media.resolve(it.file, self.p.srcdir) if it.file else None
        frames, rate = sample_info(f) if f and os.path.isfile(f) else (None, None)
        if not frames:
            self.stats['missing'] += 1
        c = copy.deepcopy(self.proto_aclip)
        pos, s0 = it.pos, it.soffs or 0.0
        sr = float(self.p.samplerate or 48000)
        plain = (abs((it.playrate or 1.0) - 1.0) < 1e-9 and abs(it.pitch or 0.0) < 1e-9
                 and len(getattr(it, 'stretch_markers', None) or []) < 2)
        if plain and rate and abs(rate - sr) < 1.0 \
                and getattr(self.p, 'pan_law_of', None) == 'reaper' \
                and not os.environ.get('CPR_NO_SAMPLE_SNAP'):
            # Whole samples, as cpr_build does for Cubase: an item that
            # starts between two samples plays in REAPER from the nearest
            # one, with the file offset rounded the same way; Live places a
            # clip at the exact fraction. TPA's shaker, every hit 0.86 of a
            # sample in, nulled at -5 dB for it.
            p = pos * sr
            pi = int(math.floor(p + 0.5))
            dd = int(math.floor(s0 * sr - p + 0.5))
            if pi + dd >= 0:
                pos, s0 = pi / sr, (pi + dd) / sr
        b0 = self.beats(pos)
        b1 = self.beats(pos + it.length)
        self._clip_common(c, it, b0, b1, color)
        s1 = s0 + it.length * (it.playrate or 1.0)
        dur = frames / float(rate) if frames else s1
        pitch = it.pitch or 0.0
        stretched = (abs((it.playrate or 1.0) - 1.0) > 1e-6
                     or len(getattr(it, 'stretch_markers', None) or []) >= 2)
        warped = stretched or abs(pitch) > 1e-6
        wm = c.find('WarpMarkers')
        for m in list(wm):
            wm.remove(m)
        if not warped:
            # unwarped: the clip's own time is seconds of the file, and it
            # plays at the file's own speed whatever the tempo does
            setv(c, 'Loop/LoopStart', fmt(s0))
            setv(c, 'Loop/LoopEnd', fmt(s1))
            setv(c, 'Loop/OutMarker', fmt(max(dur, s1)))
            setv(c, 'Loop/HiddenLoopStart', '0')
            setv(c, 'Loop/HiddenLoopEnd', fmt(max(dur, s1)))
            setv(c, 'IsWarped', 'false')
            bps = self.bpm_at(it.pos) / 60.0
            ET.SubElement(wm, 'WarpMarker', Id='0', SecTime='0', BeatTime='0')
            ET.SubElement(wm, 'WarpMarker', Id='1', SecTime=fmt(1.0 / 32 / bps),
                          BeatTime='0.03125')
        else:
            # A stretch or a pitch shift is Live's own warp: markers that tie
            # the file to the beats where the source item played it - at
            # the item's ends, every stretch marker and every tempo change
            # inside it - so the clip lasts and lands exactly as the item
            # did. An item that is only transposed gets markers at its own
            # speed: nothing is stretched, Transpose needs warp to work.
            # The clip's beat 0 is the item's start.
            span = b1 - b0
            marks = warp_markers(it, self.beats, self.tempo)
            for k, (s, b) in enumerate(marks):
                ET.SubElement(wm, 'WarpMarker', Id=str(k), SecTime=fmt(s),
                              BeatTime=fmt(b))
            setv(c, 'Loop/LoopStart', '0')
            setv(c, 'Loop/LoopEnd', fmt(span))
            setv(c, 'Loop/OutMarker', fmt(span))
            setv(c, 'Loop/HiddenLoopStart', '0')
            setv(c, 'Loop/HiddenLoopEnd', fmt(span))
            setv(c, 'IsWarped', 'true')
            # REAPER's 'preserve pitch' off is a tape-speed change: Re-Pitch.
            # Otherwise Complex Pro, Live's highest-quality stretch
            setv(c, 'WarpMode', '3' if (stretched and not it.preserve_pitch) else '6')
            # Complex Pro keeps formants at 100 by default, which reshapes a
            # transposed sound: a +3 st tone came out 5.8 dB quiet. REAPER's
            # shifter keeps no formants, so neither does the clip
            setv(c, 'ComplexProFormants', '0')
            coarse = int(round(pitch))
            setv(c, 'PitchCoarse', str(coarse))
            setv(c, 'PitchFine', fmt(round((pitch - coarse) * 100.0, 3)))
            self.stats['warped'] = self.stats.get('warped', 0) + 1
        ref = c.find('SampleRef')
        if f:
            file_ref(ref.find('FileRef'), f, self.set_dir)
            try:
                setv(ref, 'LastModDate', str(int(os.path.getmtime(f))))
            except OSError:
                pass
        setv(ref, 'DefaultDuration', str(frames or int(dur * 48000)))
        setv(ref, 'DefaultSampleRate', str(rate or 48000))
        setv(c, 'SampleVolume', fmt(max(0.0, it.gain if it.gain is not None else 1.0)))
        if not warped:
            setv(c, 'PitchCoarse', '0')
            setv(c, 'PitchFine', '0')
        self.write_fades(c, it, warped)
        self.stats['audio_clips'] += 1
        return c

    def write_fades(self, c, it, warped):
        """The item's fades as the clip's own: the length in the clip's time
        (seconds unwarped, beats warped - measured: an unwarped clip's 20 s
        fade-in lasted 20 s), and the Live curve nearest the item's shape.
        Live's curves are all S-shaped with flat ends (live_fades.json), so
        a straight fade cannot be matched exactly; the nearest is written
        and the gap is reported."""
        fades = c.find('Fades')
        for key, secs, tag in (('in', it.fadein, 'FadeIn'), ('out', it.fadeout, 'FadeOut')):
            secs = max(0.0, secs or 0.0)
            if warped and secs > 0:
                if key == 'in':
                    ln = self.beats(it.pos + secs) - self.beats(it.pos)
                else:
                    end = it.pos + it.length
                    ln = self.beats(end) - self.beats(end - secs)
            else:
                ln = secs
            setv(fades, tag + 'Length', fmt(ln))
            setv(fades, 'IsDefault' + tag, 'false')
            sk = sl = 0.0
            if secs > 1e-4:
                sk, sl, err = fit_fade(source_fade(it, key), key)
                self.fade_err = max(getattr(self, 'fade_err', 0.0), err)
                self.stats['fades'] = self.stats.get('fades', 0) + 1
            setv(fades, tag + 'CurveSkew', fmt(sk))
            setv(fades, tag + 'CurveSlope', fmt(sl))

    def midi_clip(self, it, color):
        c = copy.deepcopy(self.proto_mclip)
        b0 = self.beats(it.pos)
        b1 = self.beats(it.pos + it.length)
        self._clip_common(c, it, b0, b1, color)
        span = b1 - b0
        setv(c, 'Loop/LoopStart', '0')
        setv(c, 'Loop/LoopEnd', fmt(span))
        setv(c, 'Loop/OutMarker', fmt(span))
        setv(c, 'Loop/HiddenLoopStart', '0')
        setv(c, 'Loop/HiddenLoopEnd', fmt(span))
        ppq = it.ppq or 480.0
        keys = {}
        for n in it.notes:
            pos, ln, _ch, pitch, vel = n[:5]
            keys.setdefault(int(pitch), []).append((pos / ppq, max(ln, 1.0) / ppq,
                                                   int(vel)))
        kts = c.find('Notes/KeyTracks')
        nid = 1
        for k, pitch in enumerate(sorted(keys)):
            kt = ET.SubElement(kts, 'KeyTrack', Id=str(k))
            notes = ET.SubElement(kt, 'Notes')
            for t0, ln, vel in sorted(keys[pitch]):
                ET.SubElement(notes, 'MidiNoteEvent', Time=fmt(t0), Duration=fmt(ln),
                              Velocity=fmt(max(1, min(127, vel))),
                              VelocityDeviation='0', OffVelocity='64',
                              Probability='1', IsEnabled='true', NoteId=str(nid))
                nid += 1
            ET.SubElement(kt, 'MidiKey', Value=str(pitch))
        setv(c, 'Notes/NoteIdGenerator/NextId', str(nid))
        self.stats['midi_clips'] += 1
        self.stats['notes'] += nid - 1
        return c

    def save(self):
        self.ls.find('NextPointeeId').set('Value', str(self.ids.next))
        data = ET.tostring(self.root, encoding='unicode')
        data = '<?xml version="1.0" encoding="UTF-8"?>\n' + data
        with gzip.open(self.path, 'wb') as f:
            f.write(data.encode('utf-8'))


def write(project, path, log=None):
    """Build a Live Set matching `project` at `path`. Returns stats."""
    log = log if log is not None else []
    if not os.path.exists(DONOR):
        raise SystemExit('no Live template to build from: %s' % DONOR)
    prepare(project, path, log)
    live_readable(project, path, log)
    group_inputs(project, log)
    prepare_returns(project, log)
    w = Writer(project, path, log)
    w.write_song()
    w.write_tracks()
    w.save()
    if w.stock_notes:
        from collections import Counter
        c = Counter(w.stock_notes)
        log.append('%d stock effect(s) became Live\'s own, with their settings: %s'
                   % (len(w.stock_notes), '; '.join('%s x%d' % (k, n) for k, n in sorted(c.items()))))
    if w.stats.get('fades'):
        log.append('%d fade(s) written as Live\'s own clip fades. Live\'s fade '
                   'curves are all S-shaped, so each has the nearest curve; the '
                   'worst is %.1f dB from the source at some point along it'
                   % (w.stats['fades'], getattr(w, 'fade_err', 0.0)))
    if w.stats.get('warped'):
        log.append('%d clip(s) are warped - a stretch, a pitch shift or stretch '
                   'markers - with warp markers that hold them where the source '
                   'played them' % w.stats['warped'])
    if w.stats['missing']:
        log.append('%d audio clip(s) point at files that were not found here; '
                   'Live will ask for them' % w.stats['missing'])
    return w.stats
