"""Write the model's MIDI parts as a Standard MIDI File.

Cubase Track Archives carry audio, mixer state and plug-ins but MIDI parts do
not survive them, so the Cubase direction writes a type-1 SMF beside the
archive: one MIDI track per source track, at the project's tempo map, ready
for File > Import > MIDI File.
"""
from .model import playing_items
import struct

TPQ = 960


def _vlq(n):
    n = max(0, int(n))
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def _chunk(tag, body):
    return tag + struct.pack('>I', len(body)) + body


def _track(events):
    """events: [(abs_tick, bytes)] -> a MTrk chunk"""
    events.sort(key=lambda e: e[0])
    body = bytearray()
    last = 0
    for t, data in events:
        body += _vlq(t - last) + data
        last = t
    body += _vlq(0) + b'\xff\x2f\x00'
    return _chunk(b'MTrk', bytes(body))


def write(proj, path):
    tracks = []

    # tempo / time signature map
    meta = []
    num, den = proj.tsig
    dpow = max(0, (den.bit_length() - 1))
    meta.append((0, b'\xff\x58\x04' + bytes([num, dpow, 24, 8])))
    for sec, bpm in proj.tempo:
        us = int(round(60000000.0 / max(bpm, 1e-6)))
        tick = _sec_to_tick(proj, sec)
        meta.append((tick, b'\xff\x51\x03' + struct.pack('>I', us)[1:]))
    tracks.append(_track(meta))

    n_parts = 0
    for t in proj.tracks:
        events = []
        name = t.name.encode('utf-8')[:127]
        events.append((0, b'\xff\x03' + bytes([len(name)]) + name))
        for it in playing_items(t):
            if it.kind != 'midi' or not (it.notes or getattr(it, 'ccs', ())):
                continue
            n_parts += 1
            base = _sec_to_tick(proj, it.pos)
            scale = float(TPQ) / float(it.ppq or 480.0)
            for pos, status, d1, d2 in getattr(it, 'ccs', ()):
                a = base + int(round(pos * scale))
                if (status & 0xF0) == 0xC0:
                    events.append((a, bytes([status, d1 & 0x7F])))
                else:
                    events.append((a, bytes([status, d1 & 0x7F, d2 & 0x7F])))
            for pos, ln, ch, pitch, vel, *rest in it.notes:
                a = base + int(round(pos * scale))
                b = base + int(round((pos + max(ln, 1.0)) * scale))
                ch &= 0x0F
                offv = max(0, min(127, int(rest[0]))) if rest else 64
                events.append((a, bytes([0x90 | ch, pitch & 0x7F,
                                         max(1, min(127, int(vel)))])))
                events.append((max(b, a + 1), bytes([0x80 | ch, pitch & 0x7F, offv])))
        if len(events) > 1:
            tracks.append(_track(events))

    if n_parts == 0:
        return 0
    head = _chunk(b'MThd', struct.pack('>HHH', 1, len(tracks), TPQ))
    with open(path, 'wb') as f:
        f.write(head + b''.join(tracks))
    return n_parts


def _sec_to_tick(proj, sec):
    """Seconds -> ticks through the project's tempo map."""
    tick = 0.0
    prev_sec, prev_bpm = 0.0, proj.tempo[0][1]
    for s, bpm in proj.tempo:
        if s >= sec:
            break
        tick += (s - prev_sec) * (prev_bpm / 60.0) * TPQ
        prev_sec, prev_bpm = s, bpm
    tick += (sec - prev_sec) * (prev_bpm / 60.0) * TPQ
    return int(round(tick))
