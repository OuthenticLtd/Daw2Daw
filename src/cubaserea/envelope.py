"""Automation curves that play the same in both hosts.

Each host draws a straight line between two automation points, but in its
own units. REAPER's volume envelope is linear in gain; Cubase's volume lane
is linear in the fader position (fader.py), which is itself linear in gain
only between -6.02 dB and 0 dB. A ramp from 0 dB to -12 dB therefore sat
1.2 dB quieter at its middle in Cubase than in REAPER (TB3 identity test,
2026-09-28) while ramps that stayed above -6 dB nulled at -84 dB. The same
holds for pan: Cubase's balance panner is linear in the pan position,
REAPER's follows a tangent law (panlaw.py).

`densify` puts extra points onto a curve so that the destination host,
drawing its straight lines through them, stays within `tol` of what the
source host played. The model keeps every envelope in the source's own
straight-line sense; the writers call this on the way out, and the Cubase
reader on the way in, so a project that goes across and back keeps its
curve rather than its point list.
"""
import math

MAX_DEPTH = 16           # a fade to nothing needs fine pieces near zero
PROBES = 7               # interior samples used to find the worst deviation


def _lerp(a, b, f):
    return a + (b - a) * f


def densify(points, to_dst, to_src, err, tol, max_depth=MAX_DEPTH):
    """`points`: [(time, value)] in the source's units, played by the
    source as straight lines in those units. The destination plays
    straight lines in `to_dst(value)` units and maps back with `to_src`.
    `err(true, played)` measures the deviation in the source's units;
    where it exceeds `tol` the segment is split at its middle (in time)
    with the value the source played there. Returns the new point list
    (values still in the source's units)."""
    if not points or len(points) < 2:
        return list(points) if points else points
    out = [points[0]]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t1 <= t0:
            out.append((t1, v1))
            continue
        _split(t0, v0, t1, v1, to_dst, to_src, err, tol, max_depth, out)
    return out


def _split(t0, v0, t1, v1, to_dst, to_src, err, tol, depth, out):
    w0, w1 = to_dst(v0), to_dst(v1)
    worst = 0.0
    for k in range(1, PROBES + 1):
        f = k / (PROBES + 1.0)
        true = _lerp(v0, v1, f)
        played = to_src(_lerp(w0, w1, f))
        worst = max(worst, err(true, played))
    if worst <= tol or depth <= 0:
        out.append((t1, v1))
        return
    tm = 0.5 * (t0 + t1)
    vm = _lerp(v0, v1, 0.5)
    _split(t0, v0, tm, vm, to_dst, to_src, err, tol, depth - 1, out)
    _split(tm, vm, t1, v1, to_dst, to_src, err, tol, depth - 1, out)


# ---------------------------------------------------------------- volume
DB_TOL = 0.02
_FLOOR = 10 ** (-80.0 / 20.0)    # below this a fade is silence in both hosts


def db_err(a, b):
    return abs(20.0 * math.log10((abs(a) + _FLOOR) / (abs(b) + _FLOOR)))


def volume_for_cubase(points, tol=DB_TOL):
    """A gain-linear envelope (REAPER's, the model's) as the points Cubase
    needs to play the same ramps with its position-linear lane. Values
    stay gains; the writer turns them into positions."""
    from . import fader
    return densify(points, fader.gain_to_norm, fader.norm_to_gain, db_err, tol)


def volume_from_cubase(points, tol=DB_TOL):
    """A Cubase lane's points, (time, gain), as the gain-linear envelope
    that plays the same ramps."""
    from . import fader
    if not points or len(points) < 2:
        return points
    norm = [(t, fader.gain_to_norm(g)) for t, g in points]
    dense = densify(norm, fader.norm_to_gain, fader.gain_to_norm,
                    db_err, tol)
    return [(t, fader.norm_to_gain(n)) for t, n in dense]


LIVE_FLOOR = 0.0003162277571      # Live's fader and lane stop at -70 dB


