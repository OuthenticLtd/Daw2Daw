"""Round-trip check for the built-in effect mappings, without any DAW.

    python tools/natives_check.py <work dir> [family ...]

For each case below a source project is made - Cubase effects with given
records (a .cpr built from the converter's donor), or REAPER blocks / JS
effects (a .rpp) - then converted to the other two DAWs and back to its
own. Printed per case: what each target got (the converter's log lines
for the track), anything left out, and the settings that came back
different from what went in (records for Cubase, the data block's
values or JS sliders for REAPER). Rendering (null tests) is separate:
these are the settings.
"""
import contextlib
import io
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'src')
sys.path.insert(0, SRC)
from cubaserea import builtins, chan_eq, cpr_build, cpr_read, model, natives, rpp_read, stock  # noqa: E402

L = stock.db2lin

# family -> [(case name, 'cubase', effect name, records) | (name, 'reaper', [entries])]
CASES = {
    'modulation': [
        ('c_chorus', 'cubase', 'Chorus', {'rate': 0.8, 'temposync': 0.0, 'width': 30.0, 'mix': 40.0}),
        ('c_flanger', 'cubase', 'Flanger', {'rate': 0.5, 'temposync': 0.0, 'feedback': 40.0, 'mix': 50.0}),
        ('c_phaser', 'cubase', 'Phaser', {'rate': 0.4, 'temposync': 0.0, 'feedback': 30.0, 'mix': 50.0}),
        ('c_tremolo', 'cubase', 'Tremolo', {'rate': 5.0, 'depth': 60.0}),
        ('c_autopan', 'cubase', 'AutoPan', {'rate': 2.0, 'width': 80.0}),
        ('c_vibrato', 'cubase', 'Vibrato', {'rate': 5.0, 'tempoSync': 0.0, 'depth': 40.0}),
        ('c_wah', 'cubase', 'WahWah', {'pedal': 60.0}),
        ('r_chorus', 'reaper', [('js', 'sstillwell/chorus', [12.0, 2.0, 0.7, 0.4, -6.0, -3.0])]),
        ('r_flanger', 'reaper', [('js', 'guitar/flanger', [5.0, -10.0, -6.0, -6.0, 0.4])]),
        ('r_phaser', 'reaper', [('js', 'guitar/phaser', [0.6, 300.0, 2000.0, -6.0, 0.0])]),
        ('r_trem', 'reaper', [('js', 'guitar/tremolo', [6.0, -12.0, 0.0])]),
        ('r_ppp', 'reaper', [('js', 'loser/ppp', [1.5, 70.0])]),
    ],
    'delay': [
        ('c_mono', 'cubase', 'MonoDelay', {'delay': 320.0, 'temposync': 0.0, 'feedback': 40.0, 'mix': 30.0}),
        ('c_studio', 'cubase', 'StudioDelay', {'delaytime0': 450.0, 'temposync': 0.0, 'feedback': 35.0, 'mix': 40.0}),
        ('c_modm', 'cubase', 'ModMachine', {'delaytime': 280.0, 'temposync': 0.0, 'delayfeedback': 30.0}),
        ('r_jsdelay', 'reaper', [('js', 'delay/delay', [350.0, -8.0, 0.0, -6.0, 0.0, 0.0])]),
        ('r_jspong', 'reaper', [('js', 'sstillwell/delay_pong', [300.0, -10.0, 0.0, -6.0, 0.0, 80.0, 0.25])]),
    ],
    'eq': [
        ('c_studioeq', 'cubase', 'StudioEQ', {'gainlfl': 4.0, 'freqlfl': 120.0, 'gainp1l': -3.0, 'qp1l': 2.0,
                                             'freqp1l': 800.0, 'hftype': 3.0, 'freqhfl': 9000.0}),
        ('c_djeq', 'cubase', 'DJ-Eq', {'lowgain': 4.0, 'highgain': -6.0}),
        ('c_geq10', 'cubase', 'GEQ-10', {'slider3': 0.8, 'slider7': 0.3}),
        ('c_eqm5', 'cubase', 'EQ-M5', {'boostlow': 4.0, 'attenmid': 3.0}),
        ('c_eqp1a', 'cubase', 'EQ-P1A', {'lowboost': 4.0, 'highatten': 3.0}),
        ('r_hpflpf', 'reaper', [('js', 'sstillwell/hpflpf', [80.0, 12000.0, -2.0])]),
        ('r_rbj7', 'reaper', [('js', 'sstillwell/rbj7eq', [40.0, 3.0, 0, -2.0, 0, 4.0, 0, -3.0])]),
        ('r_3band', 'reaper', [('js', 'loser/3BandEQ', [4.0, 200.0, 0.0, 2500.0, -3.0, 0.0])]),
    ],
    'dynamics': [
        ('c_expander', 'cubase', 'Expander', {'threshold': -30.0, 'ratio': 3.0, 'attack': 4.0, 'release': 80.0}),
        ('c_deesser', 'cubase', 'DeEsser', {'threshold': -28.0, 'autothreshold': 0.0, 'reduction': 6.0,
                                            'lowfreq': 5000.0, 'highfreq': 12000.0, 'release': 60.0}),
        ('c_maximizer', 'cubase', 'Maximizer', {'optimise': 40.0, 'output': -0.3}),
        ('c_raiser', 'cubase', 'Raiser', {'input': 4.0, 'threshold': -0.5}),
        ('c_voxcomp', 'cubase', 'VoxComp', {'threshold': -20.0, 'output': 3.0, 'mix': 80.0}),
        ('c_blackvalve', 'cubase', 'Black Valve', {'peakreduction': 30.0, 'output': 2.0}),
        ('c_tube', 'cubase', 'Tube Compressor', {'inputgain': 10.0, 'attack': 5.0, 'release': 200.0, 'mix': 70.0}),
        ('c_vintage', 'cubase', 'VintageCompressor', {'inputgain': 8.0, 'ratio': 1.0, 'outputgain': 2.0}),
        ('c_vstdyn', 'cubase', 'VSTDynamics', {'con': 1.0, 'cthreshold': -18.0, 'cratio': 4.0, 'gon': 1.0,
                                              'gthreshold': -50.0, 'lon': 1.0, 'loutput': -0.5}),
        ('c_mbc', 'cubase', 'MultibandCompressor', {'threshold1': -20.0, 'ratio1': 3.0, 'freq1': 120.0,
                                                    'threshold2': -18.0, 'freq2': 2000.0, 'freq3': 8000.0,
                                                    'makeup3': 2.0, 'ratio4': 6.0}),
        ('c_mbe', 'cubase', 'MultibandExpander', {'threshold1': -40.0, 'ratio1': 2.0, 'freq1': 200.0}),
        ('r_reaxcomp', 'reaper', [('vst', 'ReaXcomp', natives.reaxcomp([
            dict(top_hz=150.0, threshold_db=-20.0, ratio=3.0, attack_ms=10, release_ms=120, gain_db=1.0),
            dict(top_hz=2500.0, threshold_db=-15.0, ratio=2.0, attack_ms=5, release_ms=80),
            dict(top_hz=24000.0, threshold_db=-25.0, ratio=4.0, attack_ms=2, release_ms=60, gain_db=-1.0)]))]),
        ('r_expander', 'reaper', [natives.js_expander(-35.0, 1.8, 0.0, False, 20.0, 50.0)]),
        ('r_deess', 'reaper', natives._comp(-30.0, 5.0, 0.5, 80.0, lowpass=12000.0, hipass=5000.0)),
        ('r_mbexp', 'reaper', [('vst', 'ReaXcomp', natives.reaxcomp([
            dict(top_hz=250.0, threshold_db=-45.0, ratio=0.5, attack_ms=5, release_ms=100),
            dict(top_hz=24000.0, threshold_db=-40.0, ratio=0.5, attack_ms=5, release_ms=100)]))]),
    ],
}


