"""Frequency response of rendered stems against the noise that went in.

    python tools/fx_response.py <dir with noise.wav> <stems dir> [--table]

Prints, per stem, its gain (dB) at a fixed set of frequencies - what a
setting of an effect does, read off a render - or, with a function from
another script, returns (freqs, dB) arrays (response()).
"""
import glob
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compare_renders as cr   # noqa: E402

FS = 48000.0
SEG = 8192
TABLE = [30, 60, 100, 200, 400, 700, 1000, 1400, 2000, 3000, 5000, 7000, 10000, 14000, 18000]


def _spec(a):
    win = np.hanning(SEG)
    S = np.zeros(SEG // 2 + 1)
    for i in range(0, len(a) - SEG, SEG // 2):
        S += np.abs(np.fft.rfft(a[i:i + SEG] * win)) ** 2
    return S


_REF = {}


def response(noise, stem, lo=25.0, hi=19000.0, step=1):
    if noise not in _REF:
        _REF[noise] = np.array(cr.read_wav(noise)[0])
    ref = _REF[noise]
    a = np.array(cr.read_wav(stem)[0])
    n = min(len(a), len(ref))
    f = np.fft.rfftfreq(SEG, 1 / FS)
    h = 10 * np.log10(np.maximum(_spec(a[:n]), 1e-30) / np.maximum(_spec(ref[:n]), 1e-30))
    sel = np.where((f >= lo) & (f <= hi))[0][::step]
    return f[sel], h[sel]


def at(f, h, x):
    k = np.argmin(np.abs(f - x))
    return float(np.mean(h[max(0, k - 2):k + 3]))


def main():
    noise, stems = sys.argv[1], sys.argv[2]
    print('%-14s' % 'Hz' + ''.join('%7d' % x for x in TABLE))
    for p in sorted(glob.glob(os.path.join(stems, '*.wav'))):
        m = re.match(r'(?:\d{4} - )?(.+)[.]wav$', os.path.basename(p))
        if not m or 'Stereo Out' in p:
            continue
        f, h = response(noise, p)
        print('%-14s' % m.group(1)[:14] + ''.join('%7.1f' % at(f, h, x) for x in TABLE))


if __name__ == '__main__':
    main()
