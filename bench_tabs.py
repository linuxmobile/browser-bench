#!/usr/bin/env python3
"""Memory and idle CPU at 1/10/20 pages, plus popup new-tab timing (headless).

Fairness rules, applied identically to every browser:
  * memory = PSS/RSS of the whole browser process tree, nothing excluded;
  * CPU = delta of that tree minus GPU and extension processes, and the
    excluded CPU is recorded next to the included value;
  * a run that did not open every requested tab is marked complete=false and
    is left out of medians by report.py instead of being silently averaged in;
  * browser order rotates by round and tab count.

The popup new-tab row uses the browser's own window.open() path for all
browsers, so Ladybird and Chromium are measured the same way. Two timestamps
are kept per tab: the page load beacon (always available, the comparable row)
and the first-contentful-paint beacon (may be missing in some headless builds).
Sample counts are written alongside the values so partial coverage is visible.
"""
import os, shutil, statistics, subprocess, tempfile, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bench_common as common

PORT = 18085
N_TABS = [int(x) for x in os.environ.get('TABS', '1,10,20').split(',') if x]
ROUNDS = int(os.environ.get('ROUNDS', '3'))
SAMPLE_S = float(os.environ.get('SAMPLE_S', '6'))
GAP = float(os.environ.get('GAP', '2'))
BROWSERS = tuple(x for x in os.environ.get('BROWSERS', 'ladybird,ardia,helium').split(',') if x)
OUT = os.environ.get('OUT', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tabs_results.json'))
GPU = os.environ.get('GPU', '0') == '1'
LOAD_TIMEOUT = float(os.environ.get('LOAD_TIMEOUT', '18'))
events = {}
lock = threading.Lock()

def page(n):
    return ("<!doctype html><html><head><meta charset='utf-8'></head>"
        "<body style='background:#e11d48'><h1>Tab %d</h1><script>" % n +
        "var p=new URLSearchParams(location.search), run=p.get('run'), root=p.get('root')==='1', n=+p.get('n');"
        "function b(k){new Image().src='/beacon?run='+run+'&kind='+k;}"
        "new PerformanceObserver(function(l){for(var e of l.getEntries())if(e.name==='first-contentful-paint')b('fcp')}).observe({type:'paint',buffered:true});"
        "window.addEventListener('load',function(){b('load');"
        "if(root)for(var i=1;i<n;i++)window.open('/t?run='+run.split('-')[0]+'-'+i+'&n='+n,'_blank');});"
        "</script></body></html>").encode()

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == '/beacon':
            with lock: events.setdefault(q.get('run', ['?'])[0], []).append((q.get('kind', ['?'])[0], time.monotonic()))
            self.send_response(204); self.end_headers(); return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(page(int(q.get('n', ['0'])[0])))
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def first(kind, vals):
    return next((t for k, t in vals if k == kind), None)

def launch(browser, prof, url):
    if browser == 'ladybird':
        return ['ladybird', '--headless=manual', '--allow-popups', '--profile-path=' + prof, url]
    binary = common.binary_path(browser)
    gpu = [] if GPU else ['--disable-gpu']
    return [binary, '--headless=new', '--no-first-run'] + gpu + [
        '--disable-popup-blocking', '--user-data-dir=' + prof, url]

meta = common.make_meta('bench_tabs.py', tabs=N_TABS, rounds=ROUNDS, sample_s=SAMPLE_S,
                        gap_s=GAP, gpu=GPU, browsers=list(BROWSERS),
                        load_timeout_s=LOAD_TIMEOUT,
                        memory_rule='whole process tree PSS/RSS',
                        cpu_rule='whole tree minus gpu-process and extension-process')
results = {}

for rnd in range(ROUNDS):
    for ni, n in enumerate(N_TABS):
        order = common.rotated(BROWSERS, rnd + ni)
        for browser in order:
            token = '%s-%d-%d' % (time.time_ns(), n, rnd)
            prefix = token.split('-')[0]
            prof = tempfile.mkdtemp(prefix='lb-bench-mem-')
            url = 'http://127.0.0.1:%d/t?run=%s-0&root=1&n=%d' % (PORT, token, n)
            cmd = launch(browser, prof, url)
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + LOAD_TIMEOUT
            loaded = 0
            while time.monotonic() < deadline:
                with lock:
                    runs = {k: list(v) for k, v in events.items() if k.startswith(prefix + '-')}
                loaded = sum(1 for vals in runs.values() if first('load', vals))
                if loaded >= n: break
                time.sleep(0.1)
            complete = loaded >= n
            pids = common.tree([prof])
            root_vals = events.get(token + '-0', [])
            root_load = first('load', root_vals)
            attempts = max(0, n - 1)
            load_ok = fcp_ok = 0
            if root_load:
                with lock:
                    runs = {k: list(v) for k, v in events.items()
                            if k.startswith(prefix + '-') and k != token}
                for vals in runs.values():
                    lt, ft = first('load', vals), first('fcp', vals)
                    if lt:
                        load_ok += 1
                        results.setdefault(browser, {}).setdefault('newtab_load', []).append(
                            round((lt - root_load) * 1000, 1))
                    if ft:
                        fcp_ok += 1
                        results.setdefault(browser, {}).setdefault('newtab', []).append(
                            round((ft - root_load) * 1000, 1))
            row = {'complete': complete, 'loaded': loaded, 'requested': n,
                   'pids': len(pids), 'cmd': cmd, 'profile': prof,
                   'newtab_attempts': attempts, 'newtab_load_ok': load_ok,
                   'newtab_fcp_ok': fcp_ok, 'sample': None}
            if complete and pids:
                row['sample'] = common.sample([prof], SAMPLE_S)
                m = row['sample']
                print('r%d %-9s n=%-2d loaded=%-2d pids=%-2d pss=%.1f cpu=%.3f%% (+%.3f excluded) load/fcp=%d/%d' %
                      (rnd + 1, browser, n, loaded, m['pids'], m['pss_mb'], m['cpu_pct'],
                       m['cpu_excluded_pct'], load_ok, fcp_ok), flush=True)
            else:
                print('r%d %-9s n=%-2d INCOMPLETE loaded=%d/%d - not sampled' %
                      (rnd + 1, browser, n, loaded, n), flush=True)
            for key in ('newtab_attempts', 'newtab_load_ok', 'newtab_fcp_ok'):
                results.setdefault(browser, {}).setdefault(key, 0)
                results[browser][key] += row[key]
            results.setdefault(browser, {}).setdefault(n, []).append(row)
            p.terminate()
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired: p.kill()
            shutil.rmtree(prof, ignore_errors=True)
            time.sleep(GAP)

print('\n=== medians (complete runs only) ===')
for browser in BROWSERS:
    for n in N_TABS:
        rows = [r for r in results.get(browser, {}).get(n, [])
                if r.get('complete') and r.get('sample')]
        if not rows:
            print('%-9s %2d tabs: no complete samples' % (browser, n)); continue
        pss = [r['sample']['pss_mb'] for r in rows]
        cpu = [r['sample']['cpu_pct'] for r in rows]
        print('%-9s %2d tabs: PSS %.1f MB (%.1f-%.1f, n=%d)  CPU %.3f%% (n=%d)' %
              (browser, n, statistics.median(pss), min(pss), max(pss), len(pss),
               statistics.median(cpu), len(cpu)))
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'newtab_results.json') \
    if os.environ.get('NEWTAB_ONLY') else OUT
common.write_json(out, {'meta': meta, **results})
srv.shutdown()
