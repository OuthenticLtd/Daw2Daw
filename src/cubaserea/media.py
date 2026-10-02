"""Audio files made into what Cubase can play, the way its importer does.

A Cubase clip describes a WAV down to its length in samples, and Cubase
plays a file at the project's sample rate whatever the file's own is. So a
REAPER project can point at things a .cpr cannot carry as they are: an MP3
or an OGG (Cubase reports the file missing), or a WAV at another rate (it
plays at the wrong speed and pitch). Cubase's own importer decodes both
into a WAV at the project rate in the project's Audio folder; this does the
same, before the events are written, so they point at the converted file.

Decoding is done by whatever is at hand: ffmpeg beside the tool or on the
PATH, else REAPER's own batch converter (reaper.exe -batchconvert). With
neither, the files are reported and left as they are.
"""
import array
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

# REAPER's render config for 24-bit WAV: the RENDER_CFG block a REAPER
# project saves when that is what it renders
_WAV24 = 'ZXZhdxgAAQ=='

_DECODE_EXT = ('.mp3', '.ogg', '.oga', '.flac', '.m4a', '.aac', '.wma',
               '.aif', '.aiff', '.opus', '.wav', '.w64', '.caf', '.mp4',
               '.rf64')


def tool_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def linux_arch():
    """The folder under linux/ that holds this machine's bundled Python and
    ffmpeg ('x86_64' or 'aarch64'), or None off Linux / on another CPU."""
    if not sys.platform.startswith('linux'):
        return None
    m = (os.uname().machine or '').lower()
    return {'x86_64': 'x86_64', 'amd64': 'x86_64',
            'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(m)


def find_ffmpeg():
    exe = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    dirs = [tool_dir(), os.path.join(tool_dir(), 'python'),
            os.path.join(tool_dir(), 'ffmpeg'),
            os.path.join(tool_dir(), 'ffmpeg', 'bin')]
    arch = linux_arch()
    if arch:
        # the static Linux build that ships with the tool
        dirs.insert(0, os.path.join(tool_dir(), 'linux', arch, 'ffmpeg'))
    for d in dirs:
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return shutil.which('ffmpeg')


def source_channels(path):
    """How many channels a media file has: from the header for a WAV,
    from ffmpeg's description for anything else. None when unknown."""
    try:
        n = wav_channels(path)
        if n:
            return n
    except (OSError, struct.error):
        pass
    ff = find_ffmpeg()
    if not ff or not os.path.isfile(path):
        return None
    try:
        r = subprocess.run([ff, '-hide_banner', '-i', path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    txt = r.stderr.decode('utf-8', 'replace')
    m = re.search(r'Audio: [^\n]*?\d+ Hz, ([^,\n]+)', txt)
    if not m:
        return None
    lay = m.group(1).strip().lower()
    if lay.startswith('mono'):
        return 1
    if lay.startswith('stereo'):
        return 2
    m2 = re.match(r'(\d+) channels', lay)
    if m2:
        return int(m2.group(1))
    m3 = re.match(r'(\d+)\.(\d+)', lay)
    if m3:
        return int(m3.group(1)) + int(m3.group(2))
    return None


def channel_filter(channels, src=None):
    """The ffmpeg filter that gives a file `channels` channels at unity.

    `-ac 2` on a mono file goes through libswresample's rematrix, which
    treats the one channel as a centre channel and mixes it to left and
    right at -3 dB (its centre level, which the mono upmix ignores when
    told otherwise). REAPER and Cubase both play a mono file on a stereo
    track at 0 dB on both sides, so every mono file that went through here
    arrived 3 dB quieter than it plays in REAPER. A mono source is copied
    to every output channel outright; anything else keeps ffmpeg's
    mapping, which leaves equal channel counts alone."""
    n = int(channels)
    layout = {1: 'mono', 2: 'stereo'}.get(n, '%dc' % n)
    in_ch = source_channels(src) if src else None
    if in_ch == 1 and n > 1:
        return 'pan=%s|%s' % (layout, '|'.join('c%d=c0' % i for i in range(n)))
    return 'aresample=ochl=%s' % layout


def reaper_wanted():
    """Whether the converter may run REAPER. It may not, unless asked: a
    conversion needs neither DAW, and a REAPER window opening while one
    runs is exactly what it promises not to do. CPR_USE_REAPER=1 lets it
    print host-dependent instruments, take FX and stretched audio through
    REAPER when that is installed; CPR_NO_REAPER=1 still forbids it."""
    if os.environ.get('CPR_NO_REAPER'):
        return False
    return bool(os.environ.get('CPR_USE_REAPER'))


def find_reaper():
    """reaper.exe when the converter is allowed to use it (reaper_wanted),
    else None. The test bench, which does need REAPER, uses
    find_reaper_exe."""
    return find_reaper_exe() if reaper_wanted() else None


def find_reaper_exe():
    """reaper.exe, from the registry or the usual folders (Windows only)."""
    if os.name != 'nt':
        return None
    found = []
    try:
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE,
             r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\REAPER'),
            (winreg.HKEY_CURRENT_USER,
             r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\REAPER'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\REAPER'),
        ]
        for hive, sub in keys:
            for view in (0, getattr(winreg, 'KEY_WOW64_64KEY', 0)):
                try:
                    with winreg.OpenKey(hive, sub, 0,
                                        winreg.KEY_READ | view) as k:
                        for val in ('DisplayIcon', 'InstallLocation',
                                    'UninstallString', ''):
                            try:
                                v, _ = winreg.QueryValueEx(k, val)
                            except OSError:
                                continue
                            v = str(v).strip('"').split('",')[0]
                            if v.lower().endswith('.exe'):
                                v = os.path.dirname(v)
                            found.append(os.path.join(v, 'reaper.exe'))
                except OSError:
                    pass
    except ImportError:
        pass
    for env in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)'):
        base = os.environ.get(env)
        if base:
            found.append(os.path.join(base, 'REAPER (x64)', 'reaper.exe'))
            found.append(os.path.join(base, 'REAPER', 'reaper.exe'))
    for p in found:
        if os.path.isfile(p):
            return p
    return None


_MP3_TAGGED = {}
MP3_GAPLESS_TRIM = 1105     # samples at the file's own rate: LAME's 576 + the decoder's 529


def mp3_gapless_tag(ff, src):
    """Does this MP3 carry encoder-delay (LAME/Xing) information? ffmpeg
    reports it as a non-zero stream start ('start: 0.025057'); a file
    without it (ElevenLabs exports, for one) starts at 0.000000."""
    import re
    key = os.path.normcase(src)
    if key in _MP3_TAGGED:
        return _MP3_TAGGED[key]
    tagged = False
    try:
        r = subprocess.run([ff, '-hide_banner', '-i', src], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        m = re.search(r'start:\s*([0-9.]+)', r.stderr.decode('utf-8', 'replace'))
        tagged = bool(m and float(m.group(1)) > 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        tagged = False
    _MP3_TAGGED[key] = tagged
    return tagged


def mp3_decode_args(ff, src, gapless):
    """(arguments before -i, audio filters) that make ffmpeg decode an MP3
    the way REAPER plays it, measured against REAPER's own renders
    (2026-09-28, Cherry Link):

    - FILE line without the flag: REAPER plays the decoder's full output,
      encoder delay and all, behind one frame (1152 samples) of silence;
      ffmpeg is told to keep every sample (skip_manual) and given the lead
      (nulls at -90 dB).
    - FILE line with the '1' (REAPER's gapless mode, what it writes for a
      freshly imported MP3): the LAME delay and padding are trimmed, which
      is ffmpeg's default when the file carries the tag (the print lined up
      to the sample). A file without the tag is still cut by 1105 samples
      at its own rate (576 encoder + 529 decoder delay) - REAPER trims that
      much whether or not a tag says so (measured 1211-1221 at 48k on two
      ElevenLabs files, so within 0.4 ms of 1105 at 44.1k)."""
    if not src.lower().endswith('.mp3'):
        return [], []
    if not gapless:
        return ['-flags2', '+skip_manual'], ['adelay=1152S:all=1']
    if mp3_gapless_tag(ff, src):
        return [], []
    return ['-flags2', '+skip_manual'], ['atrim=start_sample=%d' % MP3_GAPLESS_TRIM]


class Decoder:
    def __init__(self, log=None):
        self.log = log if log is not None else []
        self.ffmpeg = find_ffmpeg()
        self.reaper = None if self.ffmpeg else find_reaper()

    @property
    def name(self):
        if self.ffmpeg:
            return 'ffmpeg'
        if self.reaper:
            return 'REAPER'
        return None

    def available(self):
        return bool(self.ffmpeg or self.reaper)

    def convert(self, jobs, rate, channels=None):
        """jobs: [(source, destination wav)]. Returns the destinations
        that exist afterwards."""
        if not jobs:
            return []
        # A file collected into Audio and then found at the wrong rate or
        # channel count is its own destination; no decoder writes over
        # the file it is reading ("Error opening output files: Invalid
        # argument"), so it is made beside it and moved over it after.
        swap = {}
        run = []
        for job in jobs:
            src, dst = job[0], job[1]
            if os.path.normcase(os.path.abspath(src)) == \
                    os.path.normcase(os.path.abspath(dst)):
                tmp = os.path.splitext(dst)[0] + ' (converting).wav'
                swap[os.path.normcase(tmp)] = dst
                job = (src, tmp) + tuple(job[2:])
            run.append(job)
        if self.ffmpeg:
            made = self._ffmpeg(run, rate, channels)
        elif self.reaper:
            made = self._reaper(run, rate)
        else:
            made = []
        out = []
        for p in made:
            dst = swap.get(os.path.normcase(p))
            if dst:
                os.replace(p, dst)
                p = dst
            out.append(p)
        for tmp in swap:
            if os.path.isfile(tmp):
                os.remove(tmp)
        return out

    def _ffmpeg(self, jobs, rate, channels=None):
        done = []
        from . import progress
        bar = progress.Progress(len(jobs), 'converting audio', 'files')
        for job in jobs:
            bar.step(0, note=os.path.basename(job[0])[:28])
            self._ffmpeg_one(job, rate, channels, done)
            bar.step()
        bar.done()
        return done

    def _ffmpeg_one(self, job, rate, channels, done):
        # (source, destination[, gapless]): gapless is REAPER's '1'
        # behind the FILE path of an MP3, which trims the LAME delay
        # the way ffmpeg does by default
        src, dst = job[0], job[1]
        gapless = bool(job[2]) if len(job) > 2 else False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        is_mf = os.path.splitext(src)[1].lower() in _MF_EXT
        if is_mf and not channels and decode_like_reaper(src, dst, rate, self.log):
            done.append(dst)
            return
        cmd = [self.ffmpeg, '-v', 'error', '-y']
        filters = []
        if is_mf:
            # REAPER plays an MP4-family decode one frame early
            cmd += ['-flags2', '+skip_manual']
            filters.append('atrim=start_sample=1,asetpts=PTS-STARTPTS')
        if src.lower().endswith('.mp3'):
            # decoded the way REAPER plays it (mp3_decode_args)
            pre, flt = mp3_decode_args(self.ffmpeg, src, gapless)
            cmd += pre
            filters += flt
        cmd += ['-i', src, '-vn', '-map_metadata', '-1',
                '-ar', str(int(rate))]
        if channels:
            filters.append(channel_filter(channels, src))
        if filters:
            cmd += ['-af', ','.join(filters)]
        cmd += ['-c:a', 'pcm_s24le', '-rf64', 'auto', dst]
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log.append('ffmpeg failed on %s: %s'
                            % (os.path.basename(src), e))
            return
        if r.returncode == 0 and os.path.isfile(dst):
            done.append(dst)
        else:
            err = r.stderr.decode('utf-8', 'replace').strip()
            self.log.append('ffmpeg could not convert %s: %s'
                            % (os.path.basename(src),
                               err.splitlines()[-1] if err else
                               'exit %d' % r.returncode))

    def _reaper(self, jobs, rate):
        """One run of REAPER's batch converter for every file going to the
        same folder. The list format is the one the converter itself saves:
        a CONFIG block, then one source path per line."""
        done = []
        by_dir = {}
        for job in jobs:
            src, dst = job[0], job[1]
            by_dir.setdefault(os.path.dirname(dst), []).append((src, dst))
        for out_dir, items in by_dir.items():
            os.makedirs(out_dir, exist_ok=True)
            lines = ['<CONFIG',
                     '  SRATE %d' % int(rate),
                     '  RSMODE 9',
                     '  DITHER 0',
                     '  USESRCSTART 0',
                     '  PAD_START 0',
                     '  PAD_END 0',
                     "  OUTPATH '%s'" % out_dir,
                     "  OUTPATTERN '$source'",
                     '  <OUTFMT',
                     '    ' + _WAV24,
                     '  >',
                     '>']
            lines += [src for src, _ in items]
            fd, lst = tempfile.mkstemp(prefix='cubase-convert-',
                                       suffix='.txt')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
            try:
                subprocess.run([self.reaper, '-batchconvert', lst],
                               timeout=3600)
            except (OSError, subprocess.TimeoutExpired) as e:
                self.log.append('REAPER batch conversion failed: %s' % e)
            logf = lst + '.log'
            if os.path.isfile(logf):
                try:
                    txt = open(logf, encoding='utf-8', errors='replace')\
                        .read().strip()
                    if txt:
                        self.log.append('REAPER batch converter said: %s'
                                        % txt.splitlines()[-1])
                except OSError:
                    pass
            for src, dst in items:
                # the converter names the output after the source, with its
                # own extension
                made = os.path.join(out_dir, os.path.splitext(
                    os.path.basename(src))[0] + '.wav')
                if os.path.isfile(made):
                    if os.path.normcase(made) != os.path.normcase(dst):
                        try:
                            os.replace(made, dst)
                        except OSError:
                            dst = made
                    done.append(dst)
            for p in (lst, logf):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return done


def resolve(path, srcdir=''):
    """A media path as REAPER wrote it, made absolute for this machine.

    REAPER writes paths relative to the .rpp and with backslashes; on a
    machine that is not Windows the backslashes have to become separators
    or nothing is found."""
    if not path:
        return path
    if os.sep == '/':
        path = path.replace('\\', '/')
    if (len(path) > 2 and path[1] == ':' and path[2] in '\\/'):
        return os.path.normpath(path)          # a Windows drive path
    if srcdir and not os.path.isabs(path):
        path = os.path.join(srcdir, path)
    return os.path.normpath(path)


def wav_rate(path, wav_info):
    """(is a WAV, its sample rate) for a file, via the caller's WAV reader."""
    info = wav_info(path) if path else None
    if info:
        return True, float(info[3])
    return False, None


_found = {}


def locate(path, roots):
    """A file of this name somewhere under one of `roots`, or None.

    REAPER remembers where a file was when it was added, and a project that
    has moved machines points at folders that are not there. REAPER itself
    then looks in the project folder; so does this: every folder under the
    project (Media, Audio, subfolders) is listed once and searched by name."""
    name = os.path.basename(path or '')
    if not name:
        return None
    key = name.lower()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        idx = _found.get(os.path.normcase(root))
        if idx is None:
            idx = {}
            depth0 = root.rstrip(os.sep).count(os.sep)
            for dp, dns, fns in os.walk(root):
                # stay out of the converter's own output folders and of
                # the caches Cubase and REAPER keep beside a project
                dns[:] = [d for d in dns
                          if not d.startswith('.')
                          and d.lower() not in ('images', 'peaks',
                                                'track pictures',
                                                'backups', 'auto saves')
                          and dp.count(os.sep) - depth0 < 6]
                for f in fns:
                    idx.setdefault(f.lower(), os.path.join(dp, f))
            _found[os.path.normcase(root)] = idx
        hit = idx.get(key)
        if hit:
            return hit
    return None


def relink(project, out_path, log, items=None):
    """Point items at files that exist, when the named one does not.

    Returns (relinked, still missing) as lists of paths."""
    if items is None:
        items = [i for t in project.tracks for i in t.items]
    roots = [project.srcdir,
             os.path.dirname(os.path.abspath(out_path))]
    fixed, missing = {}, {}
    for it in items:
        if it.kind not in ('audio', 'video') or not it.file:
            continue
        it.file = resolve(it.file, project.srcdir)
        if os.path.isfile(it.file):
            continue
        alt = locate(it.file, roots)
        if alt:
            fixed[it.file] = alt
            it.file = alt
        else:
            missing[os.path.normcase(it.file)] = it.file
    if fixed:
        log.append('%d file(s) were not where the project said and '
                   'were found by name in the project folder instead: %s'
                   % (len(fixed), ', '.join(sorted(
                       os.path.basename(k) for k in fixed))))
    if missing:
        log.append('%d file(s) are not on this machine at all and stay as '
                   'named - the DAW will report them missing: %s'
                   % (len(missing), '; '.join(sorted(missing.values()))))
    return list(fixed), list(missing.values())


def _drop_first_frames(path, n=1):
    """Rewrite a PCM WAV without its first `n` frames (in place)."""
    import struct as _st
    with open(path, 'rb') as f:
        d = bytearray(f.read())
    o = 12
    fmt_align = None
    while o < len(d) - 8:
        cid = bytes(d[o:o + 4])
        size = _st.unpack_from('<I', d, o + 4)[0]
        if cid == b'fmt ':
            fmt_align = _st.unpack_from('<H', d, o + 8 + 12)[0]
        elif cid == b'data' and fmt_align:
            cut = fmt_align * n
            del d[o + 8:o + 8 + cut]
            _st.pack_into('<I', d, o + 4, size - cut)
            _st.pack_into('<I', d, 4, len(d) - 8)
            with open(path, 'wb') as f:
                f.write(d)
            return True
        o += 8 + size + (size & 1)
    return False


# REAPER decodes MP4/M4A/AAC with Windows Media Foundation and plays the
# decode one sample earlier than it comes out of the decoder (measured
# 2026-10-01 on three files, both with Media Foundation and ffmpeg). Media
# Foundation's decode of an AAC stream matches REAPER's to -78 dB where
# ffmpeg's comes to -35 (AAC's noise substitution is the decoder's own).
MF_DECODE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mf_decode.ps1')
_MF_EXT = ('.mp4', '.m4a', '.m4v', '.mov', '.aac', '.3gp')


def decode_like_reaper(src, dst, rate, log=None):
    """Decode a compressed file's audio to a WAV that plays like REAPER's
    own decode: Media Foundation on Windows, ffmpeg otherwise, and the
    first frame dropped. Returns True when dst was written."""
    ext = os.path.splitext(src)[1].lower()
    ch = source_channels(src) or 2
    if os.name == 'nt' and ext in _MF_EXT and os.path.isfile(MF_DECODE)             and not os.environ.get('CPR_NO_MF'):
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                '-File', MF_DECODE, '-src', src, '-dst', dst,
                                '-rate', str(int(rate)), '-bits', '32',
                                '-channels', str(int(ch))],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=3600)
            if r.returncode == 0 and os.path.isfile(dst) and _drop_first_frames(dst):
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    ff = find_ffmpeg()
    if not ff:
        return False
    cmd = [ff, '-v', 'error', '-y', '-flags2', '+skip_manual',
           '-i', src, '-vn', '-map_metadata', '-1',
           '-af', 'atrim=start_sample=1,asetpts=PTS-STARTPTS',
           '-ar', str(int(rate)), '-c:a', 'pcm_s24le', '-rf64', 'auto', dst]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and os.path.isfile(dst)


def extract_video_audio(project, out_path, items, log):
    """The audio stream of each video item's file as a WAV in the
    project's Audio folder, at the project rate. Returns {id(item): path}
    for the files that have a stream and came out."""
    ff = find_ffmpeg()
    if not ff:
        if log is not None:
            log.append('%d video item(s) play audio in REAPER, and ffmpeg was '
                       'not found to extract it' % len(items))
        return {}
    audio_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)), 'Audio')
    os.makedirs(audio_dir, exist_ok=True)
    rate = int(project.samplerate or 48000)
    done = {}
    made = {}
    for it in items:
        src = resolve(it.file, project.srcdir)
        if not os.path.isfile(src):
            continue
        key = os.path.normcase(src)
        if key in done:
            made[id(it)] = done[key]
            continue
        if source_channels(src) is None:
            done[key] = None            # no audio stream
            continue
        dst = os.path.join(audio_dir, os.path.splitext(os.path.basename(src))[0]
                           + ' (video audio).wav')
        if not (os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src)):
            # REAPER plays the stream's full decode, AAC priming samples
            # included (the extraction sat 1023 samples early against
            # REAPER's render without skip_manual), one frame earlier than
            # the decoder gives it, and through Media Foundation
            if not decode_like_reaper(src, dst, rate, log):
                log.append('the audio of %s could not be extracted'
                           % os.path.basename(src))
                continue
        done[key] = dst
        made[id(it)] = dst
    return made


