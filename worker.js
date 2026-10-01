// The converter, running in the visitor's browser: Python (Pyodide) runs
// the tool's own code unchanged, ffmpeg.wasm runs the ffmpeg commands it
// issues (webshim.py). Nothing leaves the computer.
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.27.8/full/';
const FFCORE = 'https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.10/dist/umd/';

let py = null;        // Pyodide
let ff = null;        // ffmpeg.wasm core
let ffLog = [];

function post(type, data, transfer) {
  self.postMessage(Object.assign({ type }, data || {}), transfer || []);
}
self.post_log = (text) => post('log', { text: String(text) });

importScripts(PYODIDE + 'pyodide.js', FFCORE + 'ffmpeg-core.js');

async function boot() {
  post('status', { text: 'Loading Python…' });
  py = await loadPyodide({ indexURL: PYODIDE });
  post('status', { text: 'Loading ffmpeg…' });
  const coreURL = FFCORE + 'ffmpeg-core.js';
  ff = await self.createFFmpegCore({
    mainScriptUrlOrBlob: coreURL + '#' + btoa(JSON.stringify({
      wasmURL: FFCORE + 'ffmpeg-core.wasm', workerURL: FFCORE + 'ffmpeg-core.worker.js' })),
  });
  ff.setLogger(({ message }) => ffLog.push(message));
  post('status', { text: 'Loading the converter…' });
  const z = await (await fetch('converter.zip?v=8bb98c6641', { cache: 'no-cache' })).arrayBuffer();
  py.FS.writeFile('/tmp/converter.zip', new Uint8Array(z));
  py.runPython(`
import zipfile, sys
zipfile.ZipFile('/tmp/converter.zip').extractall('/tool')
sys.path.insert(0, '/tool'); sys.path.insert(0, '/tool/web')
import webshim; webshim.install()
`);
  post('ready');
}

// ---- ffmpeg, called synchronously from Python --------------------------
function pyIsFile(p) {
  try { const st = py.FS.stat(p); return py.FS.isFile(st.mode); } catch (e) { return false; }
}
function ffMkdirs(dir) {
  let cur = '';
  for (const part of dir.split('/').filter(Boolean)) {
    cur += '/' + part;
    try { ff.FS.mkdir(cur); } catch (e) { /* exists */ }
  }
}
function pyMkdirs(dir) {
  let cur = '';
  for (const part of dir.split('/').filter(Boolean)) {
    cur += '/' + part;
    try { py.FS.mkdir(cur); } catch (e) { /* exists */ }
  }
}
const dirOf = (p) => p.slice(0, p.lastIndexOf('/')) || '/';

self.ffmpeg_run = (argsProxy, probe) => {
  const args = argsProxy.toJs ? argsProxy.toJs() : Array.from(argsProxy);
  const inputs = [], outputs = [];
  for (const a of args) {
    if (typeof a !== 'string' || !a.startsWith('/')) continue;
    if (pyIsFile(a)) {
      ffMkdirs(dirOf(a));
      ff.FS.writeFile(a, py.FS.readFile(a));
      inputs.push(a);
    } else {
      ffMkdirs(dirOf(a));
      outputs.push(a);
    }
  }
  ffLog = [];
  let code = 1;
  try {
    ff.setTimeout(-1);
    // a look at a file goes to ffprobe, which describes it the way the
    // desktop ffmpeg does before it stops (ffmpeg.wasm's ffmpeg does not)
    if (probe) ff.ffprobe(...args); else ff.exec(...args);
    code = ff.ret;
  } catch (e) {
    ffLog.push(String(e));
  }
  try { ff.reset(); } catch (e) { /* ignore */ }
  for (const o of outputs) {
    try {
      const data = ff.FS.readFile(o);
      pyMkdirs(dirOf(o));
      py.FS.writeFile(o, data);
      ff.FS.unlink(o);
    } catch (e) { /* not written */ }
  }
  for (const i of inputs) { try { ff.FS.unlink(i); } catch (e) { /* ignore */ } }
  return { code: code, stderr: ffLog.join('\n') };
};

// ---- a conversion --------------------------------------------------------
const ROOT = '/work';

