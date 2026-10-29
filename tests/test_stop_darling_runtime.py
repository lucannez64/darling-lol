"""Exercise the shutdown script without stopping the host's Darling session."""
import errno
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'tools/stop-darling-runtime.py'


class StopRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.prefix = self.root / 'prefix'
        self.prefix.mkdir()
        self.calls = []
        self.signals = []
        self.clock = 0
        self.status_error = None
        self.shutdown = lambda: None
        self.on_signal = lambda pid, sig: self.status(pid, 'Z')

    def process(self, pid, *, state='S', parent=1, riot=False, children=()):
        folder = self.proc / str(pid)
        folder.mkdir()
        self.status(pid, state)
        (folder / 'stat').write_text(f'{pid} (test process) ' + ' '.join(
            [state, str(parent)] + ['0'] * 17 + ['12345']))
        (folder / 'comm').write_text('darlingserver' if pid == 100 else 'helper')
        (folder / 'maps').write_text(
            str(self.prefix / 'Applications/Riot Client.app/helper') if riot else '')
        task = folder / 'task' / str(pid)
        task.mkdir(parents=True)
        (task / 'children').write_text(' '.join(map(str, children)))

    def status(self, pid, state):
        (self.proc / str(pid) / 'status').write_text(f'State:\t{state} (test)\n')

    def server(self, **kwargs):
        (self.prefix / '.init.pid').write_text('100')
        self.process(100, **kwargs)

    def run_script(self):
        proc = self.proc
        original_read = Path.read_text
        owner = self

        class RuntimePath(type(Path())):
            def __new__(cls, *parts, **kwargs):
                if parts and str(parts[0]) == '/proc':
                    parts = (proc, *parts[1:])
                return super().__new__(cls, *parts, **kwargs)

            def __init__(self, *parts, **kwargs):
                if parts and str(parts[0]) == '/proc':
                    parts = (proc, *parts[1:])
                try:
                    super().__init__(*parts, **kwargs)
                except TypeError:
                    # Python 3.10's PurePath.__init__ takes no arguments; 3.12 stores them.
                    super().__init__()

            def read_text(self, *args, **kwargs):
                if self.name == 'status' and owner.status_error is not None:
                    raise owner.status_error
                return original_read(self, *args, **kwargs)

        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            if command == ['darling', 'shutdown']:
                self.shutdown()
            return subprocess.CompletedProcess(command, 0)

        def kill(pid, sig):
            self.signals.append((pid, sig))
            self.on_signal(pid, sig)

        def monotonic():
            self.clock += 1
            return self.clock

        with patch.dict(sys.modules, {'pathlib': types.SimpleNamespace(Path=RuntimePath)}), \
                patch.dict(os.environ, {'DPREFIX': str(self.prefix)}), \
                patch('subprocess.run', side_effect=run), \
                patch('os.kill', side_effect=kill), \
                patch('time.monotonic', side_effect=monotonic), \
                patch('time.sleep'):
            runpy.run_path(str(SCRIPT), run_name='__main__')

    def assert_finished(self):
        self.assertEqual(self.calls[0][0], ['darling', 'shutdown'])
        self.assertEqual(self.calls[-1], (
            [sys.executable, str(SCRIPT.with_name('ensure-darling-runtime.py'))],
            {'check': True}))

    def test_process_disappears_during_status_read(self):
        self.server()
        self.status_error = ProcessLookupError(errno.ESRCH, 'No such process')
        self.run_script()
        self.assert_finished()

    def test_missing_process(self):
        (self.prefix / '.init.pid').write_text('100')
        self.run_script()
        self.assert_finished()

    def test_no_marker(self):
        self.run_script()
        self.assert_finished()

    def test_invalid_marker(self):
        (self.prefix / '.init.pid').write_text('invalid')
        self.run_script()
        self.assert_finished()

    def test_zombie_server(self):
        self.server(state='Z')
        self.run_script()
        self.assert_finished()

    def test_server_exits_after_shutdown(self):
        self.server()
        self.shutdown = lambda: self.status(100, 'Z')
        self.run_script()
        self.assert_finished()

    def test_live_server_blocks_runtime_changes(self):
        self.server()
        with self.assertRaisesRegex(SystemExit, 'runtime remains untouched'):
            self.run_script()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.signals, [])

    def test_permission_error_is_not_treated_as_exit(self):
        self.server()
        self.status_error = PermissionError(errno.EACCES, 'Permission denied')
        with self.assertRaises(PermissionError):
            self.run_script()
        self.assertEqual(len(self.calls), 1)

    def test_descendant_gets_sigterm(self):
        self.server(children=(101,))
        self.process(101, parent=100, riot=True)
        self.shutdown = lambda: self.status(100, 'Z')
        self.run_script()
        self.assertEqual(self.signals, [(101, signal.SIGTERM)])
        self.assert_finished()

    def test_stubborn_orphan_gets_sigkill(self):
        self.process(101, riot=True)
        self.on_signal = lambda pid, sig: (
            self.status(pid, 'Z') if sig == signal.SIGKILL else None)
        self.run_script()
        self.assertEqual(self.signals, [(101, signal.SIGTERM), (101, signal.SIGKILL)])
        self.assert_finished()

    def test_surviving_helper_blocks_runtime_changes(self):
        self.process(101, riot=True)
        self.on_signal = lambda pid, sig: None
        with self.assertRaisesRegex(SystemExit, 'Old Riot helpers remain alive'):
            self.run_script()
        self.assertEqual(len(self.calls), 1)

    def test_reused_pid_is_not_signalled(self):
        self.server(children=(101,))
        self.process(101, parent=100, riot=True)

        def shutdown():
            self.status(100, 'Z')
            stat = self.proc / '101/stat'
            stat.write_text(stat.read_text().replace('12345', '54321'))

        self.shutdown = shutdown
        self.run_script()
        self.assertEqual(self.signals, [])
        self.assert_finished()


if __name__ == '__main__':
    unittest.main()
