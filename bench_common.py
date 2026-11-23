#!/usr/bin/env python3
"""Shared measurement helpers for the browser benchmarks.

Process accounting, environment capture, statistics and JSON provenance live here
so every harness applies the same rules to each browser.
"""
import json, os, platform, shutil, statistics, subprocess, time

BROWSERS = ('ladybird', 'ardia', 'helium')
BINARY = {'ladybird': 'ladybird', 'helium': 'helium-browser', 'ardia': 'ardia'}


def _first_line(cmd):
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=8, text=True).stdout
        return out.strip().splitlines()[0] if out.strip() else None
    except Exception:
        return None


def binary_path(name):
    """Resolve the browser executable deterministically.

    For Ardia, PATH may contain the separate `ardia` CLI (a different binary)
    while the documented browser launcher is /usr/bin/ardia. Prefer the
    launcher so a benchmark run cannot accidentally attach to, or measure, a
    different installation. ARDIA_BIN overrides this on other systems.
    """
    override = os.environ.get('ARDIA_BIN') if name == 'ardia' else None
    if override:
        return override
    if name == 'ardia' and os.path.exists('/usr/bin/ardia'):
        return '/usr/bin/ardia'
    return shutil.which(BINARY[name]) or BINARY[name]


def browser_info(name):
    path = binary_path(name)
    return {'binary': BINARY[name], 'path': path,
            'version': _first_line([path, '--version'])}


def os_info():
    distro = None
    try:
        for line in open('/etc/os-release'):
            if line.startswith('PRETTY_NAME='):
                distro = line.split('=', 1)[1].strip().strip('"')
    except OSError:
        pass
    u = os.uname()
    return {'system': u.sysname, 'release': u.release, 'machine': u.machine,
            'distro': distro, 'python': platform.python_version()}


def git_state():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        commit = subprocess.run(['git', '-C', here, 'rev-parse', '--short', 'HEAD'],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(['git', '-C', here, 'status', '--porcelain'],
                                    capture_output=True, text=True, timeout=5).stdout.strip())
        return {'commit': commit or None, 'dirty': dirty}
    except Exception:
        return {'commit': None, 'dirty': None}


def make_meta(tool, **settings):
    """Provenance block written into every result file."""
    return {
        'tool': tool,
        'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'git': git_state(),
        'os': os_info(),
        'loadavg_at_start': [round(x, 2) for x in os.getloadavg()],
        'browsers': {b: browser_info(b) for b in BROWSERS},
        'settings': settings,
    }


def proc_snapshot():
    """One pass over /proc: pid -> {ppid, cmd, jiffies}."""
    snap = {}
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            cmd = open('/proc/%d/cmdline' % pid, 'rb').read().replace(b'\0', b' ')
            raw = open('/proc/%d/stat' % pid).read()
            tail = raw[raw.rindex(')') + 2:].split()
            snap[pid] = {'ppid': int(tail[1]), 'cmd': cmd,
                         'jiffies': int(tail[11]) + int(tail[12])}
        except (OSError, ValueError, IndexError):
            continue
    return snap


def tree(markers, snap=None, exclude=None):
    """PIDs whose cmdline contains any marker, plus all of their descendants.

    markers: iterable of bytes. exclude: optional predicate on cmdline bytes.
    A single /proc snapshot is used, so the cost stays linear in process count.
    """
    markers = [m.encode() if isinstance(m, str) else m for m in markers]
    snap = snap or proc_snapshot()
    roots = {pid for pid, p in snap.items() if any(m in p['cmd'] for m in markers)}
    children = {}
    for pid, p in snap.items():
        children.setdefault(p['ppid'], []).append(pid)
    out, frontier = set(), list(roots)
    while frontier:
        pid = frontier.pop()
        if pid in out:
            continue
        out.add(pid)
        frontier.extend(children.get(pid, ()))
    if exclude:
        out = {pid for pid in out if not exclude(snap[pid]['cmd'])}
    return out


def _mem(pid):
    rss = pss = 0
    try:
        for line in open('/proc/%d/status' % pid):
            if line.startswith('VmRSS:'):
                rss = int(line.split()[1])
    except OSError:
        pass
    try:
        for line in open('/proc/%d/smaps_rollup' % pid):
            if line.startswith('Pss:'):
                pss = int(line.split()[1])
    except OSError:
        pass
    return rss, pss


# CPU accounting rule, applied identically to every browser:
#   * GPU processes are excluded (software rendering noise in headless mode).
#   * extension processes are excluded (they spin at ~90 % in this environment).
# The excluded totals are still reported so the reader can see what was removed.
def cpu_excluded(cmd):
    return b'--type=gpu-process' in cmd or b'--extension-process' in cmd


def sample(markers, seconds, exclude_cpu=cpu_excluded):
    """Sample memory and CPU over `seconds` for the tree matching `markers`.

    Memory includes every process in the tree (nothing is silently dropped).
    CPU is a delta over processes present in both snapshots, with excluded
    processes totalled separately.
    """
    a = proc_snapshot()
    pa = tree(markers, a)
    t0 = time.monotonic()
    time.sleep(seconds)
    b = proc_snapshot()
    pb = tree(markers, b)
    dt = time.monotonic() - t0

    rss = pss = 0
    for pid in pb:
        r, p = _mem(pid)
        rss += r
        pss += p

    cpu_j = cpu_ex_j = 0
    for pid in pa & pb:
        delta = max(0, b[pid]['jiffies'] - a[pid]['jiffies'])
        if exclude_cpu and exclude_cpu(b[pid]['cmd']):
            cpu_ex_j += delta
        else:
            cpu_j += delta

    return {
        'rss_mb': round(rss / 1024, 1),
        'pss_mb': round(pss / 1024, 1),
        'cpu_pct': round(cpu_j / 100 / dt * 100, 3),
        'cpu_excluded_pct': round(cpu_ex_j / 100 / dt * 100, 3),
        'pids': len(pb),
        'new_pids': len(pb - pa),
        'sample_s': round(dt, 2),
    }


def summarize(values):
    """Median, range and n over the non-None values. No values are dropped."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return {
        'n': len(v), 'median': statistics.median(v),
        'min': v[0], 'max': v[-1],
    }


def disjoint(*summaries):
    """True when every summary's [min,max] interval is disjoint from the rest.

    Used by report.py to decide whether a bold 'winner' is justified by the
    saved data instead of being a difference smaller than the observed noise.
    """
    s = [x for x in summaries if x]
    for i, a in enumerate(s):
        for b in s[i + 1:]:
            if not (a['max'] < b['min'] or b['max'] < a['min']):
                return False
    return len(s) > 1


def rotated(seq, i):
    """Round-robin rotation, so fixed order cannot systematically favour a browser."""
    if not seq:
        return seq
    k = i % len(seq)
    return seq[k:] + seq[:k]


def write_json(path, payload):
    with open(path, 'w') as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write('\n')
