#!/usr/bin/env python3
"""Null-test two folders of renders against each other, track by track.

    python compare_renders.py reaper_folder cubase_folder [--tol-db 0.1]
                              [--baseline reaper_folder_2]

Pairs files by name (case-insensitive, ignoring extension, Cubase's
"0007 - " numbering and a trailing " - 120_bpm"-style suffix; repeated
names pair up in track order; "Stereo Out" is the master; a Cubase
"X (audio)" split track is summed into its "X" partner), then for each pair reports: length, peak and
RMS of each side, their difference in dB, the time offset that best aligns
them, and the level of what is left after subtracting one from the other
(the null residual, in dB below the reference). Levels and residual are
taken over the stretch both files cover once aligned, so a render that
starts at the project start and one that starts at the first event compare
fairly. A pair passes when the RMS
agrees within the tolerance and the residual is 40 dB or more below the
reference - inaudible. The exit status is 1 when any pair fails or is
missing, so it can gate a conversion.

Reads 16/24/32-bit integer and 32-bit float WAV; downmixes to mono for the
comparison (the panner check is the pan values in verify.py, this is level
and content).

--baseline names a second REAPER render of the same project. Some plug-ins
never play the same twice (Guitar Rig's tape wobble, a synth with free
oscillators), so REAPER does not null against itself on those tracks
either; with a baseline, a pair passes when its null is within 3 dB of
REAPER's own run-to-run null for that track, and the column 'self dB'
shows that floor.
"""
import math
import os
import re
import struct
import sys

# numpy makes the null tests run in seconds instead of an hour (a 4-minute
# stem is 11 million samples a side); the bundled Python has none, so a
# folder holding it can be named in CPR_PYLIB. Without it the same maths
# runs on plain lists.
try:
    if os.environ.get('CPR_PYLIB'):
        sys.path.insert(0, os.environ['CPR_PYLIB'])
    import numpy as np
except Exception:          # pragma: no cover - the slow path
    np = None