def _live_db(g):
    return 20.0 * math.log10(max(abs(g), LIVE_FLOOR))


def _live_err(true, played):
    # judged down to Live's floor only: under it Live plays its floor and no
    # number of points gets lower. Judged all the way to REAPER's silence, a
    # fade to nothing split every segment to the depth limit - Cherry Link's
    # Set came out 1.2 GB with 16 million points, and Live ran out of memory
    return db_err(max(abs(true), LIVE_FLOOR), max(abs(played), LIVE_FLOOR))


def volume_for_live(points, tol=DB_TOL):
    """A gain-linear envelope as the points Live's volume lane needs: Live
    draws its straight lines in dB (measured 2026-10-01 - a ramp 1.0 ->
    0.2 played 0.445 at its middle, the dB midpoint, against REAPER's
    0.597, and 0.761 / 0.260 at a sixth and five sixths in, the dB line to
    0.005). Values stay gains."""
    return densify(points, _live_db, lambda d: 10.0 ** (d / 20.0), _live_err, tol)


# ------------------------------------------------------------------- pan
PAN_TOL = 0.0005     # near a hard pan the quiet channel's dB moves fast


def pan_err(a, b):
    return abs(a - b)


def pan_curve(points, to_dst, to_src, tol=PAN_TOL):
    """A pan envelope, linear in the source panner's position, as the
    points the destination panner needs (values in the source's units;
    the caller maps them with `to_dst` when writing)."""
    return densify(points, to_dst, to_src, pan_err, tol)


# --------------------------------------------- pan-law gain along a lane
def interp(pts, x):
    """Straight-line interpolation over (x, y) points; held flat outside."""
    if not pts:
        return None
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 if x1 <= x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def sample(fn, knots, err, tol, max_depth=MAX_DEPTH):
    """(t, fn(t)) at every knot, with more points between them wherever a
    straight line through the samples strays from `fn` by more than
    `tol`."""
    knots = sorted(set(knots))
    if not knots:
        return []
    out = [(knots[0], fn(knots[0]))]
    for t0, t1 in zip(knots, knots[1:]):
        _split_fn(fn, t0, out[-1][1], t1, fn(t1), err, tol, max_depth, out)
    return out


def _split_fn(fn, t0, v0, t1, v1, err, tol, depth, out):
    worst = 0.0
    for k in range(1, PROBES + 1):
        f = k / (PROBES + 1.0)
        worst = max(worst, err(fn(t0 + (t1 - t0) * f), _lerp(v0, v1, f)))
    if worst <= tol or depth <= 0:
        out.append((t1, v1))
        return
    tm = 0.5 * (t0 + t1)
    vm = fn(tm)
    _split_fn(fn, t0, v0, tm, vm, err, tol, depth - 1, out)
    _split_fn(fn, tm, vm, t1, v1, err, tol, depth - 1, out)


def volume_with_pan_gain(volenv, vol, panenv, gain_of_pan, tol=DB_TOL):
    """The volume envelope that carries the level a pan lane needs.

    The two panners differ in level as well as in position (panlaw.py):
    for a static pan the difference goes on the other host's fader, but
    along a pan lane it changes with the pan, so it rides on the volume
    lane instead - the track's own envelope (or its fader, `vol`, when it
    has none) multiplied by `gain_of_pan` at each moment. Gain-linear
    points; the caller then fits them to the destination's taper."""
    base = volenv or [(0.0, vol)]
    knots = [t for t, _ in base] + [t for t, _ in panenv]

    def fn(t):
        return interp(base, t) * gain_of_pan(interp(panenv, t))
    return sample(fn, knots, db_err, tol)


