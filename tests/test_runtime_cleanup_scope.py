"""Cleanup scope regressions: fake proc/prefix trees and mocked signals only."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / 'tools/ensure-darling-runtime.py'


class RuntimeCleanupScopeTests(unittest.TestCase):
    def enterContext(self, manager):
        """unittest.TestCase.enterContext() is Python 3.11+; Ubuntu 22.04 ships 3.10."""
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cleanup-scope-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.prefix = self.root / 'selected prefix'
        self.other = self.root / 'unrelated prefix'
        self.shm = self.root / 'shm'
        for folder in (self.prefix / 'private/tmp', self.other / 'private/tmp', self.shm):
            folder.mkdir(parents=True)
        # Guard signals before import; no real process may ever be signalled.
        self.kill = self.enterContext(patch('os.kill'))
        spec = importlib.util.spec_from_file_location('cleanup_scope', SCRIPT)
        assert spec is not None and spec.loader is not None
        self.cleanup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cleanup)
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.dict(os.environ, {'DPREFIX': str(self.prefix)}))
        self.enterContext(patch.object(self.cleanup, 'Path', side_effect=self.fake_path))
        # Its default Path('/proc') is bound at definition time, before patching Path.
        running = self.cleanup.any_server_running
        self.enterContext(patch.object(self.cleanup, 'any_server_running',
                                     side_effect=lambda proc_root=None: running(
                                         self.proc if proc_root is None else proc_root)))

    def fake_path(self, value):
        # Never inspect host /proc or remove host shared-memory files.
        return {'/proc': self.proc, '/dev/shm': self.shm}.get(str(value), Path(value))

    def process(self, pid, *, comm='launchd', argv=('/sbin/launchd',), state='S'):
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / 'comm').write_text(comm + '\n')
        (folder / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv) + b'\0')
        (folder / 'status').write_text(f'State:\t{state} (fixture)\n')
        return folder

    def test_direct_cleanup_entry_points_preserve_ambiguous_processes(self):
        self.process(101)
        self.process(102, comm='chrome_crashpad', argv=(
            '/Applications/Riot Client.app/Contents/Frameworks/chrome_crashpad_handler',))
        self.cleanup.stop_orphaned_launchd(self.proc)
        self.cleanup.stop_riot_crashpad(self.proc)
        self.kill.assert_not_called()
        self.assertIn('prefix ownership', self.stderr.getvalue())
        self.assertIn('launchd candidate PIDs 101', self.stderr.getvalue())
        self.assertIn('Riot crashpad candidate PIDs 102', self.stderr.getvalue())

    def test_clean_start_is_silent(self):
        self.cleanup.main()
        self.assertEqual(self.stderr.getvalue(), '')
        self.kill.assert_not_called()

    def test_unrelated_processes_are_silent(self):
        self.process(110, comm='chrome_crashpad', argv=(
            '/opt/google/chrome/chrome_crashpad_handler', '--annotation=riot'))
        self.process(111, comm='unrelated', argv=('/usr/bin/tool', '/sbin/launchd'))
        self.process(112, comm='unrelated', argv=('/usr/bin/tool',
            '/Applications/Riot Client.app/chrome_crashpad_handler'))
        self.cleanup.main()
        self.assertEqual(self.stderr.getvalue(), '')
        self.kill.assert_not_called()

    def test_foreign_owned_candidates_are_silent(self):
        self.process(113)
        self.process(114, comm='chrome_crashpad', argv=(
            '/Applications/Riot Client.app/chrome_crashpad_handler',))
        with patch.object(Path, 'stat', return_value=Mock(st_uid=os.getuid() + 1)):
            self.cleanup.stop_orphaned_launchd(self.proc)
            self.cleanup.stop_riot_crashpad(self.proc)
        self.assertEqual(self.stderr.getvalue(), '')
        self.kill.assert_not_called()

    def test_main_preserves_processes_from_another_prefix_without_a_server(self):
        self.process(101)
        self.process(102, comm='chrome_crashpad', argv=(
            str(self.other / 'Applications/Riot Client.app/crashpad_handler'),))
        selected = self.prefix / 'private/tmp/.com.riotgames.selected'
        selected.write_text('stale')
        unrelated = self.other / 'private/tmp/.com.riotgames.other'
        unrelated.write_text('preserve')
        shared = self.shm / 'darling-buffer-fixture'
        shared.write_text('stale')
        self.cleanup.main()
        self.kill.assert_not_called()
        self.assertFalse(selected.exists())
        self.assertFalse(shared.exists())
        self.assertEqual(unrelated.read_text(), 'preserve')

    def test_any_live_server_preserves_locks_and_shared_buffers(self):
        self.process(103, comm='darlingserver', argv=('darlingserver',))
        lock = self.prefix / 'private/tmp/.com.riotgames.lock'
        lock.write_text('active')
        shared = self.shm / 'darling-buffer-fixture'
        shared.write_text('active')
        self.cleanup.main()
        self.kill.assert_not_called()
        self.assertTrue(lock.exists())
        self.assertTrue(shared.exists())

    def test_unreadable_process_list_fails_closed(self):
        hidden = MagicMock()
        hidden.name = '104'
        (hidden / 'comm').read_text.side_effect = PermissionError('hidden')
        proc = Mock()
        proc.iterdir.return_value = [hidden]
        self.assertTrue(self.cleanup.any_server_running(proc))
        self.kill.assert_not_called()

    def test_dead_marker_is_recovered_only_in_the_selected_prefix(self):
        self.process(105, state='Z')
        marker = self.prefix / '.init.pid'
        marker.write_text('105')
        other = self.other / '.init.pid'
        other.write_text('105')
        self.cleanup.main()
        self.assertFalse(marker.exists())
        recovered = list(self.prefix.glob('.init.pid.stale-105-*'))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_text(), '105')
        self.assertEqual(other.read_text(), '105')
        self.kill.assert_not_called()

    def test_live_marker_is_not_replaced(self):
        self.process(106)
        marker = self.prefix / '.init.pid'
        marker.write_text('106')
        self.cleanup.main()
        self.assertEqual(marker.read_text(), '106')
        self.kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
