"""The two hosts' panners, measured, and the mapping between them.

Both were read off renders on 2026-09-28 (identity test beds TB2-TB4: a
stereo noise file on a track at every pan position, REAPER stems against a
Cubase Export Audio Mixdown, per-channel RMS against the source).

REAPER, stereo balance (PANMODE 3, and 5 at full width), pan p in -1..+1,
project pan law `law` (PANLAW: the linear gain at centre, 1.0 = 0 dB):

    theta = (1 + p) * pi / 4                      R / L = tan(theta), exactly
    L = k * cos(theta),  R = k * sin(theta)
    k = 1 + (law * sqrt(2) - 1) * g(|p|)         g: 1 at centre, 0 hard-panned

so the -3 dB law (0.7071) is plain sine/cosine, the 0 dB law boosts the
pair by up to 3 dB towards the centre and never at the edges, and any law
in between scales the same way (checked at 0.6, 0.8, 0.9, 1.0). g has no
closed form that fits to the fifth digit; it is tabulated below at 0.02
steps from a 51-position sweep and interpolated. At exactly -6 dB
(law 0.5) REAPER switches to a linear law: L = (1 - p) / 2, R = (1 + p) / 2.
Dual pan (PANMODE 6) ignores the pan value altogether. A mono item on a
stereo track is played on both channels and then panned the same way.

Cubase, Stereo Balance Panner (every stereo channel), pan c in -1..+1:

    L = 1,          R = 1 - |c|        for c <= 0    (and mirrored)

whatever Project Setup > Stereo Pan Law says: the law only touches mono
channels (rpp_write.PAN_LAW_GAIN). No boost, linear in the pan amount.

The two curves cross nowhere but the centre and the edges, so a pan value
copied across plays at a different level - 1.8 dB on the near channel at
half pan. A static pan is made identical by writing the pan that gives the
right L/R RATIO and putting the rest on the fader, which is what the two
mapping functions return: (pan for the other host, gain to multiply into
its fader).
"""
import math

SQRT2 = math.sqrt(2.0)

# REAPER's 0 dB-law boost fraction g at |pan| = 0.00, 0.02, ... 1.00
REAPER_PAN_BOOST = (
    1.00000, 0.99970, 0.99881, 0.99732, 0.99523, 0.99253,
    0.98924, 0.98533, 0.98081, 0.97567, 0.96991, 0.96351,
    0.95647, 0.94878, 0.94042, 0.93140, 0.92169, 0.91129,
    0.90018, 0.88834, 0.87576, 0.86242, 0.84831, 0.83339,
    0.81766, 0.80109, 0.78365, 0.76532, 0.74606, 0.72586,
    0.70468, 0.68248, 0.65923, 0.63488, 0.60941, 0.58276,
    0.55488, 0.52573, 0.49526, 0.46339, 0.43008, 0.39526,
    0.35885, 0.32077, 0.28096, 0.23931, 0.19574, 0.15014,
    0.10239, 0.05239, 0.00000,
)

BALANCE_MODES = (3, 5)      # stereo balance / mono pan, stereo pan
DUAL_PAN = 6


def boost_fraction(a):
    """g(|pan|), interpolated in the table."""
    a = min(1.0, max(0.0, abs(float(a))))
    x = a * 50.0
    i = min(49, int(x))
    f = x - i
    return REAPER_PAN_BOOST[i] * (1.0 - f) + REAPER_PAN_BOOST[i + 1] * f


def reaper_gains(pan, law=1.0, mode=3):
    """(L, R) linear gains REAPER's track panner applies."""
    p = min(1.0, max(-1.0, float(pan)))
    if mode == DUAL_PAN:
        return 1.0, 1.0
    law = 1.0 if law is None else float(law)
    if law <= 0.5 + 1e-9:
        return (1.0 - p) / 2.0, (1.0 + p) / 2.0
    theta = (1.0 + p) * math.pi / 4.0
    k = 1.0 + (law * SQRT2 - 1.0) * boost_fraction(p)
    return k * math.cos(theta), k * math.sin(theta)


def cubase_gains(pan):
    """(L, R) linear gains Cubase's Stereo Balance Panner applies."""
    c = min(1.0, max(-1.0, float(pan)))
    if c <= 0:
        return 1.0, 1.0 + c
    return 1.0 - c, 1.0


def _pan_from_gains_cubase(gl, gr):
    """The Cubase pan whose L/R ratio is gr/gl, and the gain of its louder
    channel (what Cubase would need on the fader to match)."""
    if gl <= 0 and gr <= 0:
        return 0.0, 0.0
    if gl >= gr:
        return -(1.0 - gr / gl), gl
    return (1.0 - gl / gr), gr


# Cubase's Project Setup > Stereo Pan Law codes -> the linear gain of a mono
# signal placed at the centre (measured on the export; 6 is the default)
CUBASE_LAW_GAIN = {4: 1.0, 6: SQRT2 / 2.0, 3: 0.70794578, 2: 0.59566214,
                   1: 0.50118723}


