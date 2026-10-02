"""Cubase's channel-fader taper.

Cubase writes a fader as a raw position ('Value', 0..32768) together with the
dB it represents ('AnchorValue'), and plays the position. The curve behind
the position was found exactly on 2026-09-28 by fitting the 98 distinct
(position, dB) pairs collected from projects Cubase itself saved; the
formula reproduces every one of them to within 0.0006 dB (check_taper.py in
the identity-test scratchpad). Three pieces, all in terms of the linear gain
g and the two anchor positions 0 dB = 101/128 and -6.02 dB = 73/128:

    g >= 1     pos = 101/128 + 27/128 * (g - 1)        (1.0 at +6.02 dB, g = 2)
    0.5..1     pos =  73/128 + 28/128 * (g - 0.5) * 2   (= 45/128 + 7/16 g)
    g <  0.5   pos =  73/128 * sqrt(2 g)

The measured pairs are kept below as the evidence and for the tests; the
converter uses the formula.
"""
import math

FULL_SCALE = 32768.0

P0 = 101.0 / 128.0          # fader position for 0 dB
P6 = 73.0 / 128.0           # fader position for -6.02 dB (gain 0.5)
UNITY = P0

# (normalised fader position, dB) as read out of real projects
TAPER = [
    (-0.00003052, -200.000000), (0.14267169, -30.091602), (0.16267111, -27.812700),
    (0.16619214, -27.440698), (0.20619092, -23.694334), (0.22393689, -22.260090),
    (0.22731488, -22.000000), (0.25351337, -20.105080), (0.34377350, -14.814220),
    (0.35728650, -14.144451), (0.37504770, -13.301654), (0.37836685, -13.148592),
    (0.37907538, -13.116093), (0.38027008, -13.061429), (0.38504572, -12.844625),
    (0.38917731, -12.659214), (0.40157437, -12.114476), (0.40422940, -11.999999),
    (0.40576215, -11.934254), (0.41620349, -11.492887), (0.42443149, -11.152811),
    (0.45355286, -10.000000), (0.47350647, -9.252078), (0.47928122, -9.041499),
    (0.48042752, -9.000000), (0.49187689, -8.590858), (0.52111084, -7.587911),
    (0.55315781, -6.551154), (0.55438721, -6.512587), (0.58376593, -5.502189),
    (0.59758682, -5.000001), (0.59998172, -4.915857), (0.63403470, -3.800046),
    (0.65906662, -3.062543), (0.66128879, -2.999999), (0.66484464, -2.900849),
    (0.68906564, -2.254005), (0.69906534, -2.000394), (0.69908112, -2.000000),
    (0.72430225, -1.391447), (0.72609186, -1.349844), (0.72906439, -1.281179),
    (0.73906409, -1.054092), (0.74906372, -0.832791), (0.76906311, -0.406418),
    (0.77906281, -0.200832), (0.78906250, 0.000000), (0.79906219, 0.402357),
    (0.80906189, 0.786898), (0.83906091, 1.847843), (0.90905875, 3.912200),
]


def gain_to_norm(g):
    """Linear gain -> the fader position (0..1) Cubase stores and plays."""
    g = float(g)
    if g <= 0.0:
        return 0.0
    if g >= 1.0:
        return P0 + (1.0 - P0) * (g - 1.0)
    if g >= 0.5:
        return P6 + (P0 - P6) * (g - 0.5) * 2.0
    return P6 * math.sqrt(2.0 * g)


def norm_to_gain(p):
    """Fader position (0..1) -> the linear gain it plays at."""
    p = float(p)
    if p <= 0.0:
        return 0.0
    if p >= P0:
        return 1.0 + (p - P0) / (1.0 - P0)
    if p >= P6:
        return 0.5 + 0.5 * (p - P6) / (P0 - P6)
    return 0.5 * (p / P6) ** 2


def norm_to_db(norm, extra=()):
    g = norm_to_gain(norm)
    return 20.0 * math.log10(g) if g > 0 else -200.0


def db_to_norm(db, extra=()):
    return gain_to_norm(10.0 ** (float(db) / 20.0))


def db_to_raw(db, extra=()):
    return db_to_norm(db) * FULL_SCALE
