#!/usr/bin/env python3
"""New tab -> first paint via CDP Target.createTarget (method-specific).

This is NOT the same method as the popup row in bench_tabs.py: each tab is
created over CDP, focused, then navigated, and the clock starts when
Page.navigate is sent. It exists because some headless Chromium builds do not
deliver paint beacons from popup tabs. Report it as a separate method, never in
the same row as the popup numbers.

One warm-up tab is opened and discarded so the first measured tab is not a
cold-start outlier. Every attempt is kept (failures are recorded as null) and
the success/attempt counts are written to the output file.
"""
import json, os, shutil, socket, statistics, subprocess, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from websockets.sync.client import connect

import bench_common as common

PORT = 18086
N = int(os.environ.get('N', '10'))
WARMUP = int(os.environ.get('WARMUP', '1'))
BROWSER = os.environ.get('BROWSER', 'helium-browser')
GPU = os.environ.get('GPU', '0') == '1'
OUT = os.environ.get('OUT')
PAGE = b'<!doctype html><html><body style="background:#e11d48"><h1>New tab</h1></body></html>'

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers(); self.wfile.write(PAGE)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

s = socket.socket(); s.bind(('127.0.0.1', 0)); CDP = s.getsockname()[1]; s.close()
prof = tempfile.mkdtemp(prefix='lb-bench-newtab-')
cmd = [BROWSER, '--headless=new', '--no-first-run', '--disable-extensions']
if not GPU:
    cmd.append('--disable-gpu')
cmd += ['--user-data-dir=' + prof, '--remote-debugging-port=%d' % CDP, 'about:blank']
p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
values, attempts = [], [0]
meta = common.make_meta('newtab_cdp.py', method='cdp-target', n=N, warmup=WARMUP,
                        gpu=GPU, browser=BROWSER, cmd=cmd,
                        clock='Page.navigate send -> FCP event arrival')
try:
    browser = None
    for _ in range(100):
        try:
            browser = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json/version' % CDP, timeout=1))['webSocketDebuggerUrl']
            break
        except Exception: time.sleep(0.1)

    def time_one(bws, counter, i, discard=False):
        attempts[0] += 1
        counter[0] += 1
        ident = counter[0]
        bws.send(json.dumps({'id': ident, 'method': 'Target.createTarget', 'params': {'url': 'about:blank'}}))
        target_id = None
        while target_id is None:
            try: msg = json.loads(bws.recv(timeout=1))
            except TimeoutError: continue
            if msg.get('id') == ident: target_id = msg.get('result', {}).get('targetId')
        bws.send(json.dumps({'id': counter[0] + 1, 'method': 'Target.activateTarget',
                             'params': {'targetId': target_id}}))
        counter[0] += 1
        time.sleep(0.1)
        tabs = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json/list' % CDP, timeout=2))
        tgt = next(t for t in tabs if t['id'] == target_id)
        with connect(tgt['webSocketDebuggerUrl'], open_timeout=5, legacy=True) as ws:
            ws.send(json.dumps({'id': 1, 'method': 'Page.enable'}))
            ws.send(json.dumps({'id': 2, 'method': 'Page.setLifecycleEventsEnabled', 'params': {'enabled': True}}))
            ws.send(json.dumps({'id': 3, 'method': 'Page.navigate',
                                'params': {'url': 'http://127.0.0.1:%d/t?run=%d' % (PORT, i)}}))
            t0 = time.monotonic()
            while time.monotonic() - t0 < 12:
                try: msg = json.loads(ws.recv(timeout=0.2))
                except TimeoutError: continue
                if msg.get('method') == 'Page.lifecycleEvent' and msg.get('params', {}).get('name') == 'firstContentfulPaint':
                    return None if discard else round((time.monotonic() - t0) * 1000, 1)
            return None

    with connect(browser, open_timeout=5, legacy=True) as bws:
        counter = [0]
        for w in range(WARMUP):
            print('warm-up', w + 1, '=', time_one(bws, counter, -1 - w, discard=True), 'ms', flush=True)
        for i in range(N):
            v = time_one(bws, counter, i)
            values.append(v)
            print('sample %d = %s ms' % (i + 1, v), flush=True)
finally:
    p.terminate()
    try: p.wait(timeout=3)
    except subprocess.TimeoutExpired: p.kill()
    shutil.rmtree(prof, ignore_errors=True)
    srv.shutdown()

valid = [v for v in values if v is not None]
payload = {'meta': meta, 'method': 'cdp-target', 'values': values,
           'attempts': attempts[0] - WARMUP, 'ok': len(valid)}
if OUT:
    common.write_json(OUT, payload)
print((os.environ.get('LABEL', 'browser')) + ' new tab FCP (CDP target):',
      [round(x, 1) if x is not None else None for x in values])
if valid:
    print('median', round(statistics.median(valid), 1), 'ms min', min(valid),
          'max', max(valid), 'n', len(valid), 'of', attempts[0] - WARMUP, 'attempts')
else:
    print('no paint events; 0 of', attempts[0] - WARMUP, 'attempts succeeded')
