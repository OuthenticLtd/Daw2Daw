#!/usr/bin/env python3
"""Render a REAPER project the way REAPER plays it: every track as a stem,
and the master mix, as 32-bit float WAV.

    python render_reference.py project.rpp out_folder

This is the reference half of the identity test. The project is REAPER's
own file with nothing changed but its render settings and every track
selected: full chains, faders, pans, mutes, sends and the master chain all
apply, so what lands in `out_folder` is what REAPER actually plays -
`<track name>.wav` per track and `master.wav` for the mix. Render the same
project from Cubase into another folder (Export Audio Mixdown, batch export
of every channel plus Stereo Out, 32-bit float, project sample rate) and
compare the two folders with compare_renders.py.

Everything is 32-bit float so nothing clips on the way to disk. REAPER is
run with -renderproject and no window; the throwaway project is deleted
afterwards unless CPR_KEEP_PRINT is set.
"""
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))
from cubaserea import media
from cubaserea.render_reaper import RENDER_WAV32F

TIMEOUT = float(os.environ.get('CPR_RENDER_TIMEOUT', '1800'))


def q(s):
    return '"%s"' % s.replace('"', "'")


def prepare(lines, out_dir, stems):
    """The project text with render settings for stems (True) or the master
    mix (False), and every track selected."""
    out = []
    in_cfg = False
    header_done = False
    # the project's own sample rate: RENDER_FMT's third field is the render
    # rate and without it REAPER falls back to 44100 whatever the project is
    rate = 48000
    for ln in lines[:200]:
        s0 = ln.strip()
        if s0.startswith('SAMPLERATE '):
            try:
                rate = int(float(s0.split()[1]))
            except ValueError:
                pass
            break
    # CPR_RENDER_RATE overrides it: the Cubase side of the identity test
    # can only export at 48 kHz on this machine (its audio device gives
    # silence at 44.1), so a 44.1 kHz project is rendered at 48 on both sides
    try:
        rate = int(os.environ.get('CPR_RENDER_RATE') or rate)
    except ValueError:
        pass
    for ln in lines:
        s = ln.strip()
        if not header_done:
            if s.startswith('<RENDER_CFG'):
                in_cfg = True
                continue
            if in_cfg:
                if s == '>':
                    in_cfg = False
                continue
            if s.startswith('RENDER_'):
                continue
            if s.startswith('<TRACK') or s.startswith('<MASTERFXLIST') \
                    or s.startswith('MASTER_') or s.startswith('<PROJBAY'):
                # the header is over: put our render settings in first
                out += ['  RENDER_FILE %s' % q(out_dir),
                        '  RENDER_PATTERN %s' % ('$track' if stems else 'master'),
                        '  RENDER_FMT 0 2 %d' % rate,
                        '  RENDER_1X 0',
                        '  RENDER_RANGE 1 0 0 0 1000',
                        '  RENDER_RESAMPLE 3 0 1',
                        '  RENDER_ADDTOPROJ 0',
                        '  RENDER_STEMS %d' % (3 if stems else 0),
                        '  RENDER_DITHER 0',
                        '  RENDER_NORMALIZE 0 0 0',
                        '  <RENDER_CFG', '    ' + RENDER_WAV32F, '  >']
                header_done = True
        if header_done and s.startswith('SEL ') and ln.startswith('    SEL '):
            out.append('    SEL 1')
            continue
        out.append(ln)
        # a track written without a SEL line (a generated test project)
        # is unselected, and stems of the selected tracks were then
        # "Nothing to render!": give it one
        if header_done and ln.startswith('  <TRACK'):
            out.append('    SEL 1')
    # a track that had its own SEL line now has two; REAPER takes the last,
    # and both say 1
    return out


