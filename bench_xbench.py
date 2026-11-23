#!/usr/bin/env python3
"""Ardia-shaped session rows for the CDP-capable browsers (Ardia, Helium).

This does not reproduce Ardia's xbench. It implements the subset of its rows
that can be observed from Linux with a local HTTP beacon and CDP:

  * new tab -> first paint       Target.createTarget -> navigate -> FCP event
  * tab switch -> shown          Target.activateTarget -> visibilitychange ->
                                 two rAFs -> beacon arrival at the harness
  * new window -> first paint    Target.createTarget({newWindow:true}); recorded
                                 unless CDP reports the same window id
  * memory at 1 / 10 / 20 tabs, and after closing back to 1, whole tree PSS/RSS

A warmup tab is created and closed before measurement so the first measured
new tab does not absorb one-time renderer startup.

The Ardia Linux headless build does not paint additional tabs in the same
window and reports no visibility change for them, so its new-tab and switch
cells stay empty. Its new-window row works because a new window is visible.

The launch rows (link, restored tab, window on screen), idle wakeups and the
download/on-disk rows are NOT measurable in this headless Linux setup. They are
left out of the JSON and rendered as unavailable by report.py, never guessed.

Usage:
    python3 bench_xbench.py
    OUT=xbench_results.json ROUNDS=2 IDLE_S=30 python3 bench_xbench.py
"""
import json, os, shutil, socket, subprocess, tempfile, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.sync.client import connect

import bench_common as common

PORT = int(os.environ.get('PAGE_PORT', '18087'))
ROUNDS = int(os.environ.get('ROUNDS', '2'))
N_TABS = int(os.environ.get('N_TABS', '20'))
SWITCHES = int(os.environ.get('SWITCHES', '16'))
NEW_WINDOWS = int(os.environ.get('NEW_WINDOWS', '3'))
SETTLE_S = float(os.environ.get('SETTLE_S', '10'))
IDLE_S = float(os.environ.get('IDLE_S', '30'))
NEW_TAB_N = int(os.environ.get('NEW_TAB_N', '5'))
WARMUP = int(os.environ.get('WARMUP', '1'))
SAMPLE_S = float(os.environ.get('SAMPLE_S', '6'))
TIMEOUT_S = float(os.environ.get('TIMEOUT_S', '15'))
GPU = os.environ.get('GPU', '0') == '1'
BROWSERS = tuple(x for x in os.environ.get('BROWSERS', 'ardia,helium').split(',') if x)
OUT = os.environ.get('OUT', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'xbench_results.json'))

PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>xbench row</title></head>
<body style="background:#e11d48;color:#fff"><h1>Bench page</h1>
<p>Session page for the Ardia-shaped rows.</p>
<script>
var q = new URLSearchParams(location.search), run = q.get('run') || '?';
function beacon(kind) { new Image().src = '/beacon?run=' + encodeURIComponent(run) + '&kind=' + kind; }
window.addEventListener('load', function () { beacon('load'); });
document.addEventListener('visibilitychange', function () {
  if (document.visibilityState !== 'visible') return;
  requestAnimationFrame(function () { requestAnimationFrame(function () { beacon('shown'); }); });
});
</script></body></html>"""

events = {}
lock = threading.Lock()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == '/beacon':
            with lock:
                events.setdefault(q.get('run', ['?'])[0], []).append(
                    (q.get('kind', ['?'])[0], time.monotonic()))
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


def wait_until(test, seconds=TIMEOUT_S):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = test()
        if value:
            return value
        time.sleep(0.02)
    return None


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Browser:
    """Small browser-level CDP client; events are ignored, responses matched by id."""

    def __init__(self, port):
        self.port = port
        version = wait_until(lambda: self._version(port))
        if not version or not version.get('webSocketDebuggerUrl'):
            raise RuntimeError('no CDP browser endpoint on port %d' % port)
        self.ws = connect(version['webSocketDebuggerUrl'], open_timeout=5, legacy=True)
        self.seq = 0

    @staticmethod
    def _version(port):
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/json/version' % port, timeout=1) as r:
                return json.load(r)
        except Exception:
            return None

    def call(self, method, params=None, timeout=10):
        self.seq += 1
        ident = self.seq
        self.ws.send(json.dumps({'id': ident, 'method': method, 'params': params or {}}))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                msg = json.loads(self.ws.recv(timeout=0.2))
            except TimeoutError:
                continue
            if msg.get('id') == ident:
                if 'error' in msg:
                    raise RuntimeError('%s: %s' % (method, msg['error'].get('message')))
                return msg.get('result', {})
        raise RuntimeError('%s timed out' % method)

    def targets(self):
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/json/list' % self.port, timeout=2) as r:
                return [t for t in json.load(r) if t.get('type') == 'page']
        except Exception:
            return []

    def close(self):
        self.ws.close()


class Session(Browser):
    def url(self, run):
        return 'http://127.0.0.1:%d/test?run=%s' % (PORT, run)

    def create(self, new_window=False):
        params = {'url': 'about:blank'}
        if new_window:
            params['newWindow'] = True
        return self.call('Target.createTarget', params).get('targetId')

    def navigate(self, target_id, run, t0=None):
        target = next((t for t in self.targets() if t['id'] == target_id), None)
        if not target:
            raise RuntimeError('target %s disappeared' % target_id)
        with connect(target['webSocketDebuggerUrl'], open_timeout=5, legacy=True) as ws:
            ws.send(json.dumps({'id': 1, 'method': 'Page.enable'}))
            ws.send(json.dumps({'id': 2, 'method': 'Page.setLifecycleEventsEnabled',
                                'params': {'enabled': True}}))
            t0 = t0 or time.monotonic()
            ws.send(json.dumps({'id': 3, 'method': 'Page.navigate', 'params': {'url': self.url(run)}}))
            end = t0 + TIMEOUT_S
            while time.monotonic() < end:
                try:
                    msg = json.loads(ws.recv(timeout=0.2))
                except TimeoutError:
                    continue
                if (msg.get('method') == 'Page.lifecycleEvent' and
                        msg.get('params', {}).get('name') == 'firstContentfulPaint'):
                    return round((time.monotonic() - t0) * 1000, 1)
            return None

    def wait_beacon(self, run, kind, since):
        return wait_until(lambda: next((t for k, t in reversed(events.get(run, []))
                                        if k == kind and t > since), None))


def launch(browser, profile, cdp_port):
    cmd = [common.binary_path(browser), '--headless=new', '--no-first-run',
           '--user-data-dir=' + profile, '--remote-debugging-port=%d' % cdp_port,
           '--window-size=1280,800', 'about:blank']
    if not GPU:
        cmd.insert(2, '--disable-gpu')
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if not common.tree([profile]):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
            continue
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/json/version' % cdp_port, timeout=1):
                return proc, cmd
        except Exception:
            time.sleep(0.05)
    raise RuntimeError('browser did not start with the fresh profile')


def sample(profile):
    if not common.tree([profile]):
        return None
    return common.sample([profile], SAMPLE_S)


def run_session(browser, token):
    profile = tempfile.mkdtemp(prefix='lb-bench-xbench-')
    cdp_port = free_port()
    row = {'error': None, 'newtab_ms': [], 'switch_ms': [], 'newwindow_ms': [],
           'memory_1': None, 'memory_10': None, 'memory_20': None, 'memory_back_1': None, 'cmd': None}
    proc = None
    s = None
    tabs = []
    try:
        proc, row['cmd'] = launch(browser, profile, cdp_port)
        s = Session(cdp_port)
        root = s.targets()[0]['id']
        s.navigate(root, token + '-root')
        if not s.wait_beacon(token + '-root', 'load', 0):
            raise RuntimeError('root page did not load')

        for i in range(WARMUP):
            run = '%s-warmup-%d' % (token, i)
            tid = s.create()
            s.call('Target.activateTarget', {'targetId': tid})
            s.navigate(tid, run)
            s.wait_beacon(run, 'load', 0)
            s.call('Target.closeTarget', {'targetId': tid})

        time.sleep(SETTLE_S)
        row['memory_1'] = sample(profile)
        for i in range(N_TABS):
            run = '%s-%d' % (token, i)
            t0 = time.monotonic()
            tid = s.create()
            tabs.append((tid, run))
            # Create, activate and navigate one tab at a time. A background
            # target does not paint, so neither batching all targets first nor
            # relying on implicit activation leaves FCP measurable.
            s.call('Target.activateTarget', {'targetId': tid})
            fcp = s.navigate(tid, run, t0)
            if i < NEW_TAB_N:
                row['newtab_ms'].append(fcp)
            if not s.wait_beacon(run, 'load', 0):
                raise RuntimeError('tab %s did not load' % run)
            if i + 1 == 10:
                row['memory_10'] = sample(profile)
            if i + 1 == 20:
                time.sleep(SETTLE_S)
                row['memory_20'] = sample(profile)

        time.sleep(SETTLE_S)

        current = len(tabs) - 1
        for i in range(SWITCHES):
            nxt = i % min(2, len(tabs))
            if nxt == current:
                nxt = (nxt + 1) % len(tabs)
            tid, run = tabs[nxt]
            before = len([e for e in events.get(run, []) if e[0] == 'shown'])
            t0 = time.monotonic()
            s.call('Target.activateTarget', {'targetId': tid})
            shown = s.wait_beacon(run, 'shown', t0 - 0.001)
            if shown and len([e for e in events.get(run, []) if e[0] == 'shown']) > before:
                row['switch_ms'].append(round((shown - t0) * 1000, 1))
            else:
                row['switch_ms'].append(None)
            current = nxt

        ref_window = None
        try:
            ref_window = s.call('Browser.getWindowForTarget', {'targetId': root}).get('windowId')
        except RuntimeError:
            pass
        for i in range(NEW_WINDOWS):
            run = '%s-window-%d' % (token, i)
            try:
                tid = s.create(new_window=True)
                s.call('Target.activateTarget', {'targetId': tid})
                try:
                    wid = s.call('Browser.getWindowForTarget', {'targetId': tid}).get('windowId')
                except RuntimeError:
                    wid = None
                fcp = s.navigate(tid, run)
                # Only reject the sample when both ids are known and equal:
                # some builds do not expose window ids at all.
                if wid is not None and wid == ref_window:
                    fcp = None
                row['newwindow_ms'].append(fcp)
                s.call('Target.closeTarget', {'targetId': tid})
            except RuntimeError:
                row['newwindow_ms'].append(None)

        for tid, _ in tabs:
            try:
                s.call('Target.closeTarget', {'targetId': tid})
            except RuntimeError:
                pass
        time.sleep(IDLE_S)
        if N_TABS == 20:
            row['memory_back_1'] = sample(profile)
    except Exception as exc:
        row['error'] = '%s: %s' % (type(exc).__name__, exc)
    finally:
        if s:
            s.close()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return row


srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

meta = common.make_meta('bench_xbench.py', rounds=ROUNDS, n_tabs=N_TABS, new_tab_n=NEW_TAB_N, warmup=WARMUP, switches=SWITCHES,
                        new_windows=NEW_WINDOWS, settle_s=SETTLE_S, idle_s=IDLE_S,
                        sample_s=SAMPLE_S, gpu=GPU, browsers=list(BROWSERS),
                        memory_rule='whole process tree PSS/RSS',
                        method='local beacon + CDP browser/target commands')
results = {b: {'rows': [], 'newtab_ms': [], 'switch_ms': [], 'newwindow_ms': [],
               'memory_1': [], 'memory_10': [], 'memory_20': [], 'memory_back_1': []} for b in BROWSERS}

for rnd in range(ROUNDS):
    for browser in common.rotated(BROWSERS, rnd):
        token = '%s-%d' % (time.time_ns(), rnd)
        row = run_session(browser, token)
        results[browser]['rows'].append(row)
        if row['error'] is None:
            for key in ('newtab_ms', 'switch_ms', 'newwindow_ms'):
                results[browser][key].extend(row[key])
            for key in ('memory_1', 'memory_10', 'memory_20', 'memory_back_1'):
                if row[key]:
                    results[browser][key].append(row[key])
        print('round %d %-8s error=%s switch_ok=%d/%d window_ok=%d/%d' %
              (rnd + 1, browser, row['error'],
               sum(v is not None for v in row['switch_ms']), len(row['switch_ms']),
               sum(v is not None for v in row['newwindow_ms']), len(row['newwindow_ms'])), flush=True)

common.write_json(OUT, {'meta': meta, **results})
print('wrote ' + OUT)
srv.shutdown()
