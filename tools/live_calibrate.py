"""Calibration beds for Live's panner and fade curves.

    python tools/live_calibrate.py make out_folder     # REAPER project + media
    python tools/live_calibrate.py tweak set.als       # set each fade track's curve
    python tools/live_calibrate.py measure reaper_dir live_dir

`make` writes calib.rpp: one track per pan position for a stereo and a mono
noise file, and fade tracks - the same 2 s linear fade-in and fade-out on
noise - that `tweak` gives different FadeIn/OutCurveSkew/Slope values once
the project has been converted (the track name says which). Render REAPER
(render_reference.py) and Live (Export, All Individual Tracks) into two
folders named <track>.wav and `measure` prints, per pan track, the L and R
gain each host applied, and per fade track the curve's gain at 10..90 %.
"""
import gzip
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_testbed import write_wav, noise, item, track   # noqa: E402

PANS = [-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0]
# (skew, slope) per fade track: a 5 x 5 grid over Live's range
GRID = (-1.0, -0.5, 0.0, 0.5, 1.0)
CURVES = [(sk, sl) for sk in GRID for sl in GRID]
SR = 48000


def make(out):
    os.makedirs(out, exist_ok=True)
    write_wav(os.path.join(out, 'st.wav'), noise(4, seed=11))
    write_wav(os.path.join(out, 'mo.wav'), noise(4, ch=1, seed=12), ch=1)
    write_wav(os.path.join(out, 'fd.wav'), noise(6, seed=13))
    tr = []
    if not os.environ.get('CALIB_FADES_ONLY'):
        for p in PANS:
            tr.append(track('pan st %+.2f' % p, item(0, 3, 'st.wav', 'st'), pan=p))
        for p in PANS:
            tr.append(track('pan mo %+.2f' % p, item(0, 3, 'mo.wav', 'mo'), pan=p))
    for sk, sl in CURVES:
        tr.append(track('fade sk%+.1f sl%+.1f' % (sk, sl),
                        item(0, 6, 'fd.wav', 'fd', fin=2.0, fout=2.0)))
    rpp = ('<REAPER_PROJECT 0.1 "7.27/win64" 1727000000\n  TEMPO 120 4 4\n'
           '  PANLAW 1\n  PANMODE 3\n  SAMPLERATE 48000 0 0\n%s>\n' % ''.join(tr))
    with open(os.path.join(out, 'calib.rpp'), 'w', newline='\n') as f:
        f.write(rpp)
    print('wrote', os.path.join(out, 'calib.rpp'))


def tweak(als):
    s = gzip.open(als).read().decode('utf-8')
    n = 0

    def one(m):
        nonlocal n
        body = m.group(0)
        name = re.search(r'<EffectiveName Value="([^"]*)"', body).group(1)
        mm = re.match(r'fade sk([+-][\d.]+) sl([+-][\d.]+)', name)
        if not mm:
            return body
        sk, sl = mm.group(1), mm.group(2)
        for tag, v in (('FadeInCurveSkew', sk), ('FadeOutCurveSkew', sk),
                       ('FadeInCurveSlope', sl), ('FadeOutCurveSlope', sl)):
            body = re.sub(r'<%s Value="[^"]*" />' % tag,
                          '<%s Value="%s" />' % (tag, float(v)), body)
        n += 1
        return body
    s = re.sub(r'<AudioTrack Id=.*?</AudioTrack>', one, s, flags=re.S)
    with gzip.open(als, 'wb') as f:
        f.write(s.encode('utf-8'))
    print('set the curve on %d fade track(s)' % n)


def read(path):
    raw = open(path, 'rb').read()
    o = 12
    while o + 8 <= len(raw):
        cid = raw[o:o + 4]
        size = struct.unpack_from('<I', raw, o + 4)[0]
        if cid == b'fmt ':
            _tag, ch, _rate = struct.unpack_from('<HHI', raw, o + 8)
        if cid == b'data':
            body = raw[o + 8:o + 8 + size]
            break
        o += 8 + size + (size & 1)
    import numpy as np
    return np.frombuffer(body, dtype='<f4').astype(np.float64).reshape(-1, ch)


