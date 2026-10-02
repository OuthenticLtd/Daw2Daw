"""Print REAPER items through REAPER itself.

Some of what an item does in REAPER cannot be written onto a Cubase event:
take FX (a plug-in chain inside the item), a stretch, a pitch shift, a
volume or pan curve drawn inside the item. Cubase has no per-event plug-ins
at all, so the only faithful way across is to print the audio the item
plays. ffmpeg can do the stretch, pitch and curves (media.render), but it
cannot run a VST; REAPER can, and it is installed wherever the .rpp came
from.

So one throwaway project is written holding every item to be printed, each
on its own track at its own stretch of time with the take's FX, envelopes,
stretch and pitch exactly as the original had them, a region over each, and
REAPER renders the regions to WAV files from the command line
(reaper -renderproject). The item's gain is left out: Cubase keeps that on
the event. Each region is named for the file it should produce.
"""
import os
import subprocess

from . import media

RENDER_WAV24 = 'ZXZhdxgAAQ=='          # REAPER's 24-bit WAV render config
# REAPER's WAV render configs, settled by rendering with each and reading
# the result's own fmt chunk back rather than by reading anything into the
# bytes: 'evaw' then a format byte, a flags byte and 0x01.
#   18 -> 24-bit integer     21 -> 32-bit integer     20 -> 32-bit FLOAT
# The integer one clips at full scale whatever its width. A print is
# rendered with the track's fader reset, because Cubase keeps the fader, so
# a track sitting well below 0 dB in REAPER can be far above it before its
# fader - and in an integer file that is baked in for good. Float has no
# ceiling, and Cubase plays it: the clip record says nothing about integer
# or float (it carries frames, depth, block align, channels, rate), so the
# format is read from the WAV header, and a project Cubase saved here plays
# a 32-bit file. 27 clipped samples on one shaker print is what this fixes.
RENDER_WAV32I = 'ZXZhdyEoAQ=='
RENDER_WAV32F = 'ZXZhdyAAAQ=='


def render_cfg():
    """The render format for prints: 32-bit float, which cannot clip.

    CPR_PRINT_24BIT=1 goes back to 24-bit integer; CPR_PRINT_CFG=<base64>
    uses any config given."""
    import os
    over = os.environ.get('CPR_PRINT_CFG')
    if over:
        return over.strip()
    if os.environ.get('CPR_PRINT_24BIT'):
        return RENDER_WAV24
    return RENDER_WAV32F


def clipped(path, limit=0.9999):
    """(clipped sample count, peak) for a WAV, or None if it cannot be read.

    The file's own fmt chunk says how wide a sample is and whether it holds
    integers or floats, and both have to be read rather than assumed: a
    24-bit file unpacked as floats reports a peak of 1e38 and a million
    clipped samples, none of it true. A float sample above 1.0 is loud, not
    damaged, so only an integer format can clip at all."""
    import struct
    try:
        raw = open(path, 'rb').read()
    except OSError:
        return None
    if raw[:4] != b'RIFF' or raw[8:12] != b'WAVE':
        return None
    o, tag, bits, data = 12, None, None, None
    while o + 8 <= len(raw):
        cid = raw[o:o + 4]
        size = struct.unpack_from('<I', raw, o + 4)[0]
        if cid == b'fmt ' and size >= 16:
            tag, _ch, _rate, _bps, _al, bits = struct.unpack_from(
                '<HHIIHH', raw, o + 8)
            if tag == 0xFFFE and size >= 40:      # extensible: the real tag
                tag = struct.unpack_from('<H', raw, o + 8 + 24)[0]
        elif cid == b'data':
            data = (o + 8, size)
            break
        o += 8 + size + (size & 1)
    if not data or tag is None:
        return None
    a, size = data
    body = raw[a:a + min(size, len(raw) - a)]
    peak, hits = 0.0, 0
    if tag == 3 and bits == 32:
        n = len(body) // 4
        for v in struct.unpack('<%df' % n, body[:n * 4]):
            av = abs(v)
            if av > peak:
                peak = av
        return (0, peak)                 # a float file cannot clip
    if tag != 1:
        return None
    if bits == 16:
        n = len(body) // 2
        vals = struct.unpack('<%dh' % n, body[:n * 2])
        scale = 32768.0
    elif bits == 24:
        vals = [int.from_bytes(body[i:i + 3], 'little', signed=True)
                for i in range(0, len(body) - 2, 3)]
        scale = 8388608.0
    elif bits == 32:
        n = len(body) // 4
        vals = struct.unpack('<%di' % n, body[:n * 4])
        scale = 2147483648.0
    else:
        return None
    for v in vals:
        av = abs(v) / scale
        if av > peak:
            peak = av
        if av >= limit:
            hits += 1
    return (hits, peak)


