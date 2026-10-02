"""Write the REAPER test bed for the Live writer's identity test.

    python tools/live_testbed.py out_folder

out_folder/livetb.rpp plus its media: every thing als_write claims to
carry, each on a track of its own so a stem shows which one is off. No
plug-ins anywhere - the writer carries none yet, and REAPER's render of the
project must be what Live can play.

    Offset gain fade   stereo noise, item cut 0.5 s in, -2 dB, linear fades
    Fader -6           a tone at -6 dB on the fader
    Pan stereo L50     stereo noise, pan -0.5
    Pan mono R50       a mono file, pan +0.5
    Vol automation     a tone under a volume ramp
    Bus (folder)       at 0.7, holding Child A and Child B (muted)
    After tempo        an item past the tempo change (120 -> 90 at 6 s)
    Rate 44k1          a 44.1 kHz file on the 48 kHz project
    Keys               MIDI only (silent in both: no instrument) - its notes
                       are compared from the Live Set itself
"""
import math
import os
import random
import struct
import sys
import wave

SR = 48000


def write_wav(path, frames, rate=SR, ch=2):
    with wave.open(path, 'wb') as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b''.join(struct.pack('<%dh' % ch, *[max(-32767, min(32767, int(v * 32767)))
                                                           for v in f]) for f in frames))


def noise(sec, rate=SR, ch=2, amp=0.25, seed=1):
    r = random.Random(seed)
    return [[amp * (2 * r.random() - 1) for _ in range(ch)] for _ in range(int(sec * rate))]


def tone(freq, sec, rate=SR, ch=2, amp=0.3):
    return [[amp * math.sin(2 * math.pi * freq * i / rate)] * ch for i in range(int(sec * rate))]


def item(pos, length, f, name, soffs=0.0, vol=1.0, fin=0.0, fout=0.0,
         rate=1.0, preserve=1, pitch=0.0, shape=0):
    return '''    <ITEM
      POSITION %g
      LENGTH %g
      FADEIN %d %g 0 0 0 0 0
      FADEOUT %d %g 0 0 0 0 0
      MUTE 0 0
      NAME "%s"
      VOLPAN %g 0 1 -1
      SOFFS %g
      PLAYRATE %g %d %g -1 0 0.0025
      <SOURCE WAVE
        FILE "%s"
      >
    >
''' % (pos, length, shape, fin, shape, fout, name, vol, soffs, rate, preserve, pitch, f)