def probe(path):
    """(duration seconds, frames per second) of a media file via ffmpeg,
    either None when it cannot be read."""
    ff = find_ffmpeg()
    if not ff or not os.path.isfile(path):
        return None, None
    try:
        r = subprocess.run([ff, '-hide_banner', '-i', path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    txt = r.stderr.decode('utf-8', 'replace')
    dur = fps = None
    m = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', txt)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r'(\d+(?:\.\d+)?) fps', txt)
    if m:
        fps = float(m.group(1))
    return dur, fps


def _interp(points, t):
    """Linear interpolation over (time, value) points, held flat outside."""
    if not points:
        return None
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            if t1 <= t0:
                return v1
            f = (t - t0) / (t1 - t0)
            return v0 + f * (v1 - v0)
    return points[-1][1]


def curved_fade(it):
    """Does the item fade with a shape other than REAPER's linear one?

    The FADEIN/FADEOUT line is shape, length, curve...; shape 0 is the
    straight line, which is what Cubase's fade record draws too. Any other
    shape is printed into the audio, since Cubase's fade curve is not
    written and a 0.17 s fast-start fade-out was the whole of what kept
    a brass render from nulling."""
    from .rpp_read import fade_length, fade_shape
    for key in ('FADEIN', 'FADEOUT'):
        toks = (getattr(it, 'fade_lines', None) or {}).get(key)
        if not toks or len(toks) < 2:
            continue
        try:
            length = fade_length(toks)
            shape, curve = fade_shape(toks)
        except ValueError:
            continue
        # REAPER's 1 ms auto-fade on every edit is below hearing whatever
        # its shape; only a fade one could hear is worth a print
        if length > 0.01 and (shape != 0 or abs(curve) > 1e-9):
            return True
    return False


def source_end(it):
    """The source second an item has reached when it ends: SOFFS plus its
    length at its playrate, or - with stretch markers - where the markers
    put the item's end (past the last one the take runs at its playrate;
    positions are take time, item seconds times the playrate)."""
    r = it.playrate or 1.0
    tau = it.length * r
    mk = sorted(getattr(it, 'stretch_markers', None) or [])
    if not mk:
        return it.soffs + tau
    if tau <= mk[0][0]:
        return mk[0][1] - (mk[0][0] - tau)
    for (ta, sa), (tb, sb) in zip(mk, mk[1:]):
        if ta <= tau <= tb:
            return sa + (sb - sa) * (tau - ta) / (tb - ta) if tb > ta else sb
    return mk[-1][1] + (tau - mk[-1][0])


def needs_render(it):
    """Does this item play something Cubase cannot express on the event?

    A stretched item (PLAYRATE), a pitch-shifted one, or one with a volume
    or pan curve drawn inside it. Cubase has musical mode for stretch, but
    no verified way to write it, no per-event pitch that can be written,
    and no per-event pan at all - so the audio the item plays is rendered
    to a file of its own, the way Bounce Selection would."""
    if it.kind != 'audio' or not it.file:
        return False
    if getattr(it, 'takefx', None):
        return True
    if getattr(it, 'section', None):
        return True                 # a reversed or cut source (<SOURCE SECTION>)
    if getattr(it, 'stretch_markers', None)             and not getattr(it, 'native_stretch', False):
        return True                 # warped between stretch markers (the
                                    # builder writes them as warp tabs)
    # a curved fade is no longer a reason to print: the shape goes to Cubase
    # as points of its linear fade interpolator (fades.py, fadetpl.blob)
    # a pitch shift on its own is the Cubase event's Transpose/Fine-tune
    # (cpr_build.write_pitch, the 'FtiP' record), so it goes across as it
    # is; only a stretch still has to be printed (Cherry Link's 49 pitched
    # items were rendered through ffmpeg's pitch shifter instead)
    # A plain stretch goes to Cubase as the event's own musical-mode stretch
    # on elastique (cpr_build.write_stretch): the builder marks the items it
    # can carry that way, and only the rest are printed
    if abs((it.playrate or 1.0) - 1.0) > 1e-6 \
            and not getattr(it, 'native_stretch', False):
        return True
    if it.volenv and (len(it.volenv) > 1 or abs(it.volenv[0][1] - 1.0) > 1e-4)             and not getattr(it, 'native_curve', False):
        return True
    if it.panenv and (len(it.panenv) > 1 or abs(it.panenv[0][1]) > 1e-4):
        return True
    if it.pitchenv and (len(it.pitchenv) > 1 or abs(it.pitchenv[0][1]) > 1e-4):
        return True
    if it.loop:
        # a looped item runs past the end of its source; a Cubase event
        # stops there, so the loop is printed out
        # (an MP3's length too: read off a WAV header alone, every looped
        # MP3 counted as running past its end and was printed - three
        # tempo-mapped songs in the ThreePots projects that end 60 s in)
        try:
            dur = wav_duration(it.file)
            if dur is None:
                dur = decoded_duration(it.file, '1' in [str(a) for a in
                                                        (getattr(it, 'file_args', None) or [])])
        except Exception:
            dur = None
        if dur is None or source_end(it) > dur + 1e-3:
            return True
    return False


def _render_tag(it):
    bits = []
    sec = getattr(it, 'section', None)
    if sec and sec.get('reverse'):
        bits.append('rev')
    elif sec:
        bits.append('section')
    if abs((it.playrate or 1.0) - 1.0) > 1e-6:
        bits.append('x%.3g' % it.playrate)
    if abs(it.pitch or 0.0) > 1e-6:
        bits.append('%+.2gst' % it.pitch)
    if it.volenv and (len(it.volenv) > 1 or abs(it.volenv[0][1] - 1.0) > 1e-4):
        bits.append('vol')
    if it.panenv and (len(it.panenv) > 1 or abs(it.panenv[0][1]) > 1e-4):
        bits.append('pan')
    if it.pitchenv and (len(it.pitchenv) > 1 or abs(it.pitchenv[0][1]) > 1e-4):
        bits.append('pitch curve')
    if it.loop:
        bits.append('looped')
    return ' '.join(bits)


def _apply_envelopes(raw_f32, channels, rate, volenv, panenv):
    """Take envelopes onto interleaved float samples, in place.

    Times are seconds from the start of the item on the timeline. REAPER's
    take pan works as stereo balance (PANMODE 3, the project default here):
    panning right attenuates the left channel and leaves the right alone."""
    a = array.array('f')
    a.frombytes(raw_f32)
    if sys.byteorder != 'little':
        a.byteswap()
    n = len(a) // channels
    vol = volenv if (volenv and (len(volenv) > 1
                                 or abs(volenv[0][1] - 1.0) > 1e-4)) else None
    pan = panenv if (panenv and channels >= 2
                     and (len(panenv) > 1 or abs(panenv[0][1]) > 1e-4)) else None
    if vol is None and pan is None:
        return raw_f32
    # a curve is evaluated once per block of samples and interpolated inside
    # the block, so a long item does not cost one Python call per sample
    BLOCK = 64
    i = 0
    while i < n:
        j = min(n, i + BLOCK)
        t0, t1 = i / rate, j / rate
        g0 = _interp(vol, t0) if vol else 1.0
        g1 = _interp(vol, t1) if vol else 1.0
        p0 = _interp(pan, t0) if pan else 0.0
        p1 = _interp(pan, t1) if pan else 0.0
        m = j - i
        for k in range(m):
            f = k / float(m)
            g = g0 + f * (g1 - g0)
            p = p0 + f * (p1 - p0)
            base = (i + k) * channels
            if pan is not None:
                a[base] *= g * (1.0 - max(p, 0.0))
                a[base + 1] *= g * (1.0 + min(p, 0.0))
                for c in range(2, channels):
                    a[base + c] *= g
            else:
                for c in range(channels):
                    a[base + c] *= g
        i = j
    if sys.byteorder != 'little':
        a.byteswap()
    return a.tobytes()


def render(project, out_path, rate, wav_info, log, items=None, channels=None):
    """Render every item that needs it (see needs_render) into
    <project>/Audio as a WAV holding exactly what the item plays, and point
    the item at that file from offset zero.

    Returns the list of items rendered."""
    if items is None:
        items = [i for t in project.tracks for i in t.items]
    todo = [i for i in items if needs_render(i)]
    if not todo:
        return []
    ff = find_ffmpeg()
    if not ff:
        log.append('%d item(s) are stretched, pitch-shifted or carry a curve '
                   'drawn inside the item; rendering them needs ffmpeg, which '
                   'was not found, so they arrive at their original speed '
                   'and pitch with the curve dropped' % len(todo))
        return []
    audio_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                             'Audio')
    os.makedirs(audio_dir, exist_ok=True)
    # REAPER itself prints whatever it can - the result is then exactly what
    # REAPER played, stretch algorithm, pitch curve, item FX and all. ffmpeg
    # takes over only where reaper.exe is not to be found.
    from . import render_reaper
    reaper = None if os.environ.get('CPR_NO_REAPER') else find_reaper()
    if reaper:
        with_fx = [i for i in todo
                   if os.path.isfile(resolve(i.file, project.srcdir))]
    else:
        with_fx = [i for i in todo if render_reaper.needs_reaper(i)
                   and os.path.isfile(resolve(i.file, project.srcdir))]
    printed_fx = []
    if with_fx:
        for i in with_fx:
            i.file = resolve(i.file, project.srcdir)
        got = render_reaper.render(with_fx, audio_dir, rate, log,
                                   reaper=reaper, channels=channels,
                                   header=getattr(project, 'header_keep', ()),
                                   tempo=(project.tempo[0][1]
                                          if project.tempo else None))
        for it, p in got.items():
            it.file = p
            it.soffs = 0.0
            it.playrate = 1.0
            it.pitch = 0.0
            it.warped = False
            it.stretch_markers = []
            if curved_fade(it):
                # the fades are in the print now
                it.fadein = it.fadeout = 0.0
            it.fade_lines = {}
            it.volenv = []
            it.panenv = []
            it.pitchenv = []
            it.takefx = None
            it.loop = False
            it.section = None
            it.file_args = []
            printed_fx.append(it)
        if got:
            log.append('%d item(s) were printed through REAPER to their own '
                       'WAV in Audio ("[printed]"), exactly as REAPER plays '
                       'them: stretch, pitch, item curves and item FX '
                       'included' % len(got))
        todo = [i for i in todo if i not in got]
        for i in todo:
            if getattr(i, 'takefx', None):
                i.takefx = None     # could not be printed; render the rest
    done = {}
    taken = set(f.lower() for f in os.listdir(audio_dir))
    rendered = []
    failed = []
    for it in todo:
        src = resolve(it.file, project.srcdir)
        if not os.path.isfile(src):
            continue
        key = (os.path.normcase(src), round(it.soffs, 6), round(it.length, 6),
               round(it.playrate, 6), round(it.pitch, 4), bool(it.preserve_pitch),
               tuple(it.volenv), tuple(it.panenv), tuple(it.pitchenv),
               repr(getattr(it, 'section', None)),
               tuple(str(a) for a in (getattr(it, 'file_args', None) or [])))
        dst = done.get(key)
        if dst is None:
            stem = os.path.splitext(os.path.basename(src))[0]
            tag = _render_tag(it)
            n = 0
            while True:
                n += 1
                cand = '%s [%s%s].wav' % (stem, tag, '' if n == 1 else ' %d' % n)
                if cand.lower() not in taken:
                    break
            taken.add(cand.lower())
            dst = os.path.join(audio_dir, cand)
            ok = _render_one(ff, src, dst, it, rate, log, channels)
            if not ok:
                failed.append(src)
                continue
            done[key] = dst
        it.file = dst
        it.soffs = 0.0
        it.playrate = 1.0
        it.pitch = 0.0
        it.warped = False
        it.stretch_markers = []
        it.volenv = []
        it.panenv = []
        it.pitchenv = []
        it.loop = False
        it.section = None
        it.file_args = []
        rendered.append(it)
    rendered = printed_fx + rendered
    if rendered:
        log.append('%d item(s) were rendered to their own WAV in Audio, as '
                   'Bounce Selection would, because Cubase cannot hold what '
                   'REAPER did to them on the event itself: a stretch '
                   '(PLAYRATE), a pitch shift, or a volume or pan curve drawn '
                   'inside the item. The files are named after the source '
                   'with the change in brackets' % len(rendered))
    if failed:
        log.append('%d item(s) could not be rendered and arrive unstretched: %s'
                   % (len(failed), ', '.join(sorted(set(
                       os.path.basename(f) for f in failed)))))
    return rendered


def _render_one(ff, src, dst, it, rate, log, channels=None):
    """ffmpeg cuts the source region and applies stretch and pitch; the
    volume and pan curves, which ffmpeg cannot follow point by point, go on
    in Python; a pitch curve is followed by re-tuning rubberband every few
    milliseconds through ffmpeg's command interface."""
    rate = int(rate or 48000)
    playrate = it.playrate or 1.0
    src_len = max(1e-4, it.length * playrate)
    # The exact stretch of source the item plays, as a plain WAV: an MP3
    # decoded the way its FILE flag says, a <SOURCE SECTION> cut or
    # reversed, looped if the item loops, then cut to [soffs, soffs +
    # src_len). The stretch below then starts at 0 and lasts it.length.
    # Seeking after the stretch filter instead (the old way) seeked on the
    # stretched time axis: a reversed take at rate 0.778 came out 26 dB
    # quiet and 22% short.
    sec = getattr(it, 'section', None)
    gapless = '1' in [str(a) for a in (getattr(it, 'file_args', None) or [])]
    prepared = _prepared_source(ff, src, sec, gapless, log,
                                soffs=max(0.0, it.soffs or 0.0), src_len=src_len,
                                loop=bool(it.loop))
    if prepared is None:
        return False
    src = prepared
    filters = []
    semis = it.pitch or 0.0
    penv = it.pitchenv if (it.pitchenv and (len(it.pitchenv) > 1
                                            or abs(it.pitchenv[0][1]) > 1e-4)) else None
    cmdfile = None
    if abs(playrate - 1.0) > 1e-6 and not it.preserve_pitch:
        # a varispeed stretch shifts the pitch along with the speed
        filters.append('asetrate=%d' % int(round(rate * playrate)))
        filters.append('aresample=%d' % rate)
        playrate_rb = 1.0
    else:
        playrate_rb = playrate
    if penv is not None:
        # one pitch command every 10 ms along the item, interpolated, on
        # top of the item's own pitch shift
        cmdfile = os.path.join(os.path.dirname(dst), '_pitch_%d.txt' % id(it))
        lines = []
        t = 0.0
        while t <= it.length + 1e-9:
            st = semis + (_interp(penv, t) or 0.0)
            lines.append('%.4f rubberband pitch %.9g;' % (t, 2.0 ** (st / 12.0)))
            t += 0.01
        with open(cmdfile, 'w', encoding='ascii') as f:
            f.write('\n'.join(lines) + '\n')
        filters.append('asendcmd=f=%s' % os.path.basename(cmdfile))
        filters.append('rubberband=tempo=%.9g:pitch=%.9g'
                       % (playrate_rb, 2.0 ** (semis / 12.0)))
        filters += _rubberband_align(ff, src, playrate_rb, 2.0 ** (semis / 12.0), log)
    elif abs(playrate_rb - 1.0) > 1e-6 or abs(semis) > 1e-6:
        filters.append('rubberband=tempo=%.9g:pitch=%.9g'
                       % (playrate_rb, 2.0 ** (semis / 12.0)))
        filters += _rubberband_align(ff, src, playrate_rb, 2.0 ** (semis / 12.0), log)
    want_env = bool(it.volenv or it.panenv)
    info = None
    try:
        info = wav_channels(src)
    except Exception:
        info = None
    want = channels
    channels = want or info or 2
    if it.panenv and channels < 2:
        channels = 2
    fd, tmp = tempfile.mkstemp(prefix='cubase-render-', suffix='.raw')
    os.close(fd)
    try:
        # the prepared source holds exactly the stretch the item plays; the
        # output lasts the item's length (the stretch changes the duration)
        cmd = [ff, '-v', 'error', '-y', '-i', src, '-t', '%.9f' % max(1e-4, it.length),
               '-vn', '-map_metadata', '-1', '-ar', str(rate)]
        # the channel count is set through the filter chain, at unity gain
        filters = list(filters) + [channel_filter(channels, src)]
        if filters:
            cmd += ['-af', ','.join(filters)]
        if want_env:
            cmd += ['-f', 'f32le', '-c:a', 'pcm_f32le', tmp]
        else:
            cmd += ['-c:a', 'pcm_s24le', '-rf64', 'auto', dst]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=1800, cwd=os.path.dirname(dst))
        if cmdfile:
            try:
                os.remove(cmdfile)
            except OSError:
                pass
        if r.returncode != 0:
            err = r.stderr.decode('utf-8', 'replace').strip().splitlines()
            log.append('ffmpeg could not render %s: %s'
                       % (os.path.basename(src), err[-1] if err else r.returncode))
            return False
        if not want_env:
            return os.path.isfile(dst)
        raw = open(tmp, 'rb').read()
        raw = _apply_envelopes(raw, channels, rate, it.volenv, it.panenv)
        open(tmp, 'wb').write(raw)
        cmd = [ff, '-v', 'error', '-y', '-f', 'f32le', '-ar', str(rate),
               '-ac', str(channels), '-i', tmp, '-c:a', 'pcm_s24le',
               '-rf64', 'auto', dst]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=1800)
        if r.returncode != 0:
            err = r.stderr.decode('utf-8', 'replace').strip().splitlines()
            log.append('ffmpeg could not write %s: %s'
                       % (os.path.basename(dst), err[-1] if err else r.returncode))
            return False
        return os.path.isfile(dst)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.append('rendering %s failed: %s' % (os.path.basename(src), e))
        return False
    finally:
        for p in (tmp, prepared):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


