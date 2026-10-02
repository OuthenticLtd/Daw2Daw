"""Fit ReaEQ bands to a measured response (an effect's render vs noise).

    from fit_reaeq import fit
    bands, err = fit(freqs, db, [(8, 1000, 6.0, 1.0), (0, 200, 3.0, 2.0)])

Each start band is (ReaEQ type, Hz, gain dB, bandwidth); frequency, gain
and bandwidth move (a pass band's gain stays at 0 dB). Returns the ReaEQ
bands (type, on, Hz, linear gain, bw) and the largest difference (dB)
over the points given. Used to turn a fixed-curve Cubase/Live EQ band
(GEQ, DJ-Eq, EQ-M5, ...) into the ReaEQ band that plays it.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
from cubaserea import chan_eq   # noqa: E402


def curve(bands, f):
    tot = np.zeros(len(f))
    for b in bands:
        tot += np.array(chan_eq.reaeq_band_db(b, list(f)))
    return tot


def fit(f, h, start, iters=600, mask_db=-60.0):
    f = np.asarray(f)
    h = np.asarray(h)
    ok = h > mask_db
    kinds = [s[0] for s in start]
    x0, steps = [], []
    for ty, hz, g, bw in start:
        x0 += [math.log(hz)]
        steps += [0.2]
        if ty in (0, 1, 8):
            x0 += [g]
            steps += [1.5]
        x0 += [math.log(bw)]
        steps += [0.3]

    def unpack(x):
        out, i = [], 0
        for ty in kinds:
            hz = math.exp(x[i]); i += 1
            g = 0.0
            if ty in (0, 1, 8):
                g = x[i]; i += 1
            bw = math.exp(x[i]); i += 1
            out.append((ty, 1, max(10.0, min(23000.0, hz)), 10 ** (g / 20.0), max(0.02, min(8.0, bw))))
        return out

    def cost(x):
        return float(np.max(np.abs(curve(unpack(x), f[ok]) - h[ok])))
    best, val = chan_eq._nelder_mead(cost, x0, steps, iters=iters)
    # a second pass from the first's end
    best, val = chan_eq._nelder_mead(cost, best, [s / 3 for s in steps], iters=iters)
    return unpack(best), val