# ------------------------------------------------ REAPER's point shapes
# A REAPER point carries the shape of the segment that leaves it (the PT
# line's third field, tension in its seventh). Measured 2026-09-28 by
# rendering a DC source under each shape (identity/gen_shapes.py):
#   0 linear   1 square (hold, then jump)   2 slow start/end: 3f^2 - 2f^3
#   3 fast start: 1 - (1-f)^3   4 fast end: f^3
#   5 bezier with tension t: a cubic bezier with, for b = 1 - |t| and t < 0,
#     control points (b/4, 1 - 3b/4) and (3b/4, 1 - b/4) - the mirror image
#     for t > 0 - which fits every measured tension (+-0.1 .. +-1) within
#     0.0002 of the range (identity/gen_shapes.py, Nelder-Mead fits).
SQUARE_STEP = 1e-4        # the width of the jump a square segment gets


def _bezier_controls(t):
    a = abs(t)
    if a >= 1.0 - 1e-9:
        # both controls in the far corner: a slow start for +1, a fast
        # rise for -1
        p1 = p2 = (1.0, 0.0) if t > 0 else (0.0, 1.0)
    else:
        # the family the measurements fit; see the module notes
        x1 = (1.0 - a) / 4.0
        w = (1.0 - a) / 2.0
        p1 = (x1, x1 + a)
        p2 = (x1 + w, x1 + w + a)
        # those are the controls for a negative tension (fast rise);
        # a positive one is the mirror image
        if t > 0:
            p1, p2 = (1.0 - p2[0], 1.0 - p2[1]), (1.0 - p1[0], 1.0 - p1[1])
        else:
            return p1, p2
    return p1, p2


def _bezier_y(p1, p2, x):
    (a, b), (c, d) = p1, p2
    lo, hi = 0.0, 1.0
    for _ in range(40):
        u = 0.5 * (lo + hi)
        xu = 3 * (1 - u) ** 2 * u * a + 3 * (1 - u) * u * u * c + u ** 3
        if xu < x:
            lo = u
        else:
            hi = u
    u = 0.5 * (lo + hi)
    return 3 * (1 - u) ** 2 * u * b + 3 * (1 - u) * u * u * d + u ** 3


def _bezier_controls_fader(t):
    """Bezier controls of a segment drawn on REAPER's fader scale (a VOLTYPE
    1 volume envelope). Measured 2026-10-01 over a 1 kHz sine, ten
    tensions -0.9 .. +0.9 between 0 and -20 dB: for b = 1 - |t| and t < 0
    the controls are (b/4, 1 - 7b/8) and (3b/4, 1 - b/8), the mirror image
    for t > 0 - every one within 0.004 dB (scratch bz/fit3.py). The x
    values are the gain family's; the y values are not."""
    a = abs(t)
    if a >= 1.0 - 1e-9:
        return _bezier_controls(t)
    b = 1.0 - a
    p1, p2 = (b / 4.0, 1.0 - 7.0 * b / 8.0), (3.0 * b / 4.0, 1.0 - b / 8.0)
    if t > 0:
        p1, p2 = (1.0 - p2[0], 1.0 - p2[1]), (1.0 - p1[0], 1.0 - p1[1])
    return p1, p2


def reaper_shape(shape, tension=0.0, fader=False):
    """f in 0..1 -> the fraction of the way from the segment's first value
    to its second, for a REAPER point shape. `fader`: the segment is drawn
    on REAPER's fader scale (VOLTYPE 1), whose bezier is its own."""
    if shape == 5 and fader and abs(tension) > 1e-9:
        p1, p2 = _bezier_controls_fader(tension)
        return lambda f: _bezier_y(p1, p2, f)
    if shape == 2:
        return lambda f: f * f * (3.0 - 2.0 * f)
    if shape == 3:
        return lambda f: 1.0 - (1.0 - f) ** 3
    if shape == 4:
        return lambda f: f ** 3
    if shape == 5 and abs(tension) > 1e-9:
        p1, p2 = _bezier_controls(tension)
        return lambda f: _bezier_y(p1, p2, f)
    return lambda f: f