def cubase_mono_gains(pan, law_code=6):
    """(L, R) linear gains Cubase's mono channel panner (PannerType 4 on a
    channel whose bus arrangement is mono) applies. Measured 2026-09-29 on
    a Cubase export at -0.75, -0.5, -0.25, 0, +0.1, +0.25, +0.5, +0.75 and
    the edges: plain sine/cosine, exact to 0.01 dB - full level on the
    near side when hard-panned, -3.01 dB on both at the centre - and the
    same under every Project Setup pan law (0 dB, -3, -4.5, -6 dB and
    Equal Power exported alike): the law touches mono FILES on stereo
    channels (CUBASE_LAW_GAIN), not this panner. `law_code` is accepted
    and ignored so callers can pass the project's."""
    c = min(1.0, max(-1.0, float(pan)))
    theta = (1.0 + c) * math.pi / 4.0
    return math.cos(theta), math.sin(theta)


def _pan_from_gains_cubase_mono(gl, gr, law_code=6):
    """The mono-channel pan whose L/R ratio is gr/gl, and the gain of the
    louder channel at that pan (a fader would need 1/that to match)."""
    if gl <= 0 and gr <= 0:
        return 0.0, 0.0
    if gl >= gr:
        ratio = gr / gl
        c = 4.0 * math.atan(ratio) / math.pi - 1.0
    else:
        ratio = gl / gr
        c = 1.0 - 4.0 * math.atan(ratio) / math.pi
    c = min(1.0, max(-1.0, c))
    ml, mr = cubase_mono_gains(c, law_code)
    return c, max(gl, gr) / max(ml, mr)


def reaper_to_cubase(pan, law=1.0, mode=3, mono=False, law_code=6):
    """REAPER pan -> (Cubase pan, gain to multiply into the Cubase fader).
    `mono`: the Cubase channel is a mono channel (its own panner)."""
    gl, gr = reaper_gains(pan, law, mode)
    if mono:
        return _pan_from_gains_cubase_mono(gl, gr, law_code)
    return _pan_from_gains_cubase(gl, gr)


def cubase_to_reaper(pan, law=1.0, mode=3, mono=False, law_code=6):
    """Cubase pan -> (REAPER pan, gain to multiply into the REAPER fader)
    for a REAPER project written with pan law `law`. `mono`: the Cubase
    channel is a mono channel, panned by cubase_mono_gains."""
    gl, gr = cubase_mono_gains(pan, law_code) if mono else cubase_gains(pan)
    if mode == DUAL_PAN:
        return 0.0, 1.0
    if gl >= gr:
        ratio = gr / gl if gl > 0 else 0.0     # R/L <= 1: left of centre
        if law <= 0.5 + 1e-9:
            p = (ratio - 1.0) / (ratio + 1.0)
        else:
            theta = math.atan(ratio)
            p = 4.0 * theta / math.pi - 1.0
    else:
        ratio = gl / gr                        # L/R < 1: right of centre
        if law <= 0.5 + 1e-9:
            p = (1.0 - ratio) / (1.0 + ratio)
        else:
            theta = math.atan(ratio)
            p = 1.0 - 4.0 * theta / math.pi
    p = min(1.0, max(-1.0, p))
    rl, rr = reaper_gains(p, law, mode)
    near_r = max(rl, rr)
    near_c = max(gl, gr)
    return p, (near_c / near_r if near_r > 0 else 1.0)


def live_gains(pan):
    """(L, R) linear gains Live 11's track panner applies. Measured
    2026-10-01 (tools/live_calibrate.py: a stereo and a mono noise file on
    tracks at -1 .. +1 in quarter steps, Export All Individual Tracks):

        L = sqrt(2) * cos(theta),  R = sqrt(2) * sin(theta),  theta = (1 + p) * pi / 4

    exact to 0.01 dB at every position - unity at the centre, +3.01 dB on
    the near side hard-panned - and the same for a mono file. The angle is
    REAPER's, so a REAPER pan value keeps its L/R ratio in Live and only
    the level differs (REAPER's boost is the tabulated g, Live's is fixed)."""
    p = min(1.0, max(-1.0, float(pan)))
    theta = (1.0 + p) * math.pi / 4.0
    return SQRT2 * math.cos(theta), SQRT2 * math.sin(theta)


def gains_to_live(gl, gr):
    """The Live pan whose L/R ratio is gr/gl, and the gain to multiply into
    Live's fader so the louder channel lands where it did."""
    if gl <= 0 and gr <= 0:
        return 0.0, 1.0
    p = 4.0 * math.atan2(gr, gl) / math.pi - 1.0
    p = min(1.0, max(-1.0, p))
    ll, lr = live_gains(p)
    if gl >= gr:
        return p, gl / ll
    return p, gr / lr


def to_live(pan, source, law=1.0, mode=3, mono=False, law_code=6):
    """A pan value of `source`'s panner ('reaper' or 'cubase') -> (Live
    pan, gain to multiply into Live's fader)."""
    if source == 'cubase':
        gl, gr = cubase_mono_gains(pan, law_code) if mono else cubase_gains(pan)
    else:
        gl, gr = reaper_gains(pan, law, mode)
    return gains_to_live(gl, gr)


def describe_gains(gl, gr):
    def db(v):
        return -144.0 if v <= 0 else 20.0 * math.log10(v)
    return 'L %+.2f dB, R %+.2f dB' % (db(gl), db(gr))
