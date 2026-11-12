"""Exercise orphan-helper cleanup with fake /proc entries and mocked signals."""
import importlib.util
import io
import os
from pathlib import Path
import signal
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'clean_stale_helpers', ROOT / 'tools/clean-stale-helpers.py')
clean_stale_helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clean_stale_helpers)

HELPER = '/repo/tools/close-league-client-in-game.py'
HIDE_HELPER = '/repo/tools/hide-league-client-in-game.py'
SPLASH_HELPER = '/repo/tools/hide-league-splash-window.py'


class CleanStaleHelpersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.prefix = self.root / '.darling'
        self.other = self.root / 'other-prefix'

    def parent(self, pid, comm):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / 'comm').write_text(comm + '\n')

    def process(self, pid, script=HELPER, parent_pid=500, parent_comm='systemd',
                prefix='own', argv0='/usr/bin/python3', state='S',
                environment=True, arguments=('--detach',)):
        folder = self.proc / str(pid)
        folder.mkdir()
        argv = [argv0, script, *arguments]
        (folder / 'cmdline').write_bytes(
            b'\0'.join(arg.encode() for arg in argv) + b'\0')
        if parent_comm is not None:
            self.parent(parent_pid, parent_comm)
        fields = [state, str(parent_pid)] + ['0'] * 17 + ['100']
        (folder / 'stat').write_text(f'{pid} (python3) ' + ' '.join(fields) + '\n')
        if environment:
            prefix = self.prefix if prefix == 'own' else prefix
            (folder / 'environ').write_bytes(
                b'DPREFIX=' + os.fsencode(prefix) + b'\0')
        else:
            (folder / 'environ').write_bytes(b'UNRELATED=1\0')
        return folder

    def run_clean(self, dry_run=False):
        killed = []

        def kill(pid, signum):
            killed.append((pid, signum))

        out = io.StringIO()
        result = clean_stale_helpers.clean(
            self.prefix, proc_root=self.proc, kill=kill,
            sleep=lambda _seconds: None, out=out, dry_run=dry_run)
        return result, killed, out.getvalue()

    def test_removes_only_orphaned_helpers_of_this_prefix(self):
        self.process(100, HELPER)
        self.process(101, HIDE_HELPER)
        self.process(102, SPLASH_HELPER)
        self.process(200, HELPER, parent_pid=600, parent_comm='darling')
        self.process(201, HELPER, prefix=self.other)
        self.process(202, HELPER, argv0='/usr/bin/vim')
        self.process(203, HELPER, state='Z')
        self.process(204, HELPER, environment=False)
        self.process(205, HELPER, parent_pid=700, parent_comm=None)
        result, killed, output = self.run_clean()
        self.assertEqual(result, 0)
        self.assertEqual(sorted(killed), sorted([
            (100, signal.SIGTERM), (100, signal.SIGKILL),
            (101, signal.SIGTERM), (101, signal.SIGKILL),
            (102, signal.SIGTERM), (102, signal.SIGKILL),
        ]))
        self.assertIn('removing 3 orphaned helper(s): 100,101,102', output)

    def test_dry_run_reports_without_signalling(self):
        self.process(100)
        result, killed, output = self.run_clean(dry_run=True)
        self.assertEqual((result, killed), (0, []))
        self.assertIn('would remove 1 orphaned helper(s): 100', output)

    def test_no_orphans_is_a_noop(self):
        self.process(200, parent_pid=600, parent_comm='darling')
        result, killed, output = self.run_clean()
        self.assertEqual((result, killed), (0, []))
        self.assertIn('no orphaned helpers', output)

    def test_relative_helper_prefix_is_preserved(self):
        self.process(100, prefix='relative-prefix')
        result, killed, output = self.run_clean()
        self.assertEqual((result, killed), (0, []))
        self.assertIn('no orphaned helpers', output)

    def test_launcher_runs_cleanup_before_starting_a_helper(self):
        source = (ROOT / 'tools/run-riot-client.sh').read_text()
        self.assertIn('RCLOL_CLEAN_STALE_HELPERS', source)
        self.assertLess(source.index('clean-stale-helpers.py'),
                        source.index('close-league-client-in-game.py'))


if __name__ == '__main__':
    unittest.main()