def expand_shapes(points, err, tol):
    """[(time, value, shape, tension)] -> [(time, value)] with straight
    segments only, within `tol` of what REAPER plays."""
    out = []
    for i, (t0, v0, shape, tens) in enumerate(points):
        if i + 1 >= len(points):
            out.append((t0, v0))
            break
        t1, v1 = points[i + 1][0], points[i + 1][1]
        if not out or out[-1][0] != t0:
            out.append((t0, v0))
        if t1 <= t0 or v1 == v0 or shape in (0, None):
            continue
        if shape == 1:
            out.append((max(t0, t1 - SQUARE_STEP), v0))
            continue
        g = reaper_shape(shape, tens)

        def fn(t, t0=t0, t1=t1, v0=v0, v1=v1, g=g):
            return v0 + (v1 - v0) * g((t - t0) / (t1 - t0))
        seg = sample(fn, [t0, t1], err, tol)
        out.extend(seg[1:-1])
    return out


# ------------------------------------------------------------ start clip
def clip_start(points, pre):
    """Shift an envelope earlier by `pre` seconds (a pre-roll the other
    host does not have) and keep the value at the new zero: the last point
    before it is replaced by the interpolated point at 0, not dropped."""
    if not points:
        return points
    pts = [(s - pre, g) for s, g in points]
    keep = [p for p in pts if p[0] >= 0]
    before = [p for p in pts if p[0] < 0]
    if before and keep and keep[0][0] > 0:
        (t0, v0), (t1, v1) = before[-1], keep[0]
        f = (0 - t0) / (t1 - t0) if t1 > t0 else 0.0
        keep.insert(0, (0.0, _lerp(v0, v1, f)))
    elif before and not keep:
        keep = [(0.0, before[-1][1])]
    return keep

# ------------------------------------------ Cubase's event volume curve
# The curve drawn along the top of a Cubase audio event (VolumeCurveDataNode:
# points of x = samples of the file, y = linear gain) is a straight line in
# Cubase's own display scale, not in gain or dB. That scale, measured on
# Cubase 15 exports (2026-09-29) by setting a segment from +24 dB to -60 dB
# across a 2 s noise event: CUBASE_EVENT_SCALE[k] is the dB at display
# position k/100, from +24 dB (the top, 0) to silence (the bottom, 1; a
# gain of 0 and of 0.001 land on the same place). A hand-drawn +6.92 ->
# -8.14 dB segment and Gradila's Serum Rhodes curve (-5.7 -> +24 dB) are
# predicted by it within 0.02 and 0.07 dB.
CUBASE_EVENT_SCALE = (
    24.000, 21.742, 19.945, 18.466, 17.209, 16.089, 15.112, 14.238, 13.432, 12.700,
    12.023, 11.400, 10.815, 10.269, 9.753, 9.262, 8.803, 8.367, 7.950, 7.551,
    7.175, 6.808, 6.461, 6.125, 5.800, 5.492, 5.186, 4.897, 4.617, 4.343,
    4.079, 3.823, 3.575, 3.332, 3.096, 2.864, 2.642, 2.425, 2.212, 2.005,
    1.803, 1.604, 1.409, 1.221, 1.037, 0.855, 0.677, 0.502, 0.331, 0.165,
    0.000, -0.175, -0.353, -0.535, -0.723, -0.914, -1.110, -1.311, -1.512, -1.722,
    -1.934, -2.156, -2.382, -2.612, -2.848, -3.095, -3.346, -3.605, -3.873, -4.147,
    -4.430, -4.728, -5.029, -5.344, -5.674, -6.015, -6.365, -6.732, -7.121, -7.521,
    -7.944, -8.391, -8.860, -9.357, -9.879, -10.435, -11.034, -11.676, -12.369, -13.124,
    -13.942, -14.864, -15.876, -17.029, -18.354, -19.914, -21.849, -24.287, -27.746, -33.569,
    -60.000,
)


