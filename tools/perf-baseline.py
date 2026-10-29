#!/usr/bin/env python3
"""Measure frame pacing from the game's own `netstats.csv` logs.

League writes one row per ten seconds of a match, including `game.frametime_average`
(seconds per frame) and `game.time`. This tool turns those rows into numbers that
can be compared before and after a performance change:

- average FPS over the match,
- p50/p95/p99 frame time,
- average FPS of the slowest quarter of the match (the part you feel as stutter),
- the count of samples at least twice as slow as the match median (hitches).

It never reads or starts the game; it only parses files that are already on disk.

Usage:
    python3 tools/perf-baseline.py                    # every match in the prefix
    python3 tools/perf-baseline.py --last 5           # the five newest matches
    python3 tools/perf-baseline.py --json
    python3 tools/perf-baseline.py --append baseline.csv
    python3 tools/perf-baseline.py --prefix /path/to/prefix --csv path/to/netstats.csv

`--append` writes one row per match to a CSV file, so several runs on the same
laptop can be compared later without re-parsing the logs.
"""
import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path

FRAMETIME_COLUMN = 'game.frametime_average'
TIME_COLUMN = 'game.time'
DEFAULT_GAME_LOGS = Path(
    'Applications/League of Legends.app/Contents/LoL/Logs/GameLogs')
HITCH_FACTOR = 2.0
# A ten-second sample above a minute is corrupt, and would make derived
# milliseconds non-finite in the JSON report.
MAX_FRAMETIME_SECONDS = 60.0
MAX_GAME_SECONDS = 6 * 60 * 60
SAMPLE_SECONDS = 10.0
APPEND_HEADER = (
    'measured', 'match', 'file', 'samples', 'seconds', 'avg_fps', 'mean_ms',
    'sample_p50_ms', 'sample_p95_ms', 'sample_p99_ms',
    'slowest_quarter_fps', 'slowest_quarter_ms', 'hitches', 'host',
)


def default_prefix():
    return Path(os.environ.get('DPREFIX') or (Path.home() / '.darling')).expanduser()


def discover(prefix=None):
    """Return netstats files, newest first, for a Darling prefix."""
    prefix = Path(prefix or default_prefix()).expanduser()
    return sorted(Path(prefix, DEFAULT_GAME_LOGS).glob('*/*_netstats.csv'),
                  key=lambda path: path.stat().st_mtime, reverse=True)


def read_rows(path):
    """Yield (game_time, frametime_seconds) for usable rows."""
    try:
        handle = open(path, newline='')
    except OSError:
        return
    with handle:
        for row in csv.DictReader(handle):
            try:
                frametime = float(row.get(FRAMETIME_COLUMN) or '')
                game_time = float(row.get(TIME_COLUMN) or 0)
            except (TypeError, ValueError):
                continue
            if (math.isfinite(frametime) and 0 < frametime <= MAX_FRAMETIME_SECONDS
                    and math.isfinite(game_time) and 0 <= game_time <= MAX_GAME_SECONDS):
                yield game_time, frametime


def match_label(path):
    match = re.search(r'(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})',
                      Path(path).parent.name or Path(path).name)
    if match:
        return '%s %s:%s' % (match.group(1), match.group(2), match.group(3))
    return Path(path).parent.name


def percentile(values, fraction):
    """Nearest-rank percentile; values may be unsorted."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def measure(path, measured=None):
    """Return one match's metrics as a flat dict."""
    samples = list(read_rows(path))
    if not samples:
        return None
    times = [time_value for time_value, _ in samples]
    frames = [frametime for _, frametime in samples]
    intervals = [later - earlier for earlier, later in zip(times, times[1:]) if later > earlier]
    interval = statistics.median(intervals) if intervals else SAMPLE_SECONDS
    seconds = max(times) - min(times) + interval if len(times) > 1 else interval
    focus = sorted(frames)[-max(1, math.ceil(len(frames) / 4)):]
    median = statistics.median(frames)
    return {
        'measured': measured or time.strftime('%Y-%m-%dT%H:%M:%S'),
        'match': match_label(path),
        'file': Path(path).name,
        'samples': len(frames),
        'seconds': round(seconds, 1),
        'avg_fps': round(len(frames) / sum(frames), 1),
        'mean_ms': round(statistics.mean(frames) * 1000, 2),
        'sample_p50_ms': round(percentile(frames, 0.50) * 1000, 2),
        'sample_p95_ms': round(percentile(frames, 0.95) * 1000, 2),
        'sample_p99_ms': round(percentile(frames, 0.99) * 1000, 2),
        'slowest_quarter_fps': round(len(focus) / sum(focus), 1),
        'slowest_quarter_ms': round(statistics.mean(focus) * 1000, 2),
        'hitches': sum(1 for frametime in frames if frametime >= median * HITCH_FACTOR),
        'host': host_summary(),
    }


