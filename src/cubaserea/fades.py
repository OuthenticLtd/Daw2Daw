"""REAPER's item fade curves, measured (2026-09-28, identity test beds
TB5-TB7: a 1 kHz tone with a 1 s fade of every shape at nine curve values,
rendered by REAPER, gain read off in 1 ms windows).

A FADEIN/FADEOUT line is `shape length autolen flag ? curve ?` - shape 0..6,
curve -1..1 (rpp_read.fade_shape). The fade-in gain at normalised time x
(0 at the start of the fade, 1 at its end), amplitude domain:

    shape 0, 3, 4   x                         linear (three identical shapes)
    shape 1         1 - (1 - x)^2             REAPER's default: fast start
    shape 2         x                         linear, bends quadratically
    shape 5         3 x^2 - 2 x^3             slow start/end (smoothstep)
    shape 6         8 x^4 (x <= 1/2), 1 - 8 (1 - x)^4 above  steep S

A positive curve c blends the base straight towards a "slow start" target,
a negative one towards a "fast start" target, linearly in |c|:

    shapes 0/3/4    x^4               |  1 - (1 - x)^4
    shape 2         x^2               |  1 - (1 - x)^2
    shape 1         x^4               |  sqrt(1 - (1 - x)^8)

each checked at c = +-0.25, +-0.5, +-0.75, +-1 to the precision of the
measurement (0.5 %). The two S shapes do not blend linearly, so their bent
forms are kept as tables (21 points per curve, nine curves, interpolated in
both). A fade-out is the fade-in run backwards: gain(t) = fadein(1 - t/len).
"""
import math

SQRT = math.sqrt


def _base(shape, x):
    if shape == 1:
        return 1.0 - (1.0 - x) ** 2
    if shape == 5:
        return 3.0 * x * x - 2.0 * x ** 3
    if shape == 6:
        return 8.0 * x ** 4 if x <= 0.5 else 1.0 - 8.0 * (1.0 - x) ** 4
    return x


def _target(shape, x, positive):
    if shape == 2:
        return x * x if positive else 1.0 - (1.0 - x) ** 2
    if shape == 1 and not positive:
        return SQRT(max(0.0, 1.0 - (1.0 - x) ** 8))
    return x ** 4 if positive else 1.0 - (1.0 - x) ** 4


CURVES = (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)
_S56 = None


def _s56():
    """The measured curves of shapes 5 and 6, 101 points per curve at the
    nine curve values (reaper_fades_s56.json beside this module)."""
    global _S56
    if _S56 is None:
        import json
        import os
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'reaper_fades_s56.json')) as f:
            _S56 = json.load(f)
    return _S56


def _table(shape, curve, x):
    t = _s56()
    rows = t['shapes'][str(shape)]
    c = min(1.0, max(-1.0, curve))
    ci = (c + 1.0) * 4.0
    i0 = min(7, int(ci))
    fc = ci - i0
    n = len(rows[0]) - 1
    xi = min(n - 1, int(x * n))
    fx = x * n - xi
    a = rows[i0][xi] * (1 - fx) + rows[i0][xi + 1] * fx
    b = rows[i0 + 1][xi] * (1 - fx) + rows[i0 + 1][xi + 1] * fx
    return a * (1 - fc) + b * fc


def fadein_gain(shape, curve, x):
    """Amplitude gain of a REAPER fade-in at normalised time x (0..1)."""
    x = min(1.0, max(0.0, float(x)))
    shape = int(shape)
    curve = float(curve or 0.0)
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    if shape in (5, 6):
        if abs(curve) < 1e-9:
            return _base(shape, x)
        return min(1.0, max(0.0, _table(shape, curve, x)))
    if abs(curve) < 1e-9:
        return _base(shape, x)
    a = min(1.0, abs(curve))
    return (1.0 - a) * _base(shape, x) + a * _target(shape, x, curve > 0)


def fadeout_gain(shape, curve, x):
    """Gain of a fade-out at normalised time x from its start (1 at 0)."""
    return fadein_gain(shape, curve, 1.0 - min(1.0, max(0.0, float(x))))


def is_linear(shape, curve):
    return int(shape) in (0, 2, 3, 4) and abs(float(curve or 0.0)) < 1e-9


def points(shape, curve, n=32, fade_out=False):
    """(x, gain) pairs that trace the curve with straight lines between
    them, for a host that keeps fades as linear interpolators (Cubase):
    32 segments put the segments' midpoints within 0.01 dB of the curve for
    every shape. A straight fade needs only its two ends."""
    if is_linear(shape, curve):
        return [(0.0, 1.0), (1.0, 0.0)] if fade_out else [(0.0, 0.0), (1.0, 1.0)]
    f = fadeout_gain if fade_out else fadein_gain
    return [(i / float(n), f(shape, curve, i / float(n))) for i in range(n + 1)]