def cubase_event_pos(gain):
    """Linear gain -> position in Cubase's event-curve display scale."""
    import bisect
    if gain <= 0.001:
        return 1.0
    db = 20.0 * math.log10(gain)
    if db >= CUBASE_EVENT_SCALE[0]:
        return 0.0
    neg = [-v for v in CUBASE_EVENT_SCALE]
    k = bisect.bisect_left(neg, -db)
    if k <= 0:
        return 0.0
    d0, d1 = CUBASE_EVENT_SCALE[k - 1], CUBASE_EVENT_SCALE[k]
    return (k - 1 + (d0 - db) / (d0 - d1)) / 100.0


def cubase_event_gain(pos):
    """Position in Cubase's event-curve display scale -> linear gain."""
    if pos <= 0.0:
        return 10 ** (CUBASE_EVENT_SCALE[0] / 20.0)
    if pos >= 1.0:
        return 0.0
    x = pos * 100.0
    k = int(x)
    d = CUBASE_EVENT_SCALE[k] + (CUBASE_EVENT_SCALE[k + 1] - CUBASE_EVENT_SCALE[k]) * (x - k)
    return 10 ** (d / 20.0)


def cubase_points_for(volenv, t0, t1, max_points, tol=0.05):
    """Cubase event-curve points (seconds, gain) that play `volenv` - REAPER's
    take volume envelope, (seconds, linear gain), straight lines in gain,
    held at both ends - between t0 and t1, in at most `max_points` points.

    Cubase draws a straight line between two points in its own display
    scale (cubase_event_pos), not in gain, so the envelope's own points are
    kept and more are added where the two readings part by more than `tol`
    dB, worst segment first, until they agree or the points run out."""
    pts = sorted((t, max(0.0, g)) for t, g in volenv)
    if not pts:
        return [(t0, 1.0)]

    def target(t):
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        for (a, ga), (b, gb) in zip(pts, pts[1:]):
            if a <= t <= b:
                return ga + (gb - ga) * ((t - a) / (b - a) if b > a else 1.0)
        return pts[-1][1]

    def cub(knots, t):
        # what Cubase plays at t from these knots
        if t <= knots[0][0]:
            return knots[0][1]
        if t >= knots[-1][0]:
            return knots[-1][1]
        for (a, ga), (b, gb) in zip(knots, knots[1:]):
            if a <= t <= b:
                u = (t - a) / (b - a) if b > a else 1.0
                pa, pb = cubase_event_pos(ga), cubase_event_pos(gb)
                return cubase_event_gain(pa + (pb - pa) * u)
        return knots[-1][1]

    ts = sorted(set([t0, t1] + [t for t, _ in pts if t0 < t < t1]))
    if len(ts) > max_points:
        # more corners than the curve can hold: keep the ones whose removal
        # would change the line most (end points always)
        while len(ts) > max_points:
            best = None
            for i in range(1, len(ts) - 1):
                a, m, b = ts[i - 1], ts[i], ts[i + 1]
                ga, gm, gb = target(a), target(m), target(b)
                lin = ga + (gb - ga) * ((m - a) / (b - a) if b > a else 0)
                e = abs(db_err(gm, lin)) if gm > 0 and lin > 0 else abs(gm - lin)
                if best is None or e < best[0]:
                    best = (e, i)
            ts.pop(best[1])
    knots = [(t, target(t)) for t in ts]
    while len(knots) < max_points:
        worst = None
        for (a, _), (b, _) in zip(knots, knots[1:]):
            for f in (0.25, 0.5, 0.75):
                t = a + (b - a) * f
                g0, g1 = target(t), cub(knots, t)
                e = abs(db_err(g0, g1)) if g0 > 1e-6 and g1 > 1e-6 else (0 if abs(g0 - g1) < 1e-6 else 99)
                if worst is None or e > worst[0]:
                    worst = (e, t)
        if worst is None or worst[0] <= tol:
            break
        knots.append((worst[1], target(worst[1])))
        knots.sort()
    return knots


