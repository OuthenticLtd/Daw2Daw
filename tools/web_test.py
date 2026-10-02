"""Convert a project folder through the live website in a headless browser
(Edge, installed), the way a user would: choose the folder, pick the
target, convert, download. Prints the status, any warning and the log.

    python tools/web_test.py <project folder> <target: reaper|cubase|live> <out dir> [url] [plugfmt]
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

folder, target, out = sys.argv[1], sys.argv[2], sys.argv[3]
url = sys.argv[4] if len(sys.argv) > 4 else 'https://outhenticltd.github.io/Daw2Daw/'
plugfmt = sys.argv[5] if len(sys.argv) > 5 else 'source'
os.makedirs(out, exist_ok=True)

with sync_playwright() as pw:
    b = pw.chromium.launch(channel='msedge', headless=True)
    ctx = b.new_context(accept_downloads=True)
    page = ctx.new_page()
    page.goto(url + ('&' if '?' in url else '?') + 't=%d' % time.time())
    page.wait_for_function("() => !/Starting/.test(document.querySelector('#status').textContent)",
                           timeout=300000)
    print('ready:', page.inner_text('#status'))
    page.set_input_files('#folderInput', folder)
    page.wait_for_selector('#project option', state='attached', timeout=600000)
    print('projects:', page.eval_on_selector_all('#project option', 'os => os.map(o => o.textContent)'))
    page.select_option('#target', target)
    page.select_option('#plugfmt', plugfmt)
    t0 = time.time()
    with page.expect_download(timeout=3600000) as dl:
        page.click('#go')
    d = dl.value
    path = os.path.join(out, d.suggested_filename)
    d.save_as(path)
    print('downloaded %s (%.1f MB) in %.0f s' % (path, os.path.getsize(path) / 1e6, time.time() - t0))
    print('status:', page.inner_text('#status'))
    for w in page.query_selector_all('#result .warn'):
        print('WARNING:', w.inner_text())
    log = page.eval_on_selector('#log', 'e => e.textContent')
    open(os.path.join(out, 'web-log.txt'), 'w', encoding='utf-8').write(log)
    print('log lines:', len(log.splitlines()))
    b.close()
