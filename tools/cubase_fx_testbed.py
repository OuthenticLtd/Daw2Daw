"""A Cubase project that plays white noise through one of Cubase's own
effects per track, each with its own settings - to measure what a setting
does by exporting the tracks from Cubase (render, then compare each to the
noise). The effect's state comes from Cubase's factory project templates
(or the converter's own cubase_templates.json); records not named keep the
template's values.

    python tools/cubase_fx_testbed.py <out dir> <effect name> <cases.json>

cases.json: [["track name", {"record": value, ...}], ...]
Writes <out dir>/noise.wav and <out dir>/<effect>.cpr.
"""
import contextlib
import glob
import io
import json
import os
import struct
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
from cubaserea import builtins, cpr_build, cpr_read, model   # noqa: E402

TEMPLATES = 'C:/Program Files/Steinberg/Cubase 15/Project Templates/*/*.cpr'


def factory_effect(name):
    """(uid, component, controller) of Cubase's `name` from its own
    templates, else the converter's."""
    for f in glob.glob(TEMPLATES):
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                p = cpr_read.read(f)
        except Exception:
            continue
        p = p[0] if isinstance(p, tuple) else p
        for t in p.tracks + ([p.master] if p.master else []):
            for x in t.fx:
                if x.name == name and x.component:
                    return x.uid, x.component, x.controller or b''
    comp, ctrl = builtins._template(name)
    if comp is None:
        raise SystemExit('no state for %r' % name)
    uid = json.load(open(os.path.join(os.path.dirname(builtins.__file__),
                                      'cubase_templates.json')))[name].get('uid')
    return uid, comp, ctrl


def with_records(comp, recs):
    r = builtins._records(comp)
    b = bytearray(comp)
    for k, v in recs.items():
        if k not in r:
            raise SystemExit('no record %r (has %s...)' % (k, sorted(r)[:20]))
        struct.pack_into('<d', b, r[k][0], float(v))
    return bytes(b)


def main():
    out, name, cases = sys.argv[1], sys.argv[2], json.load(open(sys.argv[3]))
    os.makedirs(out, exist_ok=True)
    rng = np.random.default_rng(1)
    x = (rng.standard_normal((48000 * 4, 2)) * 0.1).clip(-1, 1)
    w = wave.open(os.path.join(out, 'noise.wav'), 'wb')
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(48000)
    w.writeframes((x * 32767).astype('<i2').tobytes())
    w.close()
    # name '-': each case names its own effect: [track, effect, records]
    if name == '-':
        cases = [(c[0], c[1], c[2]) for c in cases]
    else:
        cases = [(c[0], name, c[1]) for c in cases]
    states = {}
    for _t, eff, _r in cases:
        if eff not in states:
            if builtins._template(eff)[0] is not None:
                states[eff] = (builtins.uid_of_name(eff) or factory_effect(eff)[0],) + builtins._template(eff)
            else:
                states[eff] = factory_effect(eff)
    L = ['<REAPER_PROJECT 0.1 "7.0/win64" 0', '  TEMPO 120 4 4', '  SAMPLERATE 48000 0 0']
    for tn, _e, _r in cases:
        L += ['  <TRACK', '    NAME %s' % tn, '    <ITEM', '      POSITION 0', '      LENGTH 4',
              '      <SOURCE WAVE', '        FILE "noise.wav"', '      >', '    >', '  >']
    L += ['>']
    rpp = os.path.join(out, 'bed.rpp')
    open(rpp, 'w').write('\n'.join(L) + '\n')
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
    import convert
    log = []
    with contextlib.redirect_stdout(io.StringIO()):
        p = convert.load(rpp, log)
    for t, (tn, eff, recs) in zip(p.tracks, cases):
        uid, comp, ctrl = states[eff]
        f = model.Fx()
        f.name, f.uid, f.controller = eff, uid, ctrl
        f.component = with_records(comp, recs)
        t.fx = [f]
    dst = os.path.join(out, ('batch' if name == '-' else name).replace(' ', '_') + '.cpr')
    if os.path.exists(dst):
        os.remove(dst)
    cpr_build.write(p, dst, donor=cpr_build.pick_donor(p), log=log)
    print(dst, len(cases), 'tracks')


if __name__ == '__main__':
    main()