_RB_LATENCY = {}


def _wav_rate_of(path):
    with open(path, 'rb') as f:
        head = f.read(12)
        if head[:4] not in (b'RIFF', b'RF64') or head[8:12] != b'WAVE':
            return None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                return None
            cid, size = hdr[:4], struct.unpack('<I', hdr[4:])[0]
            if cid == b'fmt ':
                body = f.read(size)
                return struct.unpack_from('<I', body, 4)[0]
            f.seek(size + (size & 1), 1)


def _rubberband_align(ff, src, tempo, pitch, log):
    """Filters that take rubberband's processing delay back out.

    ffmpeg's rubberband filter puts its output some milliseconds late (14
    ms at tempo 0.778, 27 ms with a pitch shift on top, measured against
    REAPER's render of the same items) and the delay depends on the
    settings. An impulse is sent through the same filter once per
    (rate, tempo, pitch) and its landing place gives the delay, which is
    then trimmed (or padded) right after the filter, at the source rate.

    OFF unless CPR_RB_ALIGN=1: the impulse says the filter runs 431 samples
    early at tempo 0.778, but against REAPER's own render the same stretch
    came out 693 samples LATE, and correcting by the impulse made it worse
    (2026-09-28). The relation between rubberband's delay and REAPER's
    elastique timing is not understood; without it the render sits 14-27
    ms off REAPER on a stretched or pitched take (level and length right)."""
    if not os.environ.get('CPR_RB_ALIGN'):
        return []
    try:
        rate = _wav_rate_of(src) or 48000
    except Exception:
        rate = 48000
    key = (int(rate), round(float(tempo), 6), round(float(pitch), 6))
    lat = _RB_LATENCY.get(key)
    if lat is None:
        lat = 0
        n0 = int(rate) // 2
        n = int(rate)
        fd, imp = tempfile.mkstemp(prefix='cubase-rb-', suffix='.wav')
        os.close(fd)
        fd, out = tempfile.mkstemp(prefix='cubase-rb-', suffix='.raw')
        os.close(fd)
        try:
            data = bytearray(n * 4)
            struct.pack_into('<f', data, n0 * 4, 1.0)
            with open(imp, 'wb') as f:
                f.write(b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVE'
                        + b'fmt ' + struct.pack('<IHHIIHH', 16, 3, 1, int(rate),
                                                int(rate) * 4, 4, 32)
                        + b'data' + struct.pack('<I', len(data)) + bytes(data))
            cmd = [ff, '-v', 'error', '-y', '-i', imp,
                   '-af', 'rubberband=tempo=%.9g:pitch=%.9g' % (tempo, pitch),
                   '-f', 'f32le', '-c:a', 'pcm_f32le', out]
            if _ff_run(ff, cmd, 'measure rubberband delay', log):
                raw = open(out, 'rb').read()
                vals = array.array('f')
                vals.frombytes(raw[:len(raw) - len(raw) % 4])
                if len(vals):
                    peak = max(range(len(vals)), key=lambda i: abs(vals[i]))
                    lat = int(round(peak - n0 / float(tempo)))
        except Exception as e:
            log.append('rubberband delay measurement failed: %s' % e)
            lat = 0
        finally:
            for p in (imp, out):
                try:
                    os.remove(p)
                except OSError:
                    pass
        _RB_LATENCY[key] = lat
    if lat > 0:
        return ['atrim=start_sample=%d' % lat]
    if lat < 0:
        return ['adelay=%dS:all=1' % (-lat)]
    return []