def run(*a):
    return subprocess.run([sys.executable, os.path.join(SRC, 'convert.py')] + list(a),
                          capture_output=True, text=True, encoding='utf-8', errors='replace').stdout


def build_cubase(path, cases, wd):
    import wave
    wv = os.path.join(wd, 'one.wav')
    if not os.path.exists(wv):
        w = wave.open(wv, 'wb')
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b'\0\0\0\0' * 4800)
        w.close()
    L_ = ['<REAPER_PROJECT 0.1 "7.0/win64" 0', '  TEMPO 120 4 4', '  SAMPLERATE 48000 0 0']
    for nm, *_ in cases:
        L_ += ['  <TRACK', '    NAME %s' % nm, '    <ITEM', '      POSITION 0', '      LENGTH 0.1',
               '      <SOURCE WAVE', '        FILE "one.wav"', '      >', '    >', '  >']
    rpp = os.path.join(wd, '_bed.rpp')
    open(rpp, 'w').write('\n'.join(L_ + ['>']) + '\n')
    sys.path.insert(0, SRC)
    import convert
    with contextlib.redirect_stdout(io.StringIO()):
        p = convert.load(rpp, [])
    for t, (nm, _k, eff, rec) in zip(p.tracks, cases):
        f = model.Fx()
        f.name, f.uid = eff, builtins.uid_of_name(eff)
        f.component = builtins.table_state(eff, rec)
        f.controller = builtins._template(eff)[1]
        t.fx = [f]
    if os.path.exists(path):
        os.remove(path)
    with contextlib.redirect_stdout(io.StringIO()):
        cpr_build.write(p, path, donor=cpr_build.pick_donor(p), log=[])


