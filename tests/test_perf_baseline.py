"""Frame-pacing baseline from League's own netstats.csv logs; reads files only."""
import contextlib
import csv
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/perf-baseline.py'
SPEC = importlib.util.spec_from_file_location('perf_baseline', SCRIPT)
perf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(perf)

HEADER = ['game.frametime_average', 'game.frametime_variance', 'game.time',
          'network.rtt_average', 'network.packets_lost']


def write_match(folder, frametimes, name='2026-10-23T17-03-29'):
    target = Path(folder) / name / ('%s_netstats.csv' % name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for index, frametime in enumerate(frametimes):
            writer.writerow([frametime, 0.001, index * 10.0, 30.0, 0])
    return target


class MeasureTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_metrics(self):
        path = write_match(self.root, [0.03] * 6 + [0.06, 0.09])
        result = perf.measure(path)
        self.assertEqual(result['match'], '2026-10-23 17:03')
        self.assertEqual(result['samples'], 8)
        self.assertEqual(result['seconds'], 80.0)
        self.assertEqual(result['avg_fps'], 24.2)
        self.assertEqual(result['sample_p50_ms'], 30.0)
        self.assertEqual(result['sample_p95_ms'], 90.0)
        self.assertEqual(result['slowest_quarter_fps'], 13.3)
        self.assertEqual(result['slowest_quarter_ms'], 75.0)
        self.assertEqual(result['hitches'], 2)

    def test_file_is_a_basename_and_not_a_path(self):
        path = write_match(self.root, [0.03])
        self.assertEqual(perf.measure(path)['file'], path.name)
        self.assertNotIn(str(self.root), json.dumps(perf.measure(path)))

    def test_non_finite_time_and_huge_frames_are_skipped(self):
        path = self.root / '2026-01-01T00-00-00' / '2026-01-01T00-00-00_netstats.csv'
        path.parent.mkdir()
        path.write_text(','.join(HEADER) + '\n'
                        '0.03,0,nan,0,0\n'
                        '0.03,0,inf,0,0\n'
                        '1e308,0,10,0,0\n'
                        '0.03,0,10,0,0\n')
        self.assertEqual(perf.measure(path)['samples'], 1)

    def test_bad_and_empty_rows_are_skipped(self):
        path = self.root / '2026-01-01T00-00-00' / '2026-01-01T00-00-00_netstats.csv'
        path.parent.mkdir()
        path.write_text(','.join(HEADER) + '\n'
                        ',0,0,0,0\n'
                        '0.000,0,0,0,0\n'
                        'nan,0,0,0,0\n'
                        '0.050,0,10,0,0\n'
                        'oops,0,20,0,0\n')
        result = perf.measure(path)
        self.assertEqual(result['samples'], 1)
        self.assertEqual(result['avg_fps'], 20.0)

    def test_rows_without_frametime_return_none(self):
        path = self.root / '2026-01-01T00-00-00' / '2026-01-01T00-00-00_netstats.csv'
        path.parent.mkdir()
        path.write_text(','.join(HEADER) + '\n')
        self.assertIsNone(perf.measure(path))

    def test_percentile_bounds(self):
        self.assertEqual(perf.percentile([], 0.5), 0.0)
        self.assertEqual(perf.percentile([3, 1, 2], 0.0), 1)
        self.assertEqual(perf.percentile([3, 1, 2], 0.5), 2)
        self.assertEqual(perf.percentile([3, 1, 2], 1.0), 3)

    def test_match_label_falls_back_to_folder(self):
        self.assertEqual(perf.match_label('/tmp/odd-folder/file.csv'), 'odd-folder')


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.prefix = Path(self.folder.name)
        self.logs = self.prefix / perf.DEFAULT_GAME_LOGS
        self.old = write_match(self.logs, [0.03], '2026-10-21T10-00-00')
        self.new = write_match(self.logs, [0.03], '2026-10-23T10-00-00')
        os.utime(self.old, (1000, 1000))
        os.utime(self.new, (2000, 2000))
        unrelated = self.logs / '2026-10-22T10-00-00' / 'other.csv'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('x')

    def tearDown(self):
        self.folder.cleanup()

    def test_newest_first_and_only_netstats(self):
        self.assertEqual(perf.discover(self.prefix), [self.new, self.old])


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.path = write_match(self.root, [0.04, 0.04, 0.04, 0.08])
        self.result = perf.measure(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def test_render_table(self):
        text = perf.render([self.result])
        self.assertIn('avgfps', text)
        self.assertIn('2026-10-23 17:03', text)
        self.assertIn('hitch', text)

    def test_render_empty_explains_expected_path(self):
        text = perf.render([])
        self.assertIn('No netstats.csv files found', text)
        self.assertIn('Applications/League of Legends.app', text)

    def test_append_writes_header_once(self):
        path = self.root / 'baseline.csv'
        perf.append_csv([self.result], path)
        perf.append_csv([self.result], path)
        with open(path, newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['match'], '2026-10-23 17:03')
        self.assertEqual(rows[0]['avg_fps'], '20.0')
        self.assertEqual(rows[0]['sample_p50_ms'], '40.0')
        self.assertNotIn('p50_ms', rows[0])
        self.assertEqual(path.read_text().count('measured,match'), 1)

    def test_json_cli(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = perf.main(['--csv', str(self.path), '--json'])
        self.assertEqual(code, 0)
        document = json.loads(stream.getvalue())
        self.assertEqual(document['matches'][0]['samples'], 4)

    def test_last_counts_usable_matches(self):
        empty = self.root / '2026-10-24T10-00-00' / '2026-10-24T10-00-00_netstats.csv'
        empty.parent.mkdir(parents=True)
        empty.write_text(','.join(HEADER) + '\n')
        results = perf.collect([empty, self.path], 1)
        self.assertEqual([result['match'] for result in results], ['2026-10-23 17:03'])

    def test_duration_includes_the_final_sample_interval(self):
        path = write_match(self.root, [0.04] * 4)
        self.assertEqual(perf.measure(path)['seconds'], 40.0)

    def test_cli_without_matches_returns_one(self):
        missing = self.root / 'missing.csv'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(perf.main(['--csv', str(missing)]), 1)


if __name__ == '__main__':
    unittest.main()