def host_summary():
    """Short host name for the append CSV, so rows from several machines differ."""
    model = ''
    try:
        model = Path('/sys/devices/virtual/dmi/id/product_name').read_text().strip()
    except OSError:
        pass
    cpu = ''
    try:
        for line in Path('/proc/cpuinfo').read_text().splitlines():
            if line.startswith('model name'):
                cpu = line.split(':', 1)[1].strip()
                break
    except OSError:
        pass
    return ' / '.join(part for part in (model, cpu) if part) or 'unknown'


def collect(paths, limit=None):
    """Measure usable matches; --last counts matches, not file paths."""
    results = []
    for path in paths:
        result = measure(path)
        if result:
            results.append(result)
            if limit and len(results) >= limit:
                break
    return results


def render(results):
    if not results:
        return ('No netstats.csv files found. Play a match first, or pass '
                '--prefix/--csv.\nExpected under: <prefix>/%s' % DEFAULT_GAME_LOGS)
    lines = [
        '%-17s %6s %6s %6s %6s %6s %6s %5s' % (
            'match', 'avgfps', 'p50ms', 'p95ms', 'p99ms', 'slowfps', 'slowms', 'hitch'),
    ]
    for result in results:
        lines.append('%-17s %6.1f %6.2f %6.2f %6.2f %6.1f %6.2f %5d' % (
            result['match'], result['avg_fps'], result['sample_p50_ms'],
            result['sample_p95_ms'], result['sample_p99_ms'],
            result['slowest_quarter_fps'], result['slowest_quarter_ms'],
            result['hitches']))
    lines.append('')
    lines.append('avgfps = 1 / mean sampled frame time. p50/p95/p99 are percentiles of')
    lines.append('the ten-second samples, not of individual frames. slowfps/slowms cover')
    lines.append('the slowest quarter of the match. hitch = samples at least %.0fx the'
                 % HITCH_FACTOR)
    lines.append('median frame time.')
    return '\n'.join(lines)


def append_csv(results, path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file() and path.stat().st_size > 0
    with open(path, 'a', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=APPEND_HEADER, extrasaction='ignore')
        if not exists:
            writer.writeheader()
        for result in results:
            writer.writerow(result)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Measure League frame pacing from netstats.csv logs.')
    parser.add_argument('--prefix', type=Path, help='Darling prefix (default: DPREFIX or ~/.darling)')
    parser.add_argument('--csv', type=Path, action='append',
                        help='measure this netstats.csv (repeatable); skips discovery')
    parser.add_argument('--last', type=int, help='only the N newest matches')
    parser.add_argument('--json', action='store_true', help='print JSON instead of a table')
    parser.add_argument('--append', type=Path, help='append one CSV row per match to this file')
    args = parser.parse_args(argv)

    if args.csv:
        paths = args.csv
    else:
        prefix = args.prefix or default_prefix()
        paths = discover(prefix)
    results = collect(paths, args.last)

    if args.json:
        print(json.dumps({'schema': 1, 'tool': 'darling-lol perf-baseline',
                          'host': host_summary(), 'matches': results}, indent=2))
    else:
        print(render(results))
    if args.append and results:
        written = append_csv(results, args.append)
        if not args.json:
            print('Appended %d row(s) to %s' % (len(results), written))
    return 0 if results else 1


if __name__ == '__main__':
    sys.exit(main())