def src(path):
    import numpy as np
    import wave
    w = wave.open(path)
    ch = w.getnchannels()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float64) / 32767
    return x.reshape(-1, ch)


def measure(rd, ld, base):
    import numpy as np
    st, mo, fd = (src(os.path.join(base, f)) for f in ('st.wav', 'mo.wav', 'fd.wav'))
    n = 3 * SR

    def gains(out, s):
        o = out[:n]
        sl = s[:n, 0]
        sr = s[:n, -1]
        return (math.sqrt((o[:, 0] ** 2).mean() / (sl ** 2).mean()),
                math.sqrt((o[:, 1] ** 2).mean() / (sr ** 2).mean()))
    db = lambda g: 20 * math.log10(max(g, 1e-12))
    print('%-14s %-17s %-17s %s' % ('track', 'REAPER L/R dB', 'Live L/R dB', 'Live - REAPER'))
    for kind, s in (('st', st), ('mo', mo)):
        for p in PANS:
            name = 'pan %s %+.2f' % (kind, p)
            a = gains(read(os.path.join(rd, name + '.wav')), s)
            b = gains(read(os.path.join(ld, name + '.wav')), s)
            print('%-14s %+7.2f %+7.2f   %+7.2f %+7.2f   %+6.2f %+6.2f'
                  % (name, db(a[0]), db(a[1]), db(b[0]), db(b[1]),
                     db(b[0]) - db(a[0]), db(b[1]) - db(a[1])))
    blk = SR // 100
    fr = [0.1, 0.25, 0.5, 0.75, 0.9]
    for k, (sk, sl) in enumerate(CURVES):
        name = 'fade sk%+.1f sl%+.1f' % (sk, sl)
        # every REAPER fade track is the same: shown once
        for d, who in (((rd, 'REAPER'),) if k == 0 else ()) + ((ld, 'Live'),):
            p = os.path.join(d, name + '.wav')
            if not os.path.exists(p):
                continue
            o = read(p)[:, 0]
            s = fd[:, 0]

            def g(t):
                i = int(t * SR)
                a, b = o[i - blk // 2:i + blk // 2], s[i - blk // 2:i + blk // 2]
                return math.sqrt((a ** 2).mean() / (b ** 2).mean())
            fin = ' '.join('%.3f' % g(f * 2.0) for f in fr)
            fout = ' '.join('%.3f' % g(4.0 + f * 2.0) for f in fr)
            print('%-20s %-6s in  %s   out  %s' % (name, who, fin, fout))


def table(ld, base, out):
    """Live's fade-in and fade-out gain at x = 0, 0.05 .. 1 for every
    (skew, slope) on the grid, as JSON for als_write."""
    import json
    fd = src(os.path.join(base, 'fd.wav'))[:, 0]
    blk = SR // 200
    xs = [k / 20.0 for k in range(21)]
    rows = {}
    for sk, sl in CURVES:
        o = read(os.path.join(ld, 'fade sk%+.1f sl%+.1f.wav' % (sk, sl)))[:, 0]

        def g(t):
            i = min(len(o) - blk, max(blk, int(t * SR)))
            a, b = o[i - blk // 2:i + blk // 2], fd[i - blk // 2:i + blk // 2]
            return math.sqrt((a ** 2).mean() / (b ** 2).mean())
        fin = [0.0] + [round(g(x * 2.0), 4) for x in xs[1:-1]] + [1.0]
        fout = [1.0] + [round(g(4.0 + x * 2.0), 4) for x in xs[1:-1]] + [0.0]
        rows['%g %g' % (sk, sl)] = {'in': fin, 'out': fout}
    with open(out, 'w') as f:
        json.dump({'x': xs, 'grid': list(GRID), 'curves': rows,
                   'measured': '2026-10-01, Live 11.3.43, tools/live_calibrate.py'},
                  f, indent=0)
    print('wrote', out)


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'make':
        make(sys.argv[2])
    elif cmd == 'tweak':
        tweak(sys.argv[2])
    elif cmd == 'table':
        table(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == 'measure':
        measure(sys.argv[2], sys.argv[3], sys.argv[4])
