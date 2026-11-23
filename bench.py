#!/usr/bin/env python3
"""Small Linux benchmark: Ladybird vs Ardia vs Helium, adapted from Ardia's vs-browsers.md.

Rows implemented:
  1. cold launch -> first contentful paint (fresh profile, local page)
  2. steady state with that page open: RSS/PSS and CPU usage (STEADY_S window)

Timing transparency
-------------------
Two launch numbers are recorded, because the browsers cannot be instrumented
identically:
  * spawn_to_paint_ms - process spawn to the harness seeing a paint signal.
    For Ardia/Helium this includes CDP attach + Page.navigate (~200 ms), so it
    is an end-to-end number, not a like-for-like paint number.
  * nav_to_paint_ms - local server receiving the page request to the harness
    seeing the paint signal. This is the comparable row: it excludes browser
    startup and CDP setup for both families.

Ladybird paints are reported by the page through a beacon. Ardia and Helium are
observed through CDP Page.lifecycleEvent (headless Chromium does not expose
paint entries to page JS in this build); the CDP event is timestamped when the
harness receives it, so only local socket latency separates the two methods.

Browsers are interleaved/rotated; no cache purge and no other apps are touched.
Everything the run depended on is written into results.json under "meta".
"""
import json, os, shutil, socket, statistics, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from websockets.sync.client import connect

import bench_common as common

HERE = os.path.dirname(os.path.abspath(__file__))

