#!/usr/bin/env python3
"""Regenerate the comparison tables from the raw JSON files.

Why this exists: the previous table was copied by hand, which let expired or
partly-sourced numbers survive next to reproducible ones. This script reads the
files in this directory, states which file every row comes from, keeps all
saved samples (including outliers), and bolds a winner only when the observed
min-max intervals do not overlap.

Usage:
    ./report.py                 # print the generated Markdown block
    ./report.py --update BENCHMARK.md   # replace the marked block in a file
"""
import datetime, json, os, sys

import bench_common as common

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ['results.json', 'all_tabs_results.json', 'tabs_results.json',
         'ardia_tabs_results.json', 'newtab_results.json', 'ardia_newtab.json',
         'xbench_results.json']
LABEL = {'ladybird': 'Ladybird', 'ardia': 'Ardia (Linux)', 'helium': 'Helium'}


def load(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def samples_for(doc, browser, n):
    """Normalise old and new bench_tabs rows into sample dicts."""
    if not doc or browser not in doc:
        return []
    rows = doc[browser].get(str(n), doc[browser].get(n, []))
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get('sample'):
            s = dict(row['sample'])
            s['complete'] = row.get('complete', True)
        elif 'pss_mb' in row:
            s = dict(row)
            s['complete'] = row.get('loaded', n) == n
        else:
            continue
        if s.get('complete', True) and s.get('pss_mb') is not None:
            out.append(s)
    return out


def series(doc, browser, key):
    if not doc or browser not in doc:
        return []
    vals = doc[browser].get(key, [])
    return [v for v in vals if v is not None]


def num(x, digits=1):
    if x is None:
        return '–'
    return ('%.*f' % (digits, x))


def cell(s, unit='', digits=1, bold=False):
    if not s:
        return '–'
    text = '%s%s (%s–%s, n=%d)' % (
        num(s['median'], digits), unit, num(s['min'], digits),
        num(s['max'], digits), s['n'])
    return '**%s**' % text if bold else text


def row_cells(summaries, unit='', digits=1, lower=True, ineligible=()):
    """Bold the lowest median only when every interval is disjoint."""
    present = {b: s for b, s in summaries.items() if s}
    eligible = {b: s for b, s in present.items() if b not in ineligible}
    bold = None
    if eligible and common.disjoint(*eligible.values()):
        key = min if lower else max
        bold = key(eligible, key=lambda b: eligible[b]['median'])
    return ' | '.join(cell(present.get(b), unit, digits, b == bold)
                      for b in common.BROWSERS)


def provenance():
    rows = ['| File | Modified | Metadata | Contents |',
            '| --- | --- | --- | --- |']
    for name in FILES:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            rows.append('| `%s` | missing | – | – |' % name)
            continue
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M')
        doc = load(name)
        has_meta = 'yes' if isinstance(doc, dict) and doc.get('meta') else 'no (pre-refactor)'
        if name == 'results.json':
            what = 'cold launch (spawn→paint, page FCP)'
        elif name in ('all_tabs_results.json', 'tabs_results.json', 'ardia_tabs_results.json'):
            what = 'memory, CPU, popup new-tab'
        elif name == 'newtab_results.json':
            what = 'popup new-tab replication'
        elif name == 'xbench_results.json':
            what = 'Ardia-shaped CDP session rows'
        else:
            what = 'CDP-target new-tab (method-specific)'
        rows.append('| `%s` | %s | %s | %s |' % (name, mt, has_meta, what))
    return '\n'.join(rows)


def xbench_cell(values, unit=' ms'):
    s = common.summarize(values or [])
    if not s:
        return '–'

    def n(x):
        text = num(x)
        return text[:-2] if text.endswith('.0') else text

    return '%s%s (%s–%s, n=%d)' % (n(s['median']), unit, n(s['min']), n(s['max']), s['n'])


def xbench_table():
    """Ardia-shaped rows, filled only where the harness can measure them."""
    doc = load('xbench_results.json') or {}

    def vals(browser, key, field):
        values = doc.get(browser, {}).get(key) or []
        if field:
            values = [r.get(field) for r in values if isinstance(r, dict)]
        return values

    def cell(browser, key, field, unit):
        if browser == 'ladybird' or not key:
            return '–'
        return xbench_cell(vals(browser, key, field), unit)

    lines = ['| Row | Ladybird | Ardia (Linux) | Helium |',
             '| --- | --- | --- | --- |']

    def row(title, key=None, field=None, unit=' ms'):
        cells = [cell(b, key, field, unit) for b in common.BROWSERS]
        lines.append('| %s | %s |' % (title, ' | '.join(cells)))

    row('Cold launch → page painted (link opened from another app)')
    row('Cold launch → window on screen')
    row('Cold launch → restored tab painted')
    row('New tab → first paint', 'newtab_ms')
    row('Tab switch → shown', 'switch_ms')
    row('New window → first paint', 'newwindow_ms')
    row('Memory, 1 tab (PSS on Linux)', 'memory_1', 'pss_mb', ' MB')
    row('Memory, 10 tabs (PSS on Linux)', 'memory_10', 'pss_mb', ' MB')
    row('Memory, 20 tabs (PSS on Linux)', 'memory_20', 'pss_mb', ' MB')
    row('Memory, back to 1 tab after 20 (PSS on Linux)', 'memory_back_1', 'pss_mb', ' MB')
    row('Idle CPU, 1 tab', 'memory_1', 'cpu_pct', ' %')
    row('Idle CPU, 20 tabs', 'memory_20', 'cpu_pct', ' %')
    row('Idle wakeups, 1 tab')
    row('Idle wakeups, 20 tabs')

    errors = ['%s: %s' % (b, r['error'])
              for b in common.BROWSERS
              for r in doc.get(b, {}).get('rows', []) if r.get('error')]
    if errors:
        lines += ['', 'Session errors:', ''] + ['- ' + e for e in errors]

    lines += ['',
              '`–` means not measured. `bench_xbench.py` covers only CDP-observable rows: Ladybird '
              'has no CDP target API, and Ardia\'s Linux headless build does not paint additional '
              'same-window tabs. Link launch, window-on-screen, restore, idle wakeups and '
              'download/on-disk rows cannot be measured in this setup. Ardia\'s idle CPU reflects '
              'the headless WebUI spin described below.']
    return '\n'.join(lines)


def generated():
    cold = load('results.json') or {}
    c = cold.get('cold', {})
    all_tabs = load('all_tabs_results.json')
    rep_tabs = load('tabs_results.json')
    rep_ardia = load('ardia_tabs_results.json')
    rep_newtab = load('newtab_results.json')
    ardia_cdp = load('ardia_newtab.json')

    lines = []
    lines.append('_Generated by `report.py` from the raw JSON files; do not edit by hand._')
    lines.append('')
    lines.append('### Data provenance')
    lines.append('')
    lines.append(provenance())
    lines.append('')

    # ---- upstream relationship -------------------------------------------
    lines.append('### Upstream source and reuse')
    lines.append('')
    lines.append('The row set follows Ardia\'s published '
                 '[`vs-browsers.md`](https://github.com/ardia-browser/ardia/blob/main/docs/perf/vs-browsers.md); '
                 'none of its numbers appear here.')
    lines.append('')
    lines.append('The [Ardia plugins bench harness](https://github.com/ardia-browser/plugins/tree/'
                 '65e968a3d7a0bf152a4da280da2bbb7b923762fa/plugins/cli/bench/harness) '
                 '(commit `65e968a3d7a0bf152a4da280da2bbb7b923762fa`, Apache-2.0) benchmarks agents and the '
                 'CLI, not browsers. Its launch, CDP tab, process, server and summary approaches '
                 '(`browsers.py`, `cdp_tab.mjs`, `procs.py`, `servers.py`, `summary.py`) were reimplemented for '
                 'Linux and these three browsers in `bench.py`, `newtab_cdp.py`, `bench_common.py`, '
                 '`bench_xbench.py` and `report.py`. No upstream code or runtime dependency is used; these '
                 'numbers are comparable only with each other on this machine, not with Ardia\'s published '
                 'results.')
    lines.append('')

    # ---- Ardia-shaped rows ------------------------------------------------
    lines.append('### Ardia-shaped rows (this machine)')
    lines.append('')
    lines.append('Same row names as [Ardia\'s table](https://github.com/ardia-browser/ardia/blob/main/docs/perf/vs-browsers.md), '
                 'with our three browsers. Methods are described in `bench_xbench.py`; this is not Ardia\'s harness.')
    lines.append('')
    lines.append(xbench_table())
    lines.append('')

    # ---- cold launch -----------------------------------------------------
    lines.append('### Cold launch to first paint')
    lines.append('')
    lines.append('Bold marks the lower median only where all min–max intervals are disjoint.')
    lines.append('')
    lines.append('| Measurement | Ladybird | Ardia (Linux) | Helium |')
    lines.append('| --- | --- | --- | --- |')
    def cold_row(title, key, oldkey=None, unit=' ms'):
        s = {b: common.summarize(series(c, b, key) or (series(c, b, oldkey) if oldkey else []))
             for b in common.BROWSERS}
        lines.append('| %s | %s |' % (title, row_cells(s, unit)))
    available_nav = any(series(c, b, 'nav_to_paint_ms') for b in common.BROWSERS)
    if available_nav:
        cold_row('Server request → paint (comparable)', 'nav_to_paint_ms')
    else:
        lines.append('| Server request → paint (comparable) | – (not captured; rerun bench.py) | – | – |')
    cold_row('Process spawn → paint (end-to-end; includes CDP attach/setup for Ardia and Helium)',
             'spawn_to_paint_ms', 'wall')
    cold_row('Page-reported FCP (different clock origins; not a like-for-like row)',
             'page_fcp_ms', 'fcp', ' ms')
    lines.append('')
    lines.append('Source: `results.json`. The spawn row favours Ladybird because Chromium needs '
                 'CDP attach and an explicit `Page.navigate`; the request→paint row exists so the '
                 'two families have one comparable number.')
    lines.append('')

    # ---- memory / CPU ----------------------------------------------------
    lines.append('### Memory and idle CPU (primary run)')
    lines.append('')
    lines.append('Bold marks the lower median only where all min–max intervals are disjoint. The current '
                 'harness includes the full process tree in PSS and excludes only GPU/extension '
                 'processes from CPU. The saved run predates that: it excluded extension processes '
                 'from the tree, so PSS for browsers that spawn extensions may be slightly low. '
                 'Re-running `bench_tabs.py` produces the corrected accounting.')
    lines.append('')
    lines.append('| Tabs | Metric | Ladybird | Ardia (Linux) | Helium |')
    lines.append('| --- | --- | --- | --- | --- |')
    for n in (1, 10, 20):
        pss = {b: common.summarize([s['pss_mb'] for s in samples_for(all_tabs, b, n)]) for b in common.BROWSERS}
        cpu = {b: common.summarize([s['cpu_pct'] for s in samples_for(all_tabs, b, n)]) for b in common.BROWSERS}
        rss = {b: common.summarize([s['rss_mb'] for s in samples_for(all_tabs, b, n)]) for b in common.BROWSERS}
        lines.append('| %d | PSS | %s |' % (n, row_cells(pss, ' MB')))
        lines.append('| %d | RSS | %s |' % (n, row_cells(rss, ' MB')))
        lines.append('| %d | Idle CPU (%% of one core) | %s |' %
                     (n, row_cells(cpu, ' %', 3, ineligible=('ardia',))))
    lines.append('')
    ardia_cpu = [s['cpu_pct'] for n in (1, 10, 20) for s in samples_for(all_tabs, 'ardia', n)]
    if ardia_cpu and min(ardia_cpu) > 50:
        lines.append('Ardia is shown for memory but its CPU numbers are not comparable: the '
                     '`chrome://ardia` WebUI renderer spins at %s–%s %% of one core while headless, '
                     'which dominates the idle measurement.' % (num(min(ardia_cpu)), num(max(ardia_cpu))))
        lines.append('')
    lines.append('Source: `all_tabs_results.json` (the only saved run that contains all three browsers).')
    lines.append('')

    # ---- replication -----------------------------------------------------
    lines.append('### Memory replication')
    lines.append('')
    lines.append('A second invocation, reported separately rather than pooled with the primary run.')
    lines.append('')
    lines.append('| Tabs | Metric | Ladybird | Ardia (Linux) | Helium |')
    lines.append('| --- | --- | --- | --- | --- |')
    for n in (1, 10, 20):
        def merge(b):
            return samples_for(rep_tabs, b, n) + samples_for(rep_ardia, b, n)
        pss = {b: common.summarize([s['pss_mb'] for s in merge(b)]) for b in common.BROWSERS}
        cpu = {b: common.summarize([s['cpu_pct'] for s in merge(b)]) for b in common.BROWSERS}
        lines.append('| %d | PSS | %s |' % (n, row_cells(pss, ' MB')))
        lines.append('| %d | Idle CPU (%% of one core) | %s |' % (n, row_cells(cpu, ' %', 3, ineligible=('ardia',))))
    lines.append('')
    lines.append('Sources: `tabs_results.json` (Ladybird, Helium) and `ardia_tabs_results.json` (Ardia). '
                 'Different invocation from the primary run, so the two tables are not pooled.')
    lines.append('')

    # ---- new tab ---------------------------------------------------------
    lines.append('### New tab to first paint')
    lines.append('')
    lines.append('The popup method in `bench_tabs.py` is the only method that measures every browser '
                 'the same way: a page opens tabs with `window.open()` and the page beacons the '
                 'paint. Coverage is shown as successes/attempts because headless Chromium often '
                 'omits paint beacons even when the tab loads.')
    lines.append('')
    lines.append('| Method | Ladybird | Ardia (Linux) | Helium |')
    lines.append('| --- | --- | --- | --- |')
    def newtab_row(doc, key, title, unit=' ms'):
        s = {b: common.summarize(series(doc, b, key)) for b in common.BROWSERS}
        lines.append('| %s | %s |' % (title, row_cells(s, unit)))
    if any(series(all_tabs, b, 'newtab_load') for b in common.BROWSERS):
        newtab_row(all_tabs, 'newtab_load', 'Popup → page load (comparable, primary run)')
    else:
        lines.append('| Popup → page load (comparable, primary run) | – | – | – |')
    newtab_row(all_tabs, 'newtab', 'Popup → FCP (primary run)')
    lines.append('')
    def cov(b):
        attempts = (all_tabs or {}).get(b, {}).get('newtab_attempts')
        fcp = len(series(all_tabs, b, 'newtab'))
        if attempts is None:
            return '%s FCP %d samples (attempt count not recorded)' % (LABEL[b], fcp)
        return '%s FCP %d/%d' % (LABEL[b], fcp, attempts)
    lines.append('Primary-run coverage: ' + '; '.join(cov(b) for b in common.BROWSERS) + '.')
    if not any(series(all_tabs, b, 'newtab_load') for b in common.BROWSERS):
        lines.append('')
        lines.append('Load-beacon timing was not recorded in the saved run; the harness now '
                     'records it as the method-neutral new-tab row.')
    lines.append('')
    lines.append('| Replication | Ladybird | Ardia (Linux) | Helium |')
    lines.append('| --- | --- | --- | --- |')
    newtab_row(rep_newtab, 'newtab', 'Popup → FCP (`newtab_results.json`)')
    if any(series(rep_newtab, b, 'newtab_load') for b in common.BROWSERS):
        newtab_row(rep_newtab, 'newtab_load', 'Popup → load (`newtab_results.json`)')
    lines.append('')
    lines.append('| Method-specific, not comparable | Ladybird | Ardia (Linux) | Helium |')
    lines.append('| --- | --- | --- | --- |')
    if isinstance(ardia_cdp, list):
        cdp = common.summarize([x for x in ardia_cdp if x is not None])
    elif isinstance(ardia_cdp, dict):
        cdp = common.summarize([x for x in ardia_cdp.get('values', []) if x is not None])
    else:
        cdp = None
    lines.append('| CDP `Target.createTarget` → FCP | – (no CDP harness) | %s | run `newtab_cdp.py` |' %
                 (cell(cdp, ' ms') if cdp else '–'))
    lines.append('')
    lines.append('The CDP-target method is deliberately kept out of the popup rows: it creates a '
                 'target, focuses it, then navigates, and its clock starts at `Page.navigate`, so '
                 'its numbers cannot be subtracted from, or ranked against, the popup numbers.')
    lines.append('')
    return '\n'.join(lines)


def main():
    text = generated()
    if len(sys.argv) == 3 and sys.argv[1] == '--update':
        path = sys.argv[2]
        begin, end = '<!-- BEGIN GENERATED -->', '<!-- END GENERATED -->'
        doc = open(path).read()
        if begin not in doc or end not in doc:
            sys.exit('%s lacks the generated-block markers' % path)
        head, rest = doc.split(begin, 1)
        _, tail = rest.split(end, 1)
        open(path, 'w').write(head + begin + '\n\n' + text + '\n' + end + tail)
        print('updated %s' % path)
    else:
        print(text)


if __name__ == '__main__':
    main()