def q(s):
    s = (s or '').replace('\r', ' ').replace('\n', ' ')
    if '"' not in s:
        return '"%s"' % s
    if "'" not in s:
        return "'%s'" % s
    return '`%s`' % s.replace('`', "'")


def block_text(block, indent):
    """An rpp_read.Block back as project text."""
    out = ['%s<%s%s' % (indent, block.name,
                         ''.join(' ' + a for a in map(_tok, block.args)))]
    for entry in block.raw:
        if isinstance(entry, str):
            out.append(indent + '  ' + entry)
        else:
            out.extend(block_text(entry, indent + '  '))
    out.append(indent + '>')
    return out


def _tok(a):
    return q(a) if (' ' in a or a == '' or '"' in a) else a


def needs_reaper(it):
    """Only REAPER can print this item: it carries take FX."""
    return bool(getattr(it, 'takefx', None))


def render(items, out_dir, rate, log, reaper=None, gap=2.0, channels=None,
           header=(), tempo=None):
    """Print `items` to <out_dir>/<name>.wav with REAPER.

    `header` is the source project's header lines that change how an item
    plays and `tempo` its bpm: the print is a fresh project, and an item
    whose stretch mode is 'project default' (PLAYRATE mode -1, which is
    what REAPER writes for nearly every item) takes that default from the
    project's DEFPITCHMODE. Without it the print used REAPER's global
    default instead, and a stretched shaker printed with a different
    algorithm from the one REAPER plays the project with - same level,
    drifting phase, no null against REAPER's own render.

    Returns {item: path} for the ones that came out."""
    reaper = reaper or media.find_reaper()
    if not reaper:
        log.append('%d item(s) carry take FX, which only REAPER can print, '
                   'and reaper.exe was not found - they arrive without '
                   'their FX' % len(items))
        return {}
    os.makedirs(out_dir, exist_ok=True)
    lines = ['<REAPER_PROJECT 0.1 "7.79/win64" 0']
    given = set(h.strip().split()[0] for h in header if h and h.strip())
    if 'SAMPLERATE' not in given:
        lines.append('  SAMPLERATE %d 0 0' % int(rate))
    if 'TEMPO' not in given:
        lines.append('  TEMPO %s 4 4' % fmt(tempo or 120.0))
    lines += ['  ' + h.strip() for h in header if h and h.strip()]
    lines += [
             '  RENDER_FILE %s' % q(out_dir),
             '  RENDER_PATTERN $region',
             '  RENDER_FMT 0 %d %d' % (int(channels or 2), int(rate)),
             '  RENDER_1X 0',
             '  RENDER_RANGE 3 0 0 0 1000',
             '  RENDER_RESAMPLE 3 0 1',
             '  RENDER_ADDTOPROJ 0',
             '  RENDER_STEMS 0',
             '  RENDER_DITHER 0',
             '  RENDER_TRIM 0 0 0 0',
             '  <RENDER_CFG',
             '    ' + render_cfg(),
             '  >']
    head = lines
    names = {}
    taken = set(f.lower() for f in os.listdir(out_dir))
    # Every item is printed at its own place in the project. A pitch-shifted
    # take printed at some other time nulled against REAPER's render of the
    # project only to -5..-31 dB (the shifter's blocks fell differently on
    # the source; shifting by whole 65536-sample blocks did not help), at
    # its own time it is identical. Items whose spans would overlap - a
    # region render sums every track inside it - go into separate passes,
    # each its own throwaway project and REAPER run.
    placed = []
    for k, it in enumerate(items):
        stem = os.path.splitext(os.path.basename(it.file or 'item'))[0]
        n = 0
        while True:
            n += 1
            cand = '%s [printed%s]' % (stem, '' if n == 1 else ' %d' % n)
            if (cand + '.wav').lower() not in taken:
                break
        taken.add((cand + '.wav').lower())
        names[k] = cand
        placed.append((k, it, max(0.0, float(it.pos or 0.0))))
    passes = []                     # [(end time, [k, ...])]
    for k, it, pos in sorted(placed, key=lambda e: e[2]):
        for p in passes:
            if pos >= p[0] + 0.01:
                p[1].append(k)
                p[0] = pos + it.length
                break
        else:
            passes.append([pos + it.length, [k]])
    track_lines = {}
    from .media import curved_fade
    for k, it, pos in placed:
        lines = []
        curved = curved_fade(it)
        fl = getattr(it, 'fade_lines', None) or {}
        lines += ['  <TRACK', '    NAME %s' % q('print %d' % (k + 1)),
                  '    VOLPAN 1 0 -1 -1 1', '    MUTESOLO 0 0 0', '    ISBUS 0 0',
                  '    FIXEDLANES 9 0 0 0 0', '    MAINSEND 1 0',
                  '    <ITEM',
                  '      POSITION %s' % fmt(pos),
                  '      LENGTH %s' % fmt(it.length),
                  '      LOOP %d' % (1 if it.loop else 0), '      MUTE 0 0',
                  # the item's own fades when their shape is one Cubase
                  # cannot draw (media.curved_fade), else none
                  ('      FADEIN ' + ' '.join(fl['FADEIN'])) if (curved and 'FADEIN' in fl) else '      FADEIN 1 0 0 1 0 0 0',
                  ('      FADEOUT ' + ' '.join(fl['FADEOUT'])) if (curved and 'FADEOUT' in fl) else '      FADEOUT 1 0 0 1 0 0 0',
                  '      NAME %s' % q(it.name or ''),
                  '      VOLPAN 1 0 1 -1',
                  '      SOFFS %s' % fmt(it.soffs),
                  '      PLAYRATE %s %d %s -1 0 0.0025'
                  % (fmt(it.playrate or 1.0), 1 if it.preserve_pitch else 0,
                     fmt(it.pitch or 0.0)),
                  '      CHANMODE 0']
        # the source as REAPER's own item names it: the FILE line's extra
        # tokens ('1' behind an MP3 = gapless trim, 2257 samples at 44.1k;
        # without it the print sat 51 ms late and a looped item wrapped at
        # the wrong point) and the <SOURCE SECTION> wrapper of a reversed
        # or cut source, so the print plays exactly what the project does
        file_line = ' '.join(['FILE', q(it.file)]
                             + [str(a) for a in (getattr(it, 'file_args', None) or [])])
        sec = getattr(it, 'section', None)
        if sec:
            lines += ['      <SOURCE SECTION',
                      '        LENGTH %s' % fmt(sec.get('length', 0.0)),
                      '        MODE %d' % int(sec.get('mode', 0)),
                      '        STARTPOS %s' % fmt(sec.get('startpos', 0.0)),
                      '        OVERLAP %s' % fmt(sec.get('overlap', 0.01)),
                      '        <SOURCE %s' % media_kind(it.file),
                      '          ' + file_line,
                      '        >',
                      '      >']
        else:
            lines += ['      <SOURCE %s' % media_kind(it.file),
                      '        ' + file_line,
                      '      >']
        if getattr(it, 'stretch_markers', None):
            # the take's stretch markers, so the print is warped as REAPER
            # plays it (pairs of source second, item second)
            lines.append('      SM ' + ' + '.join('%s %s' % (fmt(a), fmt(b))
                                                  for a, b in it.stretch_markers))
        for env, tag in ((it.volenv, 'VOLENV'), (it.panenv, 'PANENV'),
                         (it.pitchenv, 'PITCHENV')):
            if env:
                lines += ['      <%s' % tag, '        ACT 1 -1', '        VIS 1 1 1',
                          '        LANEHEIGHT 0 0', '        ARM 0',
                          '        DEFSHAPE 0 -1 -1']
                # take time: item seconds times the playrate (rpp_read)
                tr_ = it.playrate or 1.0
                # a pan envelope runs the other way to the knob (+1 left)
                sg = -1.0 if tag == 'PANENV' else 1.0
                lines += ['        PT %s %s 0' % (fmt(a * tr_), fmt(sg * b)) for a, b in env]
                lines.append('      >')
        for blk in (it.takefx or []):
            lines += block_text(blk, '      ')
        lines += ['    >', '  >']
        track_lines[k] = lines
    by_k = {k: (it, pos) for k, it, pos in placed}
    projs = []
    for n, (_end, ks) in enumerate(passes):
        lines = list(head)
        for k in ks:
            it, pos = by_k[k]
            lines.append('  MARKER %d %s %s 1 0 1 R' % (k + 1, fmt(pos), q(names[k])))
            lines.append('  MARKER %d %s "" 1' % (k + 1, fmt(pos + it.length)))
        for k in ks:
            lines += track_lines[k]
        lines.append('>')
        proj = os.path.join(out_dir, '_print%s.rpp' % ('' if n == 0 else ' %d' % (n + 1)))
        projs.append(proj)
        with open(proj, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
        try:
            subprocess.run([reaper, '-nosplash', '-renderproject', proj],
                           timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.append('REAPER could not print the items: %s' % e)
            break
    if len(passes) > 1:
        log.append('the %d item(s) were printed in %d REAPER pass(es), each '
                   'item at its own time in the project so that a pitch '
                   'shift prints exactly as it plays' % (len(placed), len(passes)))
    out = {}
    hurt = []
    for k, it, _pos in placed:
        p = os.path.join(out_dir, names[k] + '.wav')
        if os.path.isfile(p):
            out[it] = p
            c = clipped(p)
            if c and c[0]:
                hurt.append((os.path.basename(p), c[0], c[1]))
    if hurt:
        log.append('%d printed item(s) came back with samples pinned at full '
                   'scale - the render clipped, and it is baked into the '
                   'audio: %s. The print is rendered with the fader reset, '
                   'so a track sitting well below 0 dB in REAPER can be far '
                   'above it here. Prints are 32-bit float, which cannot clip, '
                   'so CPR_PRINT_24BIT or CPR_PRINT_CFG must be set'
                   % (len(hurt), ', '.join('%s (%d sample(s), peak %.2f)' % h
                                           for h in hurt[:4])))
    if not os.environ.get('CPR_KEEP_PRINT'):
        for proj in projs:
            try:
                os.remove(proj)
            except OSError:
                pass
    if len(out) < len(placed):
        log.append('REAPER printed %d of %d item(s); the rest arrive '
                   'without their item FX' % (len(out), len(placed)))
    return out


def media_kind(path):
    ext = os.path.splitext(path or '')[1].lower()
    return {'.mp3': 'MP3', '.ogg': 'VORBIS', '.flac': 'FLAC',
            '.mp4': 'VIDEO', '.mov': 'VIDEO'}.get(ext, 'WAVE')


def fmt(v):
    v = float(v)
    # full precision: REAPER's own playrate for a stretched loop has
    # 14 digits so that the loop comes round on an exact sample, and
    # a rate rounded to nine decimals re-seeded the stretcher on the
    # second pass through the loop - the print matched REAPER's own
    # render for the first pass and nothing after it
    return str(int(v)) if v == int(v) else repr(v)
