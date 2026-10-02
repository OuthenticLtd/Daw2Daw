"""Rebuild converter.zip from src/ and refresh the cache-busting version.

    python build.py

src/ is the converter's source (convert.py, cubaserea/, templates/, web/);
the worker unzips converter.zip into /tool inside Pyodide. The version
string in index.html and worker.js changes with the zip's contents, so
visitors' browsers fetch the new build instead of a cached one.
"""
import hashlib
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'src')
ZIP = os.path.join(ROOT, 'converter.zip')
SKIP_DIRS = {'__pycache__'}


def files():
    out = []
    for d, dirs, names in os.walk(SRC):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for n in sorted(names):
            if not n.endswith('.pyc'):
                out.append(os.path.join(d, n))
    return out


def build():
    paths = files()
    h = hashlib.sha1()
    # fixed timestamps: the same source always gives the same zip
    with zipfile.ZipFile(ZIP, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in paths:
            arc = os.path.relpath(p, SRC).replace(os.sep, '/')
            with open(p, 'rb') as f:
                data = f.read()
            h.update(arc.encode() + b'\0' + data)
            info = zipfile.ZipInfo(arc, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    ver = h.hexdigest()[:10]
    # the worker's own version also follows its own text, so a change to
    # worker.js alone still reaches browsers that cached the old one
    with open(os.path.join(ROOT, 'worker.js'), encoding='utf-8', newline='') as f:
        wtext = re.sub(r"(converter\.zip\?v=)[0-9a-f]+", r'\g<1>' + ver, f.read())
    wver = hashlib.sha1((ver + wtext).encode()).hexdigest()[:10]
    for name, pat, v in (('index.html', r"(worker\.js\?v=)[0-9a-f]+", wver),
                         ('worker.js', r"(converter\.zip\?v=)[0-9a-f]+", ver)):
        path = os.path.join(ROOT, name)
        with open(path, encoding='utf-8', newline='') as f:
            text = f.read()
        new, n = re.subn(pat, r'\g<1>' + v, text)
        if n != 1:
            raise SystemExit(f'{name}: expected one version string, found {n}')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(new)
    print(f'converter.zip: {len(paths)} files, {os.path.getsize(ZIP)} bytes, v={ver}')


if __name__ == '__main__':
    build()