def cubase_event_curve(points, t0, t1, tol=DB_TOL):
    """What a Cubase event volume curve plays between t0 and t1 (seconds of
    the event), as gain-linear points dense enough that any straight-line
    reading of them stays within `tol` dB. `points` are (seconds, gain)
    on the same clock, sorted; before the first and after the last the
    curve holds its end value."""
    pts = sorted(points)
    if not pts:
        return []
    ps = [(t, cubase_event_pos(g)) for t, g in pts]

    def fn(t):
        if t <= ps[0][0]:
            return cubase_event_gain(ps[0][1])
        if t >= ps[-1][0]:
            return cubase_event_gain(ps[-1][1])
        for (a, pa), (b, pb) in zip(ps, ps[1:]):
            if a <= t <= b:
                u = (t - a) / (b - a) if b > a else 1.0
                return cubase_event_gain(pa + (pb - pa) * u)
        return cubase_event_gain(ps[-1][1])
    knots = [t0] + [t for t, _ in pts if t0 < t < t1] + [t1]
    return sample(fn, knots, db_err, tol)

# ------------------------------------------------ REAPER fader scaling
# REAPER's take volume envelope in fader scaling (VOLTYPE 1 - how 2314 of
# the user's 2318 take volume envelopes are saved) goes in a straight line
# in its fader's scale, not in gain: halfway down a +12 -> -80 dB ramp it
# is at -10 dB. The scale, measured 2026-09-30 by rendering that ramp in
# REAPER over a 1 kHz sine (scratch ep3): position along the ramp at each
# whole dB from +12 down to -80. It predicts four independent ramps
# (0 -> -12, 0 -> -40, 0 -> +6, 0 -> +12 dB) within 0.1 dB, most within
# 0.01. VOLTYPE 0 (amplitude) is straight in gain, measured within 0.002 dB.
REAPER_FADER_DB0 = 12.0          # REAPER_FADER_POS[i] is at (12 - i) dB
REAPER_FADER_POS = [
    0.000000, 0.026395, 0.052454, 0.078180, 0.103575, 0.128639, 0.153372, 0.177773, 0.201838, 0.225564,
    0.248946, 0.271979, 0.294654, 0.316966, 0.338904, 0.360461, 0.381628, 0.402394, 0.422750, 0.442688,
    0.462199, 0.481274, 0.499907, 0.518091, 0.535821, 0.553094, 0.569906, 0.586256, 0.602145, 0.617573,
    0.632542, 0.647058, 0.661123, 0.674743, 0.687927, 0.700679, 0.713010, 0.724927, 0.736439, 0.747557,
    0.758290, 0.768647, 0.778640, 0.788279, 0.797574, 0.806535, 0.815173, 0.823498, 0.831521, 0.839250,
    0.846696, 0.853869, 0.860777, 0.867431, 0.873838, 0.880008, 0.885948, 0.891668, 0.897175, 0.902476,
    0.907580, 0.912492, 0.917222, 0.921774, 0.926156, 0.930374, 0.934433, 0.938340, 0.942101, 0.945721,
    0.949205, 0.952557, 0.955784, 0.958890, 0.961879, 0.964756, 0.967524, 0.970188, 0.972752, 0.975220,
    0.977595, 0.979881, 0.982080, 0.984197, 0.986234, 0.988194, 0.990081, 0.991897, 0.993644, 0.995326,
    0.996944, 0.998502, 1.000000,
]


# Silence (-inf) sits further down the scale than -80 dB: a bezier ramp to
# 0 from -5 dB, rendered over a sine (2026-10-01, scratch bz), put it at
# 1.038 from every point along the ramp (+-0.0005). Between -80 dB (1.0)
# and silence the scale is taken as straight in dB down to -140 dB - all
# of it inaudible, but it keeps a ramp to silence on REAPER's own path.
REAPER_FADER_SILENCE = 1.0383
REAPER_FADER_FLOOR_DB = -140.0


