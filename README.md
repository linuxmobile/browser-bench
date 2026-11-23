# browser-benchmarks

Headless Linux benchmarks for Ladybird, Ardia and Helium:

- cold launch to first paint
- PSS/RSS and idle CPU at 1, 10 and 20 tabs
- new-tab timing through popups and through CDP

Full method, comparability notes and limitations are in [BENCHMARK.md](BENCHMARK.md). The tables there are generated from the raw JSON files by `report.py`.

## Requirements

- Linux with `/proc`, Python 3.10+
- `websockets` for the Chromium CDP harnesses:

```sh
python3 -m pip install websockets
```

- Browser binaries available:
  - `ladybird`
  - `helium-browser`
  - `/usr/bin/ardia` (the browser launcher, not the separate `ardia` CLI). Override with `ARDIA_BIN=/path/to/ardia`.
- Ports 18082, 18085 and 18086 free.

Check what the harness will use:

```sh
ladybird --version
helium-browser --version
/usr/bin/ardia --version
```

## Run

Use the default filenames if you want `report.py` to include a run, and move the previous files aside first. `report.py` reads the fixed list in `FILES`; add a custom `OUT` filename there if you use one. The scripts overwrite their default files.

### Cold launch

```sh
ROUNDS=6 GAP=2 python3 bench.py
```

Writes `results.json` and prints medians. Each round rotates the browser order. `spawn -> paint` is end-to-end and includes CDP attach for Ardia and Helium. `server request -> paint` is the comparable row.

Useful settings: `ROUNDS`, `GAP`, `OUT`, `GPU=1`, `NO_STEADY=1`, `STEADY_S`.

### Memory, idle CPU and popup new tabs

```sh
TABS=1,10,20 ROUNDS=3 SAMPLE_S=6 GAP=2 \
  BROWSERS=ladybird,ardia,helium \
  OUT=all_tabs_results.json python3 bench_tabs.py
```

Expect roughly 4 to 6 minutes with these settings. Writes `all_tabs_results.json`, the primary table used by `report.py`. A replication goes in `tabs_results.json` (Ladybird, Helium) and `ardia_tabs_results.json` (Ardia).

Runs with fewer tabs than requested are marked `complete: false` and are not used in medians. CPU excludes GPU and extension processes for every browser; the excluded total is recorded as `cpu_excluded_pct`. PSS includes the whole process tree.

Useful settings: `TABS`, `ROUNDS`, `SAMPLE_S`, `GAP`, `BROWSERS`, `OUT`, `GPU=1`, `LOAD_TIMEOUT`, `NEWTAB_ONLY=1`.

### Method-specific CDP new tab

This is not comparable with the popup new-tab numbers.

```sh
N=10 WARMUP=1 BROWSER=/usr/bin/helium-browser \
  OUT=helium_newtab_cdp.json python3 newtab_cdp.py
```

This file is not read by the default `report.py` tables.

Useful settings: `N`, `WARMUP`, `BROWSER`, `OUT`, `LABEL`, `GPU=1`.

### Regenerate the document tables

```sh
python3 report.py --update BENCHMARK.md
```

`report.py` reads the files listed in its `FILES` constant. It does not drop outliers; incomplete tab runs are excluded. It bolds a winner only when all min-max ranges are disjoint.

## Files

| File                                           | Purpose                                                              |
| ---------------------------------------------- | -------------------------------------------------------------------- |
| `bench_common.py`                              | Shared process sampling, metadata, statistics and browser resolution |
| `bench.py`                                     | Cold launch                                                          |
| `bench_tabs.py`                                | Memory, idle CPU, popup new tabs                                     |
| `newtab_cdp.py`                                | CDP target new tab, method-specific                                  |
| `report.py`                                    | Generate and update the Markdown tables                              |
| `BENCHMARK.md`                                 | Method, tables, comparability and limitations                        |
| `results.json`                                 | Cold launch results                                                  |
| `all_tabs_results.json`                        | Primary all-browser memory/CPU/new-tab run                           |
| `tabs_results.json`, `ardia_tabs_results.json` | Memory replication                                                   |
| `newtab_results.json`                          | Popup new-tab replication                                            |
| `ardia_newtab.json`                            | Single Ardia CDP new-tab sample                                      |