def without_missing_media(rpp):
    """The project's lines with every item whose media is not on disk left
    out, so REAPER renders without stopping to ask where the files went
    (print_tracks does the same for the print pass)."""
    from cubaserea import rpp_read, print_tracks
    root = rpp_read.parse(rpp)
    srcdir = os.path.dirname(os.path.abspath(rpp))
    gone, missing = print_tracks.missing_media(root, srcdir)
    if not gone:
        return open(rpp, encoding='utf-8', errors='replace').read().split('\n')
    print('  %d item(s) on %d missing file(s) left out of the render: %s'
          % (len(gone), len(missing),
             ', '.join(os.path.basename(m) for m in missing[:5])), flush=True)
    print_tracks.SKIP.clear()
    print_tracks.SKIP.update(gone)
    print_tracks.SRCDIR[0] = ''
    pr = root.blocks[0]
    lines = ['<REAPER_PROJECT%s' % ''.join(' ' + print_tracks._tok(a) for a in pr.args)]
    for e in pr.raw:
        lines += print_tracks._emit(e, '  ')
    lines.append('>')
    return lines


def render(rpp, out_dir, stems):
    # REAPER reads a relative RENDER_FILE against the project's folder
    out_dir = os.path.abspath(out_dir)
    reaper = media.find_reaper_exe()
    if not reaper:
        raise SystemExit('reaper.exe not found')
    os.makedirs(out_dir, exist_ok=True)
    lines = without_missing_media(rpp)
    text = prepare(lines, out_dir, stems)
    proj = os.path.join(out_dir, '_render_%s.rpp' % ('stems' if stems else 'master'))
    with open(proj, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(text) + '\n')
    # media paths in the project are relative to its own folder: render from
    # a copy that sits next to it, so they still resolve
    beside = os.path.join(os.path.dirname(os.path.abspath(rpp)),
                          os.path.basename(proj))
    with open(beside, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(text) + '\n')
    t0 = time.time()
    print('  rendering %s of %s ...' % ('stems' if stems else 'master',
                                        os.path.basename(rpp)), flush=True)
    try:
        subprocess.run([reaper, '-nosplash', '-renderproject', beside],
                       timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        print('  REAPER timed out after %.0f s' % TIMEOUT, flush=True)
    finally:
        if not os.environ.get('CPR_KEEP_PRINT'):
            for pth in (proj, beside):
                try:
                    os.remove(pth)
                except OSError:
                    pass
    if stems:
        rename_unnamed(rpp, out_dir)
    wavs = sorted(f for f in os.listdir(out_dir) if f.lower().endswith('.wav'))
    print('  %d file(s) in %.0f s' % (len(wavs), time.time() - t0), flush=True)
    return wavs


def rename_unnamed(rpp, out_dir):
    """REAPER names the stem of a track without a name by its number
    ('024.wav'); the converter calls that track 'Audio 24' (rpp_read), and
    so does Cubase's export. The stems are renamed to match."""
    from cubaserea import rpp_read, progress
    progress.enable(False)
    os.environ.setdefault('CPR_NO_REAPER', '1')
    os.environ.setdefault('CPR_NO_REHOST', '1')
    try:
        p = rpp_read.read(rpp, [])
    except Exception as e:
        print('  could not read the project to name its stems: %s' % e)
        return
    # REAPER's $track for an empty name is the empty string, and repeated
    # names get -001, -002 ... in track order: the k-th unnamed track's stem
    # is '-00k.wav' (the very first may be '.wav')
    n = 0
    k = 0
    for i, t in enumerate(p.tracks):
        raw = (t.raw.get('NAME', [''])[0] if getattr(t, 'raw', None) else '')
        if raw:
            continue
        k += 1
        cands = [os.path.join(out_dir, '-%03d.wav' % k)]
        if k == 1:
            cands.append(os.path.join(out_dir, '.wav'))
        dst = os.path.join(out_dir, t.name + '.wav')
        for src in cands:
            if os.path.isfile(src) and not os.path.exists(dst):
                os.replace(src, dst)
                n += 1
                break
    if n:
        print('  %d unnamed track stem(s) renamed as the converter names them' % n)


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    rpp, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        if f.lower().endswith(('.wav', '.ogg')):
            os.remove(os.path.join(out, f))
    stems = render(rpp, out, True)
    master = render(rpp, out, False)
    print('rendered: %s' % ', '.join(stems + master))


if __name__ == '__main__':
    main()