PORT = 18082
ROUNDS = int(os.environ.get('ROUNDS', '6'))
GAP = float(os.environ.get('GAP', '2'))
STEADY_S = float(os.environ.get('STEADY_S', '8'))
# GPU setting is shared by every harness. Default keeps the historical
# --disable-gpu behaviour; GPU=1 enables the GPU for both Chromium browsers.
GPU = os.environ.get('GPU', '0') == '1'
PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>bench</title></head>
<body style="margin:0;background:#e11d48;color:white"><h1>Paint benchmark</h1>
<p>Interleaved cold-launch paint benchmark.</p>
<script>
var q = location.search.slice(1);
function beacon(kind, t) { new Image().src = '/beacon?' + q + '&kind=' + kind + '&t=' + t; }
new PerformanceObserver(function (l) { for (var e of l.getEntries())
  if (e.name === 'first-contentful-paint') beacon('fcp', e.startTime);
}).observe({type:'paint', buffered:true});
function dump() { for (var e of performance.getEntriesByType('paint')) beacon('paint-' + e.name, e.startTime); }
window.addEventListener('load', function () { beacon('load', performance.now()); dump(); setTimeout(dump, 250); });
beacon('script', performance.now());
</script></body></html>"""

events = {}
requests = {}
lock = threading.Lock()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == '/beacon':
            q = urllib.parse.parse_qs(u.query)
            with lock:
                events.setdefault(q.get('run', ['?'])[0], []).append(
                    (q.get('kind', ['?'])[0], float(q.get('t', ['0'])[0]), time.monotonic()))
            self.send_response(204); self.end_headers(); return
        if u.path == '/test':
            with lock:
                requests.setdefault(urllib.parse.parse_qs(u.query).get('run', ['?'])[0],
                                    time.monotonic())
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers(); self.wfile.write(PAGE)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p

def launch(browser, url):
    """Return (process, (port, profile) or None, exact argv)."""
    if browser == 'ladybird':
        cmd = ['ladybird', '--headless=manual', '--temporary-profile',
               '--window-width=800', '--window-height=600', url]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), None, cmd
    if browser in ('helium', 'ardia'):
        port = free_port()
        profile = tempfile.mkdtemp(prefix='lb-bench-he-')
        binary = common.binary_path(browser)
        cmd = [binary, '--headless=new', '--no-first-run',
               '--user-data-dir=' + profile, '--remote-debugging-port=%d' % port,
               '--window-size=800,600', 'about:blank']
        if not GPU:
            cmd.insert(2, '--disable-gpu')
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), (port, profile), cmd
    raise SystemExit('unknown browser ' + browser)

def run_cold(browser, token, sample_s=0):
    url = 'http://127.0.0.1:%d/test?run=%s' % (PORT, token)
    result = {'spawn_to_paint_ms': None, 'nav_to_paint_ms': None,
              'page_fcp_ms': None, 'paint_reference': None,
              'request_wait_ms': None, 'steady': None, 'launch_error': None,
              'cmd': None, 'profile': None}
    t0 = time.monotonic()
    proc, extra, cmd = launch(browser, url)
    result['cmd'] = cmd
    if extra:
        result['profile'] = extra[1]
    ws = None
    marker = extra[1] if browser in ('helium', 'ardia') and extra else token
    def paint_at():
        with lock:
            req = requests.get(token)
        result['nav_to_paint_ms'] = round((paint_wall - req) * 1000, 1) if req else None
        result['request_wait_ms'] = round((req - t0) * 1000, 1) if req else None
    try:
        if extra:
            # Fail loudly instead of silently measuring a pre-existing instance:
            # the launcher must have created a browser with our fresh profile.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not common.tree([marker]):
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            if not common.tree([marker]):
                result['launch_error'] = 'no process using the fresh profile appeared; refusing to measure another instance'
                return result
        if browser == 'ladybird':
            result['paint_reference'] = 'page beacon (PerformanceObserver FCP)'
            deadline = t0 + 12
            while time.monotonic() < deadline:
                with lock: ev = list(events.get(token, []))
                fcp = next((e for e in ev if e[0] == 'fcp'), None)
                if fcp:
                    paint_wall = fcp[2]
                    result['spawn_to_paint_ms'] = round((paint_wall - t0) * 1000, 1)
                    result['page_fcp_ms'] = round(fcp[1], 1)
                    paint_at()
                    break
                time.sleep(0.01)
        else:
            result['paint_reference'] = 'CDP lifecycle firstContentfulPaint (arrival time)'
            port, profile = extra
            target = None
            while time.monotonic() - t0 < 8 and not target:
                try:
                    tabs = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json/list' % port, timeout=1))
                    target = next((t for t in tabs if t['type'] == 'page'), None)
                except Exception: time.sleep(0.05)
            if target:
                ws = connect(target['webSocketDebuggerUrl'], open_timeout=5, legacy=True)
                n = 0
                def call(method, params=None):
                    nonlocal n
                    n += 1; ws.send(json.dumps({'id': n, 'method': method, 'params': params or {}}))
                call('Page.enable'); call('Page.setLifecycleEventsEnabled', {'enabled': True})
                commit = None
                call('Page.navigate', {'url': url})
                deadline = t0 + 12
                while time.monotonic() < deadline:
                    try: msg = json.loads(ws.recv(timeout=0.2))
                    except TimeoutError: continue
                    if msg.get('method') == 'Page.lifecycleEvent':
                        p = msg.get('params', {})
                        if p.get('name') == 'commit': commit = p.get('timestamp')
                        if p.get('name') == 'firstContentfulPaint':
                            paint_wall = time.monotonic()
                            result['spawn_to_paint_ms'] = round((paint_wall - t0) * 1000, 1)
                            if commit: result['page_fcp_ms'] = round((p['timestamp'] - commit) * 1000, 1)
                            paint_at()
                            break
        if sample_s and result['spawn_to_paint_ms'] is not None:
            pids = common.tree([marker])
            result['steady'] = common.sample([marker], sample_s) if pids else None
    finally:
        if ws: ws.close()
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()
        if extra:
            shutil.rmtree(extra[1], ignore_errors=True)
    return result

def summarize(label, vals, unit):
    v = [x for x in vals if x is not None]
    if not v: return '%s: no data' % label
    return '%-40s median %8.1f %-4s  min %8.1f  max %8.1f  (n=%d)' % (
        label, statistics.median(v), unit, min(v), max(v), len(v))

OUT = os.environ.get('OUT', os.path.join(HERE, 'results.json'))
meta = common.make_meta('bench.py', rounds=ROUNDS, gap_s=GAP, steady_s=STEADY_S,
                        gpu=GPU, page='local /test', timing='spawn_to_paint; nav_to_paint')
results = {b: {'spawn_to_paint_ms': [], 'nav_to_paint_ms': [], 'page_fcp_ms': [],
               'request_wait_ms': [], 'rows': []}
           for b in common.BROWSERS}
existing = OUT
if os.environ.get('STEADY_ONLY') and os.path.exists(existing):
    old = json.load(open(existing))
    results = old.get('cold', results)
    meta = old.get('meta', meta)

for rnd in range(0 if os.environ.get('STEADY_ONLY') else ROUNDS):
    for browser in common.rotated(common.BROWSERS, rnd):
        token = '%s-%d' % (time.time_ns(), rnd)
        r = run_cold(browser, token)
        results[browser]['spawn_to_paint_ms'].append(r['spawn_to_paint_ms'])
        results[browser]['nav_to_paint_ms'].append(r['nav_to_paint_ms'])
        results[browser]['page_fcp_ms'].append(r['page_fcp_ms'])
        results[browser]['request_wait_ms'].append(r['request_wait_ms'])
        results[browser]['rows'].append(r)
        print('round %d %-9s spawn=%-8s nav=%-8s page_fcp=%s' %
              (rnd + 1, browser, r['spawn_to_paint_ms'], r['nav_to_paint_ms'],
               r['page_fcp_ms']), flush=True)
        time.sleep(GAP)

steady = {}
if not os.environ.get('NO_STEADY'):
    for i, browser in enumerate(common.rotated(common.BROWSERS, ROUNDS)):
        r = run_cold(browser, 'steady-' + browser, sample_s=STEADY_S)
        steady[browser] = r['steady']
        print('steady %-9s %s' % (browser, steady[browser]), flush=True)

print('\n=== Results (Linux x86_64, headless, local page) ===')
for b in common.BROWSERS:
    print(b.upper())
    print(' ', summarize('spawn -> paint (incl. harness setup)', results[b]['spawn_to_paint_ms'], 'ms'))
    print(' ', summarize('server request -> paint (comparable)', results[b]['nav_to_paint_ms'], 'ms'))
    print(' ', summarize('page-reported FCP (differing origin)', results[b]['page_fcp_ms'], 'ms'))
    if steady.get(b):
        print('  steady: RSS %.1f MB  PSS %.1f MB  CPU %.3f%%  excluded CPU %.3f%%' %
              (steady[b]['rss_mb'], steady[b]['pss_mb'], steady[b]['cpu_pct'],
               steady[b]['cpu_excluded_pct']))
common.write_json(OUT,
                  {'meta': meta, 'cold': results, 'steady': steady})
srv.shutdown()
