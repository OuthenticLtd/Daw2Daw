"""Running the converter in a browser (Pyodide).

The converter starts one outside program: ffmpeg (REAPER and Windows' own
decoder are optional and fall back to ffmpeg). In the browser there are no
programs to start, so subprocess.run is replaced: a call to ffmpeg goes to
ffmpeg.wasm in the same Web Worker (worker.js, ffmpeg_run - synchronous),
with the files it reads copied over and the ones it writes copied back;
anything else reports "not found", which the converter already handles.
"""
import io
import os
import subprocess
import sys

import js                   # the Web Worker's globals (Pyodide)
from pyodide.ffi import to_js

TOOL = '/tool'


def _run(cmd, *args, **kw):
    argv = [str(a) for a in (cmd if isinstance(cmd, (list, tuple)) else [cmd])]
    text = bool(kw.get('text') or kw.get('universal_newlines') or kw.get('encoding'))
    prog = os.path.basename(argv[0]).lower()
    if prog in ('ffmpeg', 'ffmpeg.exe'):
        ffargs = argv[1:]
        # ffmpeg.wasm keeps the log level of the last command (one in the
        # same process): after a '-v error' every later description of a
        # file came back empty. Each command states its own.
        if not any(a in ('-v', '-loglevel') for a in ffargs):
            ffargs = ['-loglevel', 'info'] + ffargs
        if len(ffargs) >= 2 and ffargs[-2] == '-i':
            # a look at a file (`ffmpeg -i file`, read off stderr): the
            # desktop ffmpeg describes the input before it complains that
            # there is no output; ffmpeg.wasm (5.1) complains first. An
            # output that takes nothing makes it open and describe the
            # file without decoding it.
            ffargs = ffargs + ['-t', '0', '-f', 'null', '-']
        res = js.ffmpeg_run(to_js(ffargs), False)
        code = int(res.code)
        err = str(res.stderr).encode('utf-8', 'replace')
    else:
        code, err = 127, ('%s is not available in the browser' % prog).encode()
    out = b''
    if text:
        enc = kw.get('encoding') or 'utf-8'
        out, err = out.decode(enc, 'replace'), err.decode(enc, 'replace')
    # what the caller asked for: stderr into stdout, or each on its own
    if kw.get('stderr') == subprocess.STDOUT:
        out, err = out + err, None
    if kw.get('check') and code:
        raise subprocess.CalledProcessError(code, argv, out, err)
    return subprocess.CompletedProcess(argv, code, out, err)


def install():
    """Patch subprocess and make ffmpeg findable (media.find_ffmpeg looks
    for <tool>/ffmpeg/ffmpeg)."""
    subprocess.run = _run
    os.makedirs(os.path.join(TOOL, 'ffmpeg'), exist_ok=True)
    stub = os.path.join(TOOL, 'ffmpeg', 'ffmpeg')
    if not os.path.exists(stub):
        open(stub, 'w').write('ffmpeg.wasm in the worker\n')
        os.chmod(stub, 0o755)
    os.environ['CPR_NO_REAPER'] = '1'
    # folders under the browser's /work are written into a Cubase project
    # the way a Windows Cubase writes them (cpr_build.host_dir)
    os.environ['CPR_WEB_ROOT'] = '/work'
    os.environ['CPR_PROGRESS_TTY'] = '1'
    # wide enough that the bar's "~1m left" is not cut off: the page reads it
    os.environ['CPR_COLUMNS'] = '220'
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    if TOOL not in sys.path:
        sys.path.insert(0, TOOL)


class _Lines(io.TextIOBase):
    """stdout/stderr to the page as they are written (progress bars
    redraw with a carriage return; the page does the same)."""

    def writable(self):
        return True

    def write(self, s):
        if s:
            js.post_log(s)
        return len(s)

    def isatty(self):
        return False


def _script(path, argv):
    """Run one of the tool's scripts as `python <path> <argv>` would;
    returns its exit code."""
    import runpy
    # each script starts as a fresh program, as it does on the desktop
    # (CONVERT.bat runs convert.py and verify.py one after the other): the
    # converter keeps per-run caches - the index of a folder's files, among
    # others - and verify.py reusing convert.py's took a file the conversion
    # made after the index was built for missing
    for name in [m for m in sys.modules if m == 'cubaserea' or m.startswith('cubaserea.')]:
        del sys.modules[name]
    old = sys.argv
    sys.argv = [path] + list(argv)
    try:
        runpy.run_path(path, run_name='__main__')
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = old


def convert(src, out, plugin_format='source'):
    """Convert `src` (a project file in the browser's file system) to `out`,
    collecting every file it uses beside it, then check the result against
    the original. Returns (exit code of the conversion, of the check)."""
    so, se = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = _Lines()
    try:
        # verify.py compares REAPER and Cubase projects with each other;
        # a Live Set on either side is not among them
        checked = not (out.lower().endswith('.als') or src.lower().endswith('.als'))
        print('step 1 of %d: converting\n' % (2 if checked else 1))
        env = {}
        if src.lower().endswith('.rpp'):
            # the clean build CONVERT.bat makes (drop.py)
            env = {'CPR_ON_CONSUME': '1', 'CPR_ON_DELPROTO': '1'}
        os.environ.update(env)
        rc = _script(os.path.join(TOOL, 'convert.py'),
                     [src, out, '--collect', '--plugin-format', plugin_format or 'source'])
        for k in env:
            os.environ.pop(k, None)
        rc2 = None
        if rc == 0 and checked:
            print('\nstep 2 of 2: checking the result against the original\n')
            rc2 = _script(os.path.join(TOOL, 'verify.py'), [src, out])
        return rc, rc2
    finally:
        sys.stdout, sys.stderr = so, se


def zip_folder(folder, zip_path):
    """The converted folder as one zip, its own name at the top."""
    import zipfile
    base = os.path.dirname(folder.rstrip('/'))
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                full = os.path.join(root, f)
                # audio barely compresses; storing it is much faster
                kind = (zipfile.ZIP_STORED if f.lower().endswith(
                    ('.wav', '.mp3', '.mp4', '.m4a', '.ogg', '.flac', '.aif', '.aiff', '.mov'))
                    else zipfile.ZIP_DEFLATED)
                z.write(full, os.path.relpath(full, base), compress_type=kind)
    return zip_path