def reaper_fader_pos(gain):
    """Linear gain -> position on REAPER's envelope fader scale (0 at
    +12 dB, 1 at -80 dB, REAPER_FADER_SILENCE at silence)."""
    n = len(REAPER_FADER_POS) - 1
    if gain <= 0.0:
        return REAPER_FADER_SILENCE
    db = 20.0 * math.log10(gain)
    x = REAPER_FADER_DB0 - db
    if x <= 0.0:
        return REAPER_FADER_POS[0] - (REAPER_FADER_POS[1] - REAPER_FADER_POS[0]) * (-x)
    if x >= n:
        floor = REAPER_FADER_DB0 - n                       # -80 dB
        if db <= REAPER_FADER_FLOOR_DB:
            return REAPER_FADER_SILENCE
        return 1.0 + (REAPER_FADER_SILENCE - 1.0) * (floor - db) / (floor - REAPER_FADER_FLOOR_DB)
    k = int(x)
    return REAPER_FADER_POS[k] + (REAPER_FADER_POS[k + 1] - REAPER_FADER_POS[k]) * (x - k)


def reaper_fader_gain(pos):
    """Position on REAPER's envelope fader scale -> linear gain."""
    import bisect
    n = len(REAPER_FADER_POS) - 1
    if pos >= REAPER_FADER_SILENCE:
        return 0.0
    if pos >= REAPER_FADER_POS[-1]:
        floor = REAPER_FADER_DB0 - n
        db = floor + (REAPER_FADER_FLOOR_DB - floor) * (pos - 1.0) / (REAPER_FADER_SILENCE - 1.0)
        return 10 ** (db / 20.0)
    if pos <= REAPER_FADER_POS[0]:
        step = REAPER_FADER_POS[1] - REAPER_FADER_POS[0]
        return 10 ** ((REAPER_FADER_DB0 + (REAPER_FADER_POS[0] - pos) / step) / 20.0)
    k = bisect.bisect_right(REAPER_FADER_POS, pos) - 1
    a, b = REAPER_FADER_POS[k], REAPER_FADER_POS[k + 1]
    db = REAPER_FADER_DB0 - (k + (pos - a) / (b - a))
    return 10 ** (db / 20.0)


def volume_from_reaper_fader(points, tol=DB_TOL):
    """A fader-scaled take volume envelope's points, (time, gain), as the
    gain-linear envelope that plays the same ramps."""
    if not points or len(points) < 2:
        return points
    fp = [(t, reaper_fader_pos(g)) for t, g in points]
    dense = densify(fp, reaper_fader_gain, reaper_fader_pos, db_err, tol)
    return [(t, reaper_fader_gain(p)) for t, p in dense]


def volume_from_reaper_fader_shaped(points, tol=DB_TOL):
    """[(time, gain, shape, tension)] of a fader-scaled (VOLTYPE 1) volume
    envelope -> gain-linear [(time, gain)] that plays the same.

    REAPER draws such an envelope on its fader scale: every segment's shape
    (linear, slow, fast, bezier...) runs between the two points' fader
    positions, not their gains. So the shape is applied there and the
    result sampled back into gain."""
    if not points:
        return []
    if len(points) < 2:
        return [(points[0][0], points[0][1])]
    out = []
    for i, (t0, g0, shape, tens) in enumerate(points):
        if i + 1 >= len(points):
            out.append((t0, g0))
            break
        t1, g1 = points[i + 1][0], points[i + 1][1]
        if not out or out[-1][0] != t0:
            out.append((t0, g0))
        if t1 <= t0 or g1 == g0:
            continue
        if shape == 1:
            out.append((max(t0, t1 - SQUARE_STEP), g0))
            continue
        p0, p1 = reaper_fader_pos(g0), reaper_fader_pos(g1)
        f = reaper_shape(shape or 0, tens, fader=True)

        def fn(t, t0=t0, t1=t1, p0=p0, p1=p1, f=f):
            return reaper_fader_gain(p0 + (p1 - p0) * f((t - t0) / (t1 - t0)))
        seg = sample(fn, [t0, t1], db_err, tol)
        out.extend(seg[1:-1])
    return out