def _ff_run(ff, cmd, what, log):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.append('%s failed: %s' % (what, e))
        return False
    if r.returncode != 0:
        err = r.stderr.decode('utf-8', 'replace').strip().splitlines()
        log.append('ffmpeg could not %s: %s' % (what, err[-1] if err else r.returncode))
        return False
    return True


def _prepared_source(ff, src, sec, gapless, log, soffs=None, src_len=None,
                     loop=False):
    """A temporary WAV holding the source the way REAPER reads it, before
    the item's stretch applies.

    Pass one: an MP3 is decoded as its FILE flag says (Decoder: gapless, or
    the full decode behind one frame); a <SOURCE SECTION> in MODE 0-2 is cut
    to its STARTPOS/LENGTH; one whose MODE has bit 2 set (REAPER's
    'reverse', MODE 3 for the whole file) is turned round, so the item's
    SOFFS then counts from the file's end exactly as REAPER counts it.

    Pass two, when `soffs`/`src_len` are given: the stretch of that source
    the item plays, [soffs, soffs + src_len), the source repeated first if
    the item loops (a looped item runs past its source's end and wraps).
    The file keeps its own rate; the caller resamples. Returns the path,
    or None."""
    fd, tmp = tempfile.mkstemp(prefix='cubase-source-', suffix='.wav')
    os.close(fd)
    cmd = [ff, '-v', 'error', '-y']
    filters = []
    if src.lower().endswith('.mp3'):
        pre, flt = mp3_decode_args(ff, src, gapless)
        cmd += pre
        filters += flt
    if sec and int(sec.get('mode', 0)) in (0, 1, 2) and (sec.get('length') or 0) > 0:
        cmd += ['-ss', '%.9f' % max(0.0, sec.get('startpos', 0.0) or 0.0),
                '-t', '%.9f' % sec['length']]
    cmd += ['-i', src, '-vn', '-map_metadata', '-1']
    if sec and sec.get('reverse'):
        filters.append('areverse')
    if filters:
        cmd += ['-af', ','.join(filters)]
    cmd += ['-c:a', 'pcm_f32le', '-rf64', 'auto', tmp]
    if not _ff_run(ff, cmd, 'prepare %s' % os.path.basename(src), log) \
            or not os.path.isfile(tmp):
        return None
    if soffs is None or src_len is None:
        return tmp
    fd, cut = tempfile.mkstemp(prefix='cubase-source-', suffix='.wav')
    os.close(fd)
    cmd = [ff, '-v', 'error', '-y']
    if loop:
        cmd += ['-stream_loop', '-1']
    # no filter in this pass, so the output-side seek is exact
    cmd += ['-i', tmp, '-ss', '%.9f' % soffs, '-t', '%.9f' % max(1e-4, src_len),
            '-c:a', 'pcm_f32le', '-rf64', 'auto', cut]
    ok = _ff_run(ff, cmd, 'cut %s' % os.path.basename(src), log)
    try:
        os.remove(tmp)
    except OSError:
        pass
    if not ok or not os.path.isfile(cut):
        return None
    return cut


