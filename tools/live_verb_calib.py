"""Calibrate Live's Reverb against ReaVerbate.

    python tools/live_verb_calib.py make out_folder
    python tools/live_verb_calib.py measure reaper_dir live_dir

`make` writes verbcal.rpp: one track per ReaVerbate room size (0.1 .. 0.9),
wet 1, dry 0, each fed the same 0.5 s noise burst and left to ring for
8 s. Render it with render_reference.py, convert it with convert.py and
export it from Live (All Individual Tracks); `measure` prints, per room
size, the reverb's energy in each (dB) and how far Live's is from
REAPER's - the table als_write's Reverb mapping takes its level from - and
the RT60 of each tail.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))
from live_testbed import write_wav, noise          # noqa: E402

ROOMS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
DAMP = [0.0, 0.3, 0.65]
SR = 48000


def make(out):
    from cubaserea import stock
    os.makedirs(out, exist_ok=True)
    burst = noise(0.5, seed=31) + [[0.0, 0.0]] * int(8.5 * SR)
    write_wav(os.path.join(out, 'burst.wav'), burst)
    tracks = []
    for damp in DAMP:
        for room in ROOMS:
            data = stock.reaverbate(wet_db=0.0, dry_db=None, room=room, damping=damp)
            fx = '\n'.join(stock.vst_lines('ReaVerbate', data, indent='      '))
            tracks.append('''  <TRACK
    NAME "room %.1f damp %.2f"
    VOLPAN 1 0 -1 -1 1
    MUTESOLO 0 0 0
    ISBUS 0 0
    <FXCHAIN
      SHOW 0
      LASTSEL 0
      DOCKED 0
%s
    >
    <ITEM
      POSITION 0
      LENGTH 9
      NAME "b"
      <SOURCE WAVE
        FILE "burst.wav"
      >
    >
  >
''' % (room, damp, fx))
    rpp = ('<REAPER_PROJECT 0.1 "7.27/win64" 1727000000\n  TEMPO 120 4 4\n'
           '  PANLAW 1\n  PANMODE 3\n  SAMPLERATE 48000 0 0\n%s>\n' % ''.join(tracks))
    with open(os.path.join(out, 'verbcal.rpp'), 'w', newline='\n') as f:
        f.write(rpp)
    print('wrote', os.path.join(out, 'verbcal.rpp'))


def measure(rd, ld):
    import numpy as np
    from live_calibrate import read

    def energy(x):
        return 10 * math.log10(max(float((x ** 2).mean()), 1e-20))

    def rt60(x):
        m = x.mean(axis=1) if x.ndim > 1 else x
        e = np.cumsum((m ** 2)[::-1])[::-1]
        e = 10 * np.log10(np.maximum(e / e[int(0.55 * SR)], 1e-20))
        # the -5 .. -25 dB stretch of the decay after the burst, scaled
        idx = np.arange(len(e))
        sel = (idx > int(0.55 * SR)) & (e < -5) & (e > -25)
        if sel.sum() < 100:
            return float('nan')
        k = np.polyfit(idx[sel] / SR, e[sel], 1)[0]
        return -60.0 / k if k < 0 else float('nan')
    print('%-20s %9s %9s %8s %8s %8s' % ('track', 'REAPER dB', 'Live dB', 'diff', 'RT REA', 'RT Live'))
    for damp in DAMP:
        for room in ROOMS:
            name = 'room %.1f damp %.2f' % (room, damp)
            a = read(os.path.join(rd, name + '.wav'))
            lf = [f for f in os.listdir(ld) if f.endswith(name + '.wav')]
            if not lf:
                print(name, 'no Live file')
                continue
            b = read(os.path.join(ld, lf[0]))
            n = min(len(a), len(b), int(9 * SR))
            ea, eb = energy(a[:n]), energy(b[:n])
            print('%-20s %9.2f %9.2f %+8.2f %8.2f %8.2f' % (name, ea, eb, eb - ea, rt60(a[:n]), rt60(b[:n])))


if __name__ == '__main__':
    if sys.argv[1] == 'make':
        make(sys.argv[2])
    else:
        measure(sys.argv[2], sys.argv[3])