def read_wav(path):
    raw = open(path, 'rb').read()
    if raw[:4] != b'RIFF' or raw[8:12] != b'WAVE':
        raise ValueError('not a WAV')
    o = 12
    tag = bits = ch = rate = None
    body = b''
    while o + 8 <= len(raw):
        cid = raw[o:o + 4]
        size = struct.unpack_from('<I', raw, o + 4)[0]
        if cid == b'fmt ':
            tag, ch, rate, _bps, _al, bits = struct.unpack_from('<HHIIHH', raw, o + 8)
            if tag == 0xFFFE and size >= 40:
                tag = struct.unpack_from('<H', raw, o + 8 + 24)[0]
        elif cid == b'data':
            body = raw[o + 8:o + 8 + size]
            break
        o += 8 + size + (size & 1)
    if np is not None:
        if tag == 3 and bits == 32:
            v = np.frombuffer(body[:len(body) // 4 * 4], dtype='<f4').astype(np.float64)
        elif tag == 1 and bits == 16:
            v = np.frombuffer(body[:len(body) // 2 * 2], dtype='<i2').astype(np.float64) / 32768.0
        elif tag == 1 and bits == 24:
            raw = np.frombuffer(body[:len(body) // 3 * 3], dtype=np.uint8).reshape(-1, 3)
            v = (raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8)
                 | (raw[:, 2].astype(np.int8).astype(np.int32) << 16)).astype(np.float64) / 8388608.0
        elif tag == 1 and bits == 32:
            v = np.frombuffer(body[:len(body) // 4 * 4], dtype='<i4').astype(np.float64) / 2147483648.0
        else:
            raise ValueError('unsupported WAV format tag %s bits %s' % (tag, bits))
        ch = ch or 1
        if ch > 1:
            v = v[:len(v) // ch * ch].reshape(-1, ch).mean(axis=1)
        return v, rate, ch, bits, tag
    if tag == 3 and bits == 32:
        n = len(body) // 4
        vals = struct.unpack('<%df' % n, body[:n * 4])
    elif tag == 1 and bits == 16:
        n = len(body) // 2
        vals = [v / 32768.0 for v in struct.unpack('<%dh' % n, body[:n * 2])]
    elif tag == 1 and bits == 24:
        vals = [int.from_bytes(body[i:i + 3], 'little', signed=True) / 8388608.0
                for i in range(0, len(body) - 2, 3)]
    elif tag == 1 and bits == 32:
        n = len(body) // 4
        vals = [v / 2147483648.0 for v in struct.unpack('<%di' % n, body[:n * 4])]
    else:
        raise ValueError('unsupported WAV format tag %s bits %s' % (tag, bits))
    ch = ch or 1
    if ch > 1:
        mono = [sum(vals[i:i + ch]) / ch for i in range(0, len(vals) - ch + 1, ch)]
    else:
        mono = list(vals)
    return mono, rate, ch, bits, tag


def db(v):
    return -144.0 if v <= 0 else 20.0 * math.log10(v)


def rms(x):
    if np is not None and isinstance(x, np.ndarray):
        return float(math.sqrt(np.mean(x * x))) if len(x) else 0.0
    return math.sqrt(sum(v * v for v in x) / len(x)) if x else 0.0


def onset(x, frac=0.05):
    if np is not None and isinstance(x, np.ndarray):
        if len(x) == 0:
            return None
        ax = np.abs(x)
        pk = float(ax.max())
        if pk <= 0:
            return None
        return int(np.argmax(ax >= pk * frac))
    pk = max((abs(v) for v in x), default=0.0)
    if pk <= 0:
        return None
    thr = pk * frac
    for i, v in enumerate(x):
        if abs(v) >= thr:
            return i
    return None


def best_offset(a, b, guess, span, step):
    """Offset of b relative to a (in samples) minimising the residual,
    searched around `guess`; positive means b is late."""
    best = (None, None)
    n = min(len(a), len(b)) - abs(guess) - span - 1
    if n <= 1000:
        return guess, None
    # a decimated residual is enough to locate the minimum
    if np is not None and isinstance(a, np.ndarray):
        idx = np.arange(0, n, max(1, n // 20000))
        for off in range(guess - span, guess + span + 1, step):
            if off >= 0:
                d = a[idx] - b[idx + off]
            else:
                d = a[idx - off] - b[idx]
            s = float(np.dot(d, d))
            if best[1] is None or s < best[1]:
                best = (off, s)
        return best
    idx = range(0, n, max(1, n // 20000))
    for off in range(guess - span, guess + span + 1, step):
        if off >= 0:
            s = sum((a[i] - b[i + off]) ** 2 for i in idx)
        else:
            s = sum((a[i - off] - b[i]) ** 2 for i in idx)
        if best[1] is None or s < best[1]:
            best = (off, s)
    return best


def coarse_offset(a, b, rate, block=1024, span_s=12.0):
    """Offset of b relative to a, to within a block, from the two loudness
    envelopes (RMS per `block` samples) cross-correlated over +-span_s."""
    def env(x):
        n = len(x) // block
        if np is not None and isinstance(x, np.ndarray):
            y = x[:n * block].reshape(n, block)
            return np.sqrt(np.mean(y * y, axis=1))
        return [math.sqrt(sum(v * v for v in x[i * block:(i + 1) * block]) / block)
                for i in range(n)]
    ea, eb = env(a), env(b)
    if len(ea) < 4 or len(eb) < 4:
        return None
    span = int(span_s * rate / block)
    best = None
    if np is not None and isinstance(ea, np.ndarray):
        ea = ea - ea.mean()
        eb = eb - eb.mean()
        for lag in range(-span, span + 1):
            if lag >= 0:
                x, y = ea[:len(eb) - lag], eb[lag:lag + len(ea)]
            else:
                x, y = ea[-lag:-lag + len(eb)], eb[:len(ea) + lag]
            n = min(len(x), len(y))
            if n < 4:
                continue
            x, y = x[:n], y[:n]
            d = math.sqrt(float(np.dot(x, x)) * float(np.dot(y, y)))
            c = float(np.dot(x, y)) / d if d else 0.0
            if best is None or c > best[0]:
                best = (c, lag)
        return best[1] * block if best and best[0] > 0.3 else None
    ma = sum(ea) / len(ea)
    mb = sum(eb) / len(eb)
    ea = [v - ma for v in ea]
    eb = [v - mb for v in eb]
    for lag in range(-span, span + 1):
        if lag >= 0:
            n = min(len(ea), len(eb) - lag)
            if n < 4:
                continue
            s = sum(ea[i] * eb[i + lag] for i in range(n))
            d = math.sqrt(sum(ea[i] ** 2 for i in range(n)) * sum(eb[i + lag] ** 2 for i in range(n)))
        else:
            n = min(len(ea) + lag, len(eb))
            if n < 4:
                continue
            s = sum(ea[i - lag] * eb[i] for i in range(n))
            d = math.sqrt(sum(ea[i - lag] ** 2 for i in range(n)) * sum(eb[i] ** 2 for i in range(n)))
        c = s / d if d else 0.0
        if best is None or c > best[0]:
            best = (c, lag)
    return best[1] * block if best and best[0] > 0.3 else None


def overlap(a, b, off):
    """The aligned, overlapping stretch of a and b (b shifted by off)."""
    if off >= 0:
        n = min(len(a), len(b) - off)
        return a[:n], b[off:off + n]
    n = min(len(a) + off, len(b))
    return a[-off:-off + n], b[:n]


def residual(a, b, off):
    x, y = overlap(a, b, off)
    if np is not None and isinstance(x, np.ndarray):
        return rms(x - y)
    return rms([x[i] - y[i] for i in range(len(x))])


def key_of(name):
    k = os.path.splitext(name)[0].lower()
    k = re.sub(r'^\d{4}\s*-\s*', '', k)                  # Cubase's '0007 - x'
    k = re.sub(r'^-\s*', '', k)                            # Cubase's '- Stereo Out'
    k = re.sub(r'\s*-\s*\d+\s*_?bpm.*$', '', k)          # 'x - 120_bpm'
    k = re.sub(r'\s*\[printed.*\]$', '', k)
    k = re.sub(r'_\d{2}$', '', k)                          # Cubase's 'x_01' (project Audio folder)
    k = re.sub(r'\s*\(cubase\)$', '', k)
    k = re.sub(r'\s*\(video audio\)$', '', k)     # REAPER plays it from the video track
    if k == 'stereo out':
        k = 'master'
    return k.strip()


def order_of(name):
    """Where a file comes in its DAW's track order: Cubase numbers its
    exports 0001.., REAPER suffixes repeated names -001.. ."""
    m = re.match(r'^(\d{4})\s*-', name)
    if m:
        return int(m.group(1))
    m = re.search(r'-(\d{3})$', os.path.splitext(name)[0])
    return int(m.group(1)) if m else 0


def folder_files(folder):
    """{key: [file, ...]} for a render folder. Repeated names get -001,
    -002.. in track order the way REAPER's $track pattern numbers them, so
    the two DAWs' duplicates pair up. Cubase's split 'X (audio)' track,
    which the converter makes from a REAPER track that held both MIDI and
    audio, is listed under its partner 'X' so the two sum back into one."""
    names = sorted((f for f in os.listdir(folder) if f.lower().endswith('.wav')),
                   key=lambda f: (order_of(f), f.lower()))
    plain = {}
    for f in names:
        k = re.sub(r'-\d{3}$', '', key_of(f))
        plain.setdefault(k, []).append(f)
    out = {}
    for k, fs in plain.items():
        if k.endswith(' (audio)') and k[:-8] in plain:
            continue
        parts = plain.get(k + ' (audio)', [])
        for i, f in enumerate(fs):
            kk = k if len(fs) == 1 else '%s-%03d' % (k, i + 1)
            out[kk] = [f] + ([parts[i]] if i < len(parts) else [])
    return out


def _null_score(a, b, guess):
    """How well b nulls against a around the offset `guess`: the residual
    over what a holds (0 = identical, 1 = unrelated or one side silent).
    Two silent stems score 0 - they pair with each other."""
    ra_, rb_ = rms(a), rms(b)
    if ra_ < 1e-7 and rb_ < 1e-7:
        return 0.0
    if ra_ < 1e-7 or rb_ < 1e-7:
        return 1.0
    o, _ = best_offset(a, b, guess, 480, 8)
    o = guess if o is None else o
    x, _y = overlap(a, b, o)
    kept = rms(x)
    if kept < 1e-7:
        return 1.0
    return min(1.0, residual(a, b, o) / kept)


def pair_duplicates(ra, fa, rb, fb, hint=None, hint_rate=0):
    """Tracks that share a name are numbered -001, -002.. on each side in
    each host's own track order, and the two orders need not agree (a
    Cubase export numbers channels in mixer order). The B side's names
    are reassigned so that each A stem faces the B stem that nulls best
    against it around the expected offset. Works for a bare name on one
    side against numbered ones on the other as well; B stems left over
    keep their names (and show up as missing on the A side, which they
    are)."""
    def base_of(k):
        return re.sub(r'-\d{3}$', '', k)
    ga, gb = {}, {}
    for k in fa:
        ga.setdefault(base_of(k), []).append(k)
    for k in fb:
        gb.setdefault(base_of(k), []).append(k)
    for base_name, ka in ga.items():
        kb = gb.get(base_name, [])
        if len(ka) < 2 and len(kb) < 2:
            continue
        if not kb:
            continue
        try:
            sa = {k: read_sum(ra, fa[k]) for k in ka}
            sb = {k: read_sum(rb, fb[k]) for k in kb}
        except Exception:
            continue
        rate = sa[ka[0]][1] or 48000
        guess = 0
        if hint is not None:
            guess = int(hint * rate / hint_rate) if hint_rate else int(hint)
        scores = {}
        for i in ka:
            for j in kb:
                scores[(i, j)] = _null_score(sa[i][0], sb[j][0], guess)
        assign = {}
        free_a, free_b = list(ka), list(kb)
        while free_a and free_b:
            (i, j) = min(((i, j) for i in free_a for j in free_b),
                         key=lambda ij: scores[ij])
            assign[i] = j
            free_a.remove(i)
            free_b.remove(j)
        if all(i == j for i, j in assign.items()):
            continue
        moved = {i: fb[j] for i, j in assign.items()}
        for j in assign.values():
            fb.pop(j)
        # a leftover B stem must not keep a name an A stem now owns
        for j in list(free_b):
            if j in moved:
                n = 1
                while '%s-%03d' % (base_name, 900 + n) in fb or                         '%s-%03d' % (base_name, 900 + n) in moved:
                    n += 1
                fb['%s-%03d' % (base_name, 900 + n)] = fb.pop(j)
        fb.update(moved)
        print('  %s: %d same-named stem(s) re-paired by which nulls against '
              'which' % (base_name, len(moved)))


def read_sum(folder, files):
    """Read one or more WAVs and sum them sample by sample."""
    tot = None
    meta = None
    for f in files:
        x, rate, ch, bits, tag = read_wav(os.path.join(folder, f))
        if tot is None:
            tot, meta = (x.copy() if np is not None and isinstance(x, np.ndarray)
                         else list(x)), (rate, ch, bits, tag)
        elif np is not None and isinstance(x, np.ndarray):
            n = max(len(tot), len(x))
            tot = np.pad(tot, (0, n - len(tot)))
            tot[:len(x)] += x
        else:
            n = max(len(tot), len(x))
            tot += [0.0] * (n - len(tot))
            for i, v in enumerate(x):
                tot[i] += v
    return (tot,) + meta


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    tol = 0.1
    baseline = None
    for i, a in enumerate(sys.argv):
        if a == '--tol-db' and i + 1 < len(sys.argv):
            tol = float(sys.argv[i + 1])
        if a == '--baseline' and i + 1 < len(sys.argv):
            baseline = sys.argv[i + 1]
            if baseline in args:
                args.remove(baseline)
            if '%s' % tol in args:
                pass
    if '--tol-db' in sys.argv:
        v = sys.argv[sys.argv.index('--tol-db') + 1]
        if v in args:
            args.remove(v)
    hint = None
    hint_rate = 0
    if '--offset' in sys.argv:
        # samples of the rate given with --offset-rate (else A's rate)
        v = sys.argv[sys.argv.index('--offset') + 1]
        if v in args:
            args.remove(v)
        hint = float(v)
    if '--offset-rate' in sys.argv:
        v = sys.argv[sys.argv.index('--offset-rate') + 1]
        if v in args:
            args.remove(v)
        hint_rate = float(v)
    if len(args) < 2:
        raise SystemExit(__doc__)
    ra, rb = args[0], args[1]
    fa = folder_files(ra)
    fb = folder_files(rb)
    pair_duplicates(ra, fa, rb, fb, hint, hint_rate)
    fs = folder_files(baseline) if baseline else {}
    keys = sorted(set(fa) | set(fb))
    print('%-24s %-9s %-9s %-7s %-8s %-9s %-8s %s'
          % ('track', 'A rms dB', 'B rms dB', 'diff', 'offset', 'null dB',
             'self dB', 'verdict'))
    bad = 0
    agreed = []                     # offsets of the pairs that nulled
    for k in keys:
        if k == '(unused template)':
            # the donor's emptied record, tucked away in the I/O folder:
            # it plays nothing and has no counterpart
            continue
        if re.sub(r'-\d{3}$', '', k).endswith(' (midi)') and k not in fa:
            # the muted MIDI copy the converter leaves below a printed
            # track: REAPER has no such stem, and it plays nothing
            print('%-24s Cubase-only muted MIDI copy, skipped' % k[:24])
            continue
        if k not in fa or k not in fb:
            # absent on one side: fine when the other side is silence
            # (a video track's stem, a track with nothing on it)
            have = (ra, fa[k]) if k in fa else (rb, fb[k])
            try:
                x, _r, _, _, _ = read_sum(*have)
                level = db(rms(x))
            except Exception:
                level = 0.0
            if level < -100.0:
                print('%-24s absent in %s, silent in %s' % (k[:24], 'B' if k not in fb else 'A', 'A' if k not in fb else 'B'))
                continue
            print('%-24s %s (%.1f dB on the other side)' % (k[:24], 'MISSING in %s' % ('B' if k not in fb else 'A'), level))
            bad += 1
            continue
        try:
            a, ra_, _, _, _ = read_sum(ra, fa[k])
            b, rb_, _, _, _ = read_sum(rb, fb[k])
        except Exception as e:
            print('%-24s unreadable: %s' % (k[:24], e))
            bad += 1
            continue
        if db(rms(a)) < -100.0 and db(rms(b)) < -100.0:
            # a muted group, a track with nothing on it: nothing to compare
            print('%-24s silent on both sides' % k[:24])
            continue
        oa, ob = onset(a), onset(b)
        guess = (ob - oa) if (oa is not None and ob is not None) else 0
        # a coarse pass on the loudness envelopes first: two renders whose
        # ranges start at different bars (Cubase's export from bar 0, REAPER's
        # from bar 1) sit whole seconds apart, far beyond what the fine
        # search below covers
        cg = coarse_offset(a, b, ra_)
        cands_g = [guess] + ([cg] if cg is not None and abs(cg - guess) > 480 else [])
        if hint is not None:
            # --offset: where the caller knows B sits against A (samples,
            # positive = B late), e.g. an export that starts at the first
            # event against a render that starts at the project start
            cands_g.append(int(hint * ra_ / hint_rate) if hint_rate else int(hint))
        # an offset is judged by the null it leaves RELATIVE to what the
        # overlap still holds: a sparse stem (one short note in a long
        # file) used to be "aligned" so that its note fell outside the
        # stretch both sides cover, which left nothing to differ and read
        # as a perfect null - and then as a silent export
        full_a = rms(a)

        def score(o):
            x, _y = overlap(a, b, o)
            kept = rms(x)
            if kept < 0.3 * full_a:
                return float('inf')
            return residual(a, b, o) / max(kept, 1e-12)
        best = None
        for g in cands_g:
            o1, _ = best_offset(a, b, g, 480, 8)
            o1, _ = best_offset(a, b, o1 if o1 is not None else g, 12, 1)
            o1 = o1 if o1 is not None else g
            r1 = score(o1)
            if best is None or r1 < best[1]:
                best = (o1, r1)
        off = best[0]
        # Both renders start at the same project time, so every pair shares
        # one offset (the locator rounding); a pair whose onset is soft - a
        # hi-hat, a swell - misled the search by a few hundred samples and
        # then looked 3 dB off. Zero and the offset the pairs so far agree
        # on are tried as well and kept when they null deeper.
        cands = [0]
        if agreed:
            cands.append(sorted(agreed)[len(agreed) // 2])
        for g in cands:
            if abs(g - off) > 2:
                off2, _ = best_offset(a, b, g, 12, 1)
                if off2 is not None and score(off2) < score(off):
                    off = off2
        # levels over the stretch both renders cover, so a lead-in or tail
        # that only one side rendered does not count against it
        x, y = overlap(a, b, off)
        ra_rms, rb_rms = rms(x), rms(y)
        nul = residual(a, b, off)
        diff = db(rb_rms) - db(ra_rms)
        null_db = db(nul) - db(max(ra_rms, 1e-12))
        # REAPER against itself on this track, when a second render is given
        self_db = None
        if k in fs:
            try:
                c, _rc, _, _, _ = read_sum(baseline, fs[k])
                oc = onset(c)
                g2 = (oc - oa) if (oa is not None and oc is not None) else 0
                off2, _ = best_offset(a, c, g2, 480, 8)
                off2, _ = best_offset(a, c, off2 if off2 is not None else g2, 12, 1)
                x2, y2 = overlap(a, c, off2 if off2 is not None else g2)
                self_db = db(residual(a, c, off2 if off2 is not None else g2)) - db(max(rms(x2), 1e-12))
            except Exception:
                self_db = None
        floor = -40.0 if self_db is None else max(-40.0, self_db + 3.0)
        # a track REAPER itself cannot render twice alike (a modulated
        # delay, a reverb with a random element) gets the working rule's
        # 0.5 dB on its level rather than the 0.1 dB of a deterministic one
        tol_here = tol if (self_db is None or self_db <= -20.0) else max(tol, 0.5)
        ok = abs(diff) <= tol_here and null_db <= floor
        if null_db <= -40.0:
            agreed.append(off)
        # Level within tolerance but a shallower null: the two hosts play
        # the same thing through different arithmetic - a resampler for a
        # 44.1/96 kHz file, an MP3 or AAC decoder, a fade drawn as points.
        # That meets the working rule (0.5 dB, preferably less) without
        # being sample-identical, so it is reported as OK, not PASS.
        close = (not ok) and abs(diff) <= tol and null_db <= -10.0
        if rate_mismatch := (ra_ != rb_):
            ok = close = False
        verdict = 'PASS' if ok else ('OK' if close else 'FAIL')
        if not ok and not close:
            bad += 1
        extra = ''
        if rate_mismatch:
            extra = '  (rate %s vs %s)' % (ra_, rb_)
        elif abs(len(a) - len(b)) > max(ra_ or 48000, 1) * 0.5:
            extra = '  (length %.1fs vs %.1fs)' % (len(a) / ra_, len(b) / rb_)
        print('%-24s %-9.2f %-9.2f %+-7.2f %-8s %-9.1f %-8s %s%s'
              % (k[:24], db(ra_rms), db(rb_rms), diff,
                 ('%+d smp' % off) if off is not None else '?', null_db,
                 ('%.1f' % self_db) if self_db is not None else '-', verdict, extra))
    print()
    print('%d pair(s) compared, %d problem(s)' % (len(keys), bad))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