_DECODED_DUR = {}


def decoded_duration(path, gapless=False, rate=48000.0):
    """Seconds of audio a non-WAV file decodes to the way prepare() decodes
    it (mp3_decode_args, gapless as the FILE line says), cached per file.
    None when it cannot be decoded here."""
    try:
        key = (os.path.normcase(os.path.abspath(path)), os.path.getmtime(path),
               bool(gapless), float(rate))
    except OSError:
        return None
    if key in _DECODED_DUR:
        return _DECODED_DUR[key]
    d = None
    fd, tmp = tempfile.mkstemp(prefix='cubase-dur-', suffix='.wav')
    os.close(fd)
    try:
        dec = Decoder([])
        if dec.available() and dec.convert([(path, tmp, gapless)], rate):
            d = wav_duration(tmp)
    except Exception:
        d = None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    _DECODED_DUR[key] = d
    return d


def _wav_layout(path):
    """(header bytes, data offset, data bytes, frame bytes) of a PCM or
    float WAV (RIFF, or RF64 with its ds64 size), or None."""
    with open(path, 'rb') as f:
        head = f.read(12)
        if head[:4] not in (b'RIFF', b'RF64') or head[8:12] != b'WAVE':
            return None
        ds64 = None
        frame = None
        while True:
            at = f.tell()
            hdr = f.read(8)
            if len(hdr) < 8:
                return None
            cid, size = hdr[:4], struct.unpack('<I', hdr[4:])[0]
            if cid == b'ds64':
                body = f.read(size)
                ds64 = struct.unpack_from('<Q', body, 8)[0]
                f.seek(size & 1, 1)
            elif cid == b'fmt ':
                body = f.read(size)
                ch = struct.unpack_from('<H', body, 2)[0]
                frame = struct.unpack_from('<H', body, 12)[0]   # block align
                if not frame:
                    bits = struct.unpack_from('<H', body, 14)[0]
                    frame = max(1, ch) * ((bits + 7) // 8)
                f.seek(size & 1, 1)
            elif cid == b'data':
                if size == 0xFFFFFFFF and ds64 is not None:
                    size = ds64
                if not frame:
                    return None
                f.seek(0)
                return f.read(at + 8), at + 8, size - size % frame, frame
            else:
                f.seek(size + (size & 1), 1)


def reverse_wav(src, dst):
    """`src` played backwards, sample for sample, in its own format: what
    Cubase's Audio > Process > Reverse writes into the project's Edits
    folder (and then plays). Returns True when written."""
    lay = _wav_layout(src)
    if lay is None:
        return False
    header, off, size, frame = lay
    with open(src, 'rb') as f:
        f.seek(off)
        data = f.read(size)
    out = bytearray(len(data))
    # each byte of a frame reversed on its own lane keeps the frames whole
    for j in range(frame):
        out[j::frame] = data[j::frame][::-1]
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    tmp = dst + '.part'
    with open(tmp, 'wb') as f:
        f.write(header)
        f.write(out)
        if len(out) & 1:
            f.write(b'\0')
    os.replace(tmp, dst)
    return True


def reverse_sections(project, out_path, log, items=None):
    """REAPER's reversed takes the way Cubase holds a reversed event: the
    file reversed into <project>/Edits ("<name>-Reverse-<id>.wav", Cubase's
    own naming) and an ordinary event on it. REAPER's reversed take (SOURCE
    SECTION, MODE 3, STARTPOS 0) plays the whole file backwards with SOFFS
    counted into the reversed audio - the reversed file's own offset - so
    the event keeps its offset, fades, volume and stretch, and nothing of
    the item is printed. A section that cuts the file (STARTPOS, or no
    reverse) is left to the print. Returns how many items were reversed."""
    import uuid
    edits = os.path.join(os.path.dirname(os.path.abspath(out_path)), 'Edits')
    done = {}
    n = 0
    for it in (items if items is not None else [i for t in project.tracks for i in t.items]):
        sec = getattr(it, 'section', None)
        if it.kind != 'audio' or not it.file or not sec or not sec.get('reverse'):
            continue
        if abs(sec.get('startpos') or 0.0) > 1e-9:
            continue
        src = resolve(it.file, project.srcdir)
        if not os.path.isfile(src):
            continue
        dur = wav_duration(src)
        if dur is None:
            # a compressed file: decoded first, the way Cubase imports it
            tmp = os.path.join(edits, os.path.splitext(os.path.basename(src))[0] + '.wav')
            dec = Decoder(log)
            if not (dec.available() and dec.convert(
                    [(src, tmp)], float(getattr(project, 'samplerate', 0) or 48000))):
                continue
            src = tmp
            dur = wav_duration(src)
        # MODE 3's LENGTH is not a limit: REAPER plays the whole file
        # backwards whatever it says (measured on Cherry Link's reversed
        # kicks, LENGTH 0.454 on a 1.090 s file, 2026-09-28)
        key = os.path.normcase(os.path.abspath(src))
        dst = done.get(key)
        if dst is None:
            stem = os.path.splitext(os.path.basename(src))[0]
            dst = os.path.join(edits, '%s-Reverse-%s.wav'
                               % (stem, uuid.uuid4().hex.upper()))
            if not reverse_wav(src, dst):
                continue
            done[key] = dst
        it.file = dst
        it.section = None
        n += 1
    if n:
        log.append('%d reversed item(s) arrive as Cubase holds a reversed event: the '
                   'file reversed into Edits (sample for sample, its own format, '
                   'as Audio > Process > Reverse writes it) and an ordinary event '
                   'on it - offset, fades, volume and stretch stay editable, '
                   'nothing is printed' % n)
    return n


def wav_duration(path):
    """Seconds of audio in a WAV, or None for anything else."""
    with open(path, 'rb') as f:
        head = f.read(12)
        if head[:4] not in (b'RIFF', b'RF64') or head[8:12] != b'WAVE':
            return None
        rate = ch = bits = None
        data = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack('<I', hdr[4:])[0]
            if cid == b'fmt ':
                body = f.read(size)
                ch = struct.unpack_from('<H', body, 2)[0]
                rate = struct.unpack_from('<I', body, 4)[0]
                bits = struct.unpack_from('<H', body, 14)[0]
            elif cid == b'data':
                data = size
                break
            else:
                f.seek(size + (size & 1), 1)
        if not rate or data is None:
            return None
        return data / float(max(1, (bits + 7) // 8) * max(1, ch) * rate)


def wav_channels(path):
    """Channel count from a WAV header, or None for anything else."""
    with open(path, 'rb') as f:
        head = f.read(12)
        if head[:4] not in (b'RIFF', b'RF64') or head[8:12] != b'WAVE':
            return None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                return None
            cid, size = hdr[:4], struct.unpack('<I', hdr[4:])[0]
            if cid == b'fmt ':
                body = f.read(size)
                return struct.unpack_from('<H', body, 2)[0]
            f.seek(size + (size & 1), 1)


def prepare(project, out_path, rate, wav_info, log, items=None,
            channels=None, skip_printable=True):
    """Make every audio file the project plays one Cubase can: decode what
    is not WAV, resample what is at another rate, into <project>/Audio,
    and point the items at the results.

    An item that render() is about to print is left on its original file
    (skip_printable): the print is its file for Cubase, and it has to be
    made from what REAPER plays. Made from the resampled copy instead, a
    stretched shaker came out at the right level with a drifting phase and
    never nulled against REAPER's own render of it - the stretch had been
    fed a different signal. render() converts what it could not print.

    Returns (converted, could_not) as lists of source paths."""
    audio_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                             'Audio')
    if items is None:
        items = [i for t in project.tracks for i in t.items]
    by_src = {}
    for it in items:
        if it.kind != 'audio' or not it.file:
            continue
        it.file = resolve(it.file, project.srcdir)
        if skip_printable and needs_render(it):
            continue
        by_src.setdefault(os.path.normcase(it.file), []).append(it)

    jobs = []
    why = {}
    taken = {}
    for key, its in by_src.items():
        src = its[0].file
        if not os.path.isfile(src):
            continue                    # missing here; left as named
        is_wav, frate = wav_rate(src, wav_info)
        # The clip record every event is copied from describes a file with
        # the donor's channel count, and Cubase draws no waveform for a
        # file that has another - a mono voice-over behind a stereo clip
        # came up as an empty event. So a file with the wrong count is
        # rewritten with the right one, like a file at the wrong rate.
        fch = None
        # a take with Melodyne keeps its file exactly: Melodyne's document
        # holds the analysis of that file, and a stereo copy of a mono
        # take rendered its edited notes differently in Cubase (-24 dB
        # null against REAPER; -52 dB with the file kept, 2026-10-01).
        # The clip record carries the file's own channel count
        # (cpr_build.write_audio), which Cubase plays.
        if any(getattr(i, 'ara_id', None) for i in its):
            channels_here = None
        else:
            channels_here = channels
        if is_wav and channels_here:
            try:
                fch = wav_channels(src)
            except Exception:
                fch = None
        if is_wav and (not rate or abs(frate - rate) < 1.0) and (
                not channels or fch is None or fch == channels):
            continue
        if not is_wav and os.path.splitext(src)[1].lower() not in _DECODE_EXT:
            continue
        stem = os.path.splitext(os.path.basename(src))[0]
        # two sources with one name must not land on one file - nor a
        # converted copy on a file the project plays as it is: TB1's
        # noise.mp3 became Audio/noise.wav, where the project's own
        # noise.wav already sat, and five tracks played the MP3 instead
        # (the browser's file order picked the other one)
        n = 1
        cand = stem
        while (cand in taken and taken[cand] != key) or (
                os.path.normcase(os.path.join(audio_dir, cand + '.wav')) in by_src
                and os.path.normcase(os.path.join(audio_dir, cand + '.wav')) != key):
            n += 1
            cand = '%s-%02d' % (stem, n)
        taken[cand] = key
        dst = os.path.join(audio_dir, cand + '.wav')
        why[key] = (('rate' if (not rate or abs(frate - rate) >= 1.0)
                     else 'channels') if is_wav else 'format', src, dst, its)
        fresh = (os.path.isfile(dst)
                 and os.path.getmtime(dst) >= os.path.getmtime(src)
                 and wav_rate(dst, wav_info) == (True, float(rate))
                 and (not channels or wav_channels(dst) == channels))
        if not fresh:
            # an MP3 whose FILE line carries the '1' is read gapless (the
            # LAME delay trimmed), the others with the full decode
            gapless = any('1' in [str(a) for a in (getattr(i, 'file_args', None) or [])]
                          for i in its)
            jobs.append((src, dst, gapless))

    converted, could_not = [], []
    if why:
        dec = Decoder(log)
        made = set()
        if jobs:
            if dec.available():
                made = set(os.path.normcase(p)
                           for p in dec.convert(jobs, rate, channels))
            else:
                log.append('neither ffmpeg nor REAPER was found to convert '
                           'audio with - put ffmpeg.exe beside convert.py '
                           '(or install REAPER) and run again')
        n_fmt = n_rate = n_ch = 0
        for key, (reason, src, dst, its) in why.items():
            queued = any((j[0], j[1]) == (src, dst) for j in jobs)
            ok = (not queued) or (os.path.normcase(dst) in made)
            if ok and os.path.isfile(dst):
                for it in its:
                    it.file = dst
                converted.append(src)
                if reason == 'rate':
                    n_rate += 1
                elif reason == 'channels':
                    n_ch += 1
                else:
                    n_fmt += 1
            else:
                could_not.append(src)
        if converted:
            parts = []
            if n_fmt:
                parts.append('%d not WAV (MP3, OGG...) - Cubase would call '
                             'them missing' % n_fmt)
            if n_rate:
                parts.append('%d WAV at another sample rate - Cubase would '
                             'play them at the wrong speed' % n_rate)
            if n_ch:
                parts.append('%d WAV with another channel count than the '
                             'clip record describes - Cubase would draw no '
                             'waveform' % n_ch)
            log.append('%d audio file(s) were converted to %d Hz WAV in '
                       '%s, as Cubase\'s own importer does (%s), using %s: '
                       '%s'
                       % (len(converted), int(rate),
                          os.path.join(os.path.basename(
                              os.path.dirname(audio_dir)), 'Audio'),
                          '; '.join(parts), dec.name,
                          ', '.join(sorted(os.path.basename(s)
                                           for s in converted))))
        if could_not:
            log.append('%d audio file(s) could not be converted and will '
                       'not play right in Cubase (not WAV, or not at the '
                       'project rate): %s'
                       % (len(could_not),
                          ', '.join(sorted(os.path.basename(s)
                                           for s in could_not))))
    return converted, could_not