def track(name, body='', vol=1.0, pan=0.0, mute=0, isbus='0 0', extra=''):
    return '''  <TRACK
    NAME "%s"
    VOLPAN %g %g -1 -1 1
    MUTESOLO %d 0 0
    ISBUS %s
%s%s  >
''' % (name, vol, pan, mute, isbus, extra, body)


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    write_wav(os.path.join(out, 'noise.wav'), noise(8))
    write_wav(os.path.join(out, 'noise2.wav'), noise(8, seed=2))
    write_wav(os.path.join(out, 'tone440.wav'), tone(440, 8))
    write_wav(os.path.join(out, 'tone330.wav'), tone(330, 8))
    write_wav(os.path.join(out, 'tone550.wav'), tone(550, 8))
    write_wav(os.path.join(out, 'mono.wav'), noise(8, ch=1, seed=3), ch=1)
    write_wav(os.path.join(out, 'rate441.wav'), tone(660, 6, rate=44100), rate=44100)
    db = lambda d: 10 ** (d / 20.0)
    volenv = '''    <VOLENV2
      EGUID {00000000-0000-0000-0000-00000000E001}
      ACT 1 -1
      VIS 1 1 1
      LANEHEIGHT 0 0
      ARM 0
      DEFSHAPE 0 -1 -1
      VOLTYPE 1
      PT 0 1 0
      PT 2 1 0
      PT 4 0.25 0
      PT 6 1 0
    >
'''
    tracks = [
        track('Offset gain fade', item(1.0, 5.0, 'noise.wav', 'ogf', soffs=0.5,
                                       vol=db(-2), fin=0.5, fout=1.0)),
        track('Fader -6', item(0.5, 4.0, 'tone440.wav', 'f6'), vol=db(-6)),
        track('Pan stereo L50', item(0.0, 4.0, 'noise2.wav', 'ps'), pan=-0.5),
        track('Pan mono R50', item(0.0, 4.0, 'mono.wav', 'pm'), pan=0.5),
        track('Vol automation', item(0.0, 7.0, 'tone330.wav', 'va'), extra=volenv),
        track('Bus', vol=0.7, isbus='1 1'),
        track('Child A', item(0.0, 3.0, 'tone550.wav', 'ca')),
        track('Child B', item(1.0, 3.0, 'noise.wav', 'cb'), mute=1, isbus='2 -1'),
        track('After tempo', item(8.0, 4.0, 'tone440.wav', 'at')),
        track('Rate 44k1', item(2.0, 4.0, 'rate441.wav', 'r44')),
        track('Stretch 1.25 keep pitch', item(0.5, 4.0, 'tone330.wav', 'st', rate=1.25)),
        track('Stretch 0.8 tape', item(0.5, 4.0, 'tone330.wav', 'tp', rate=0.8, preserve=0)),
        track('Pitch +3', item(0.5, 4.0, 'tone440.wav', 'p3', pitch=3.0)),
        track('Fade smoothstep', item(1.0, 5.0, 'noise.wav', 'fs', fin=1.0, fout=1.0, shape=5)),
        track('Item vol curve', item(1.0, 4.0, 'tone550.wav', 'ivc').replace(
            '      <SOURCE WAVE', '''      <VOLENV
        EGUID {00000000-0000-0000-0000-00000000E002}
        ACT 1 -1
        VIS 1 1 1
        LANEHEIGHT 0 0
        ARM 0
        DEFSHAPE 0 -1 -1
        PT 0 1 0
        PT 1 1 0
        PT 2.5 0.2 0
        PT 4 1 0
      >
      <SOURCE WAVE''')),
        track('Reversed', item(0.5, 3.0, 'noise2.wav', 'rev').replace(
            '      <SOURCE WAVE\n        FILE "noise2.wav"\n      >',
            '''      <SOURCE SECTION
        LENGTH 8
        MODE 3
        STARTPOS 0
        OVERLAP 0.01
        <SOURCE WAVE
          FILE "noise2.wav"
        >
      >''')),
        track('Keys', '''    <ITEM
      POSITION 0
      LENGTH 8
      NAME "keys"
      <SOURCE MIDI
        HASDATA 1 960 QN
        E 0 90 3c 64
        E 480 80 3c 00
        E 0 90 40 50
        E 960 80 40 00
        E 0 90 43 70
        E 1920 80 43 00
        E 960 90 48 7f
        E 240 80 48 00
        E 3600 90 30 20
        E 960 80 30 00
      >
    >
'''),
    ]
    rpp = '''<REAPER_PROJECT 0.1 "7.27/win64" 1727000000
  TEMPO 120 4 4
  PANLAW 1
  PANMODE 3
  SAMPLERATE 48000 0 0
  MARKER 1 2 "Two" 0 0 1
  MARKER 2 6 "Tempo" 0 0 1
  <TEMPOENVEX
    EGUID {00000000-0000-0000-0000-00000000E000}
    ACT 0 -1
    VIS 1 0 1
    LANEHEIGHT 0 0
    ARM 0
    DEFSHAPE 1 -1 -1
    PT 0 120 1
    PT 6 90 1
  >
%s>
''' % ''.join(tracks)
    with open(os.path.join(out, 'livetb.rpp'), 'w', newline='\n') as f:
        f.write(rpp)
    print('wrote', os.path.join(out, 'livetb.rpp'))


if __name__ == '__main__':
    main()