def build_reaper(path, cases):
    out = ['<REAPER_PROJECT 0.1 "7.0/win64" 0', '  TEMPO 120 4 4', '  SAMPLERATE 48000 0 0']
    for nm, _k, ents in cases:
        out += ['  <TRACK', '    NAME %s' % nm, '    <FXCHAIN'] + stock.lines(ents, '      ') + ['    >', '  >']
    open(path, 'w').write('\n'.join(out + ['>']) + '\n')


def cubase_fx(path):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        p = cpr_read.read(path)
    p = p[0] if isinstance(p, tuple) else p
    out = {}
    for t in p.tracks:
        fx = list(t.fx)
        if getattr(t, 'chan_eq', None):
            ce = model.Fx()
            ce.name = 'channel EQ (%d bands)' % len(t.chan_eq)
            fx.append(ce)
        out[t.name] = fx
    return out


def reaper_fx(path):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        p = rpp_read.read(path, [])
    out = {}
    for t in p.tracks:
        got = []
        for f in t.fx:
            if getattr(f, 'reaper_stock', None):
                got.append(f.reaper_stock)
            elif getattr(f, 'reaper_js', None):
                got.append(('JS:' + f.reaper_js[0], f.reaper_js[1]))
            elif f.native:
                got.append((f.name, getattr(f, 'raw_state', None) or natives.js_sliders(f)))
            else:
                got.append((f.name, None))
        if getattr(t, 'chan_eq', None):
            got.append(('channel EQ', None))
        out[t.name] = got
    return out


def diff_records(a, b):
    ra, rb = builtins._records(a or b''), builtins._records(b or b'')
    return ['%s %g->%g' % (k, ra[k][1], rb[k][1]) for k in ra
            if k in rb and abs(ra[k][1] - rb[k][1]) > max(1e-3, 1e-3 * abs(ra[k][1]))
            and not k.startswith(('reset', 'show'))]


def notes_for(log, track):
    return [l.strip() for l in log.splitlines() if track in l]