async function load(msg) {
  // msg.files: [{path: 'Song/Audio/x.wav', data: ArrayBuffer}] or msg.zip: ArrayBuffer
  py.runPython(`import shutil, os; shutil.rmtree('${ROOT}', ignore_errors=True); os.makedirs('${ROOT}')`);
  if (msg.zip) {
    py.FS.writeFile('/tmp/in.zip', new Uint8Array(msg.zip));
    py.runPython(`
import zipfile, os, shutil
with zipfile.ZipFile('/tmp/in.zip') as z:
    # some Windows zip tools (PowerShell's Compress-Archive among them)
    # write folders with backslashes, which a posix system takes for part
    # of the file name: SUPERKUR\\Audio\\x.wav as one file
    for info in z.infolist():
        name = info.filename.replace('\\\\', '/')
        parts = [p for p in name.split('/') if p not in ('', '.', '..')]
        if not parts:
            continue
        dest = os.path.join('${ROOT}', *parts)
        if name.endswith('/'):
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with z.open(info) as src, open(dest, 'wb') as out:
            shutil.copyfileobj(src, out, 1 << 20)
os.remove('/tmp/in.zip')
`);
  } else {
    for (const f of msg.files) {
      const p = ROOT + '/' + f.path;
      pyMkdirs(dirOf(p));
      py.FS.writeFile(p, new Uint8Array(f.data));
    }
  }
  const found = py.runPython(`
import os
out = []
for d, dirs, files in os.walk('${ROOT}'):
    top = os.path.normpath(d) == os.path.normpath('${ROOT}')
    # earlier conversions inside a project folder are not offered again -
    # but a converted folder dropped on its own is the project
    dirs[:] = [x for x in dirs if x.lower() not in ('auto saves', 'backups', '__macosx')
               and (top or not x.endswith((' (Cubase)', ' (REAPER)')))]
    for f in files:
        if f.lower().endswith(('.cpr', '.rpp')):
            out.append(os.path.relpath(os.path.join(d, f), '${ROOT}').replace(os.sep, '/'))
sorted(out)
`).toJs();
  post('projects', { list: found });
}

async function convert(msg) {
  // msg.project: 'Song/Song.cpr', a path inside what load() unpacked
  const root = ROOT;
  const src = root + '/' + msg.project;
  const name = msg.project.split('/').pop();
  const stem = name.replace(/\.[^.]+$/, '');
  const toCubase = /\.rpp$/i.test(name);
  const outDir = dirOf(src) + '/' + stem + (toCubase ? ' (Cubase)' : ' (REAPER)');
  const out = outDir + '/' + stem + (toCubase ? '.cpr' : '.rpp');
  py.globals.set('SRC', src); py.globals.set('OUT', out); py.globals.set('OUTDIR', outDir);
  const res = py.runPython(`
import webshim
rc, rc2 = webshim.convert(SRC, OUT)
z = None
if rc == 0:
    z = webshim.zip_folder(OUTDIR, '/tmp/result.zip')
[rc, rc2, z]
`).toJs();
  const [rc, rc2, z] = res;
  if (rc !== 0 || !z) { post('failed', { code: rc }); return; }
  const data = py.FS.readFile(z);
  const zipName = stem + (toCubase ? ' (Cubase)' : ' (REAPER)') + '.zip';
  // the converted folder goes, the project stays loaded for another try
  py.runPython(`import shutil, os; shutil.rmtree(OUTDIR, ignore_errors=True); os.path.exists('/tmp/result.zip') and os.remove('/tmp/result.zip')`);
  post('done', { zip: data.buffer, name: zipName, checked: rc2 === 0 }, [data.buffer]);
}

self.onmessage = async (e) => {
  try {
    if (e.data.type === 'diag') {   // testing only: run Python, return its value
      post('diag', { text: String(py.runPython(e.data.code)) });
      return;
    }
    if (e.data.type === 'load') await load(e.data);
    if (e.data.type === 'convert') await convert(e.data);
  } catch (err) {
    post('error', { text: String(err && err.message || err) });
  }
};

boot().catch((err) => post('error', { text: 'Could not start: ' + String(err && err.message || err) }));