def main():
    wd = os.path.abspath(sys.argv[1])
    os.makedirs(wd, exist_ok=True)
    fams = sys.argv[2:] or list(CASES)
    for fam in fams:
        cases = CASES[fam]
        cub = [c for c in cases if c[1] == 'cubase']
        rea = [c for c in cases if c[1] == 'reaper']
        print('==== %s' % fam)
        if cub:
            src = os.path.join(wd, fam + '_c.cpr')
            build_cubase(src, cub, wd)
            outs = {}
            for ext in ('rpp', 'als'):
                dst = os.path.join(wd, fam + '_c_to.' + ext)
                outs[ext] = run(src, dst)
                back = os.path.join(wd, fam + '_c_back_' + ext + '.cpr')
                if os.path.exists(back):
                    os.remove(back)
                outs[ext + '_back'] = run(dst, back)
            a, b1, b2 = cubase_fx(src), cubase_fx(os.path.join(wd, fam + '_c_back_rpp.cpr')), \
                cubase_fx(os.path.join(wd, fam + '_c_back_als.cpr'))
            for nm, _k, eff, _rec in cub:
                fa = a.get(nm, [])
                print('-- %s (%s)' % (nm, eff))
                for ext in ('rpp', 'als'):
                    for l in notes_for(outs[ext], eff)[:2]:
                        print('   to %s: %s' % (ext, l[:150]))
                for lab, bb in (('via REAPER', b1), ('via Live', b2)):
                    fb = bb.get(nm, [])
                    names = [f.name for f in fb]
                    same = [f for f in fb if f.name == eff]
                    if not same:
                        print('   back %s: %s' % (lab, names or 'NOTHING'))
                    else:
                        d = diff_records(fa[0].component, same[0].component) if fa else []
                        print('   back %s: %s %s' % (lab, eff, ('changed: ' + ', '.join(d[:8])) if d else 'same settings'))
        if rea:
            src = os.path.join(wd, fam + '_r.rpp')
            build_reaper(src, rea)
            res = {}
            for ext in ('cpr', 'als'):
                dst = os.path.join(wd, fam + '_r_to.' + ext)
                if os.path.exists(dst):
                    os.remove(dst)
                res[ext] = run(src, dst)
                back = os.path.join(wd, fam + '_r_back_' + ext + '.rpp')
                res[ext + '_back'] = run(dst, back)
            a = reaper_fx(src)
            for nm, _k, ents in rea:
                print('-- %s' % nm)
                for ext in ('cpr', 'als'):
                    for l in notes_for(res[ext], nm)[:2] + [l.strip() for l in res[ext].splitlines()
                                                             if 'left out' in l and nm in l][:1]:
                        print('   to %s: %s' % (ext, l[:150]))
                    b = reaper_fx(os.path.join(wd, fam + '_r_back_' + ext + '.rpp')).get(nm, [])
                    fa = a.get(nm, [])
                    if [x[0] for x in fa] != [x[0] for x in b]:
                        print('   back via %s: %s (went in: %s)' % (ext, [x[0] for x in b], [x[0] for x in fa]))
                        continue
                    diffs = []
                    for (n1, d1), (n2, d2) in zip(fa, b):
                        if isinstance(d1, (bytes, bytearray)) and isinstance(d2, (bytes, bytearray)):
                            if n1 == 'ReaXcomp':
                                x1, x2 = natives.reaxcomp_bands(d1), natives.reaxcomp_bands(d2)
                                for k, (u, v) in enumerate(zip(x1 or [], x2 or [])):
                                    for key in u:
                                        if abs(float(u[key]) - float(v[key])) > 0.01 * max(1.0, abs(float(u[key]))):
                                            diffs.append('band%d %s %s->%s' % (k + 1, key, u[key], v[key]))
                            elif d1 != d2:
                                n = min(len(d1), len(d2)) // 4
                                f1 = struct.unpack_from('<%df' % n, d1)
                                f2 = struct.unpack_from('<%df' % n, d2)
                                diffs += ['f32[%d] %g->%g' % (i, x, y) for i, (x, y) in enumerate(zip(f1, f2))
                                          if abs(x - y) > 1e-4 * max(1.0, abs(x))][:6]
                        elif d1 and d2 and list(d1)[:16] != list(d2)[:16]:
                            diffs.append('%s sliders %s -> %s' % (n1, list(d1)[:8], list(d2)[:8]))
                    print('   back via %s: %s' % (ext, ('changed: ' + '; '.join(diffs[:8])) if diffs else 'same settings'))


if __name__ == '__main__':
    main()
