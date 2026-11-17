"""Exercise the in-match client window hide with fake /proc entries and X11."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'hide_in_game', ROOT / 'tools/hide-league-client-in-game.py')
hide_in_game = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hide_in_game)

GAME = '/Applications/League of Legends.app/Contents/LoL/LeagueofLegends'
MAIN = '/Applications/League of Legends.app/Contents/LoL/LeagueClientUx.app/Contents/MacOS/LeagueClientUx'
RIOT = '/Applications/Riot Client.app/Contents/MacOS/RiotClientServices'


class FakeX11:
    def __init__(self):
        self.unmapped = []
        self.shown = []

    def unmap(self, window):
        self.unmapped.append(window)

    def show(self, window):
        self.shown.append(window)


class HideClientInGameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.prefix = self.root / '.darling'
        self.env_patch = patch.dict(os.environ, {'DPREFIX': str(self.prefix)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def environment(self, prefix):
        return b'%s=%s\0' % (
            hide_in_game.SOCK_VARIABLE,
            os.fsencode(prefix / hide_in_game.SOCK_NAME),
        )

    def process(self, pid, executable=GAME, prefix='own', environ=None, args=('--flag',)):
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / 'cmdline').write_bytes(
            executable.encode() + b'\0' +
            b''.join(arg.encode() + b'\0' for arg in args))
        if environ is None:
            environ = self.environment(self.prefix if prefix == 'own' else prefix)
        (folder / 'environ').write_bytes(environ)
        (folder / 'stat').write_text(f'{pid} (game) S')
        return folder

    def test_game_running_for_own_prefix(self):
        self.process(123)
        self.assertTrue(hide_in_game.game_running(self.prefix, self.proc))

    def test_no_game_is_false(self):
        self.process(123, executable=MAIN)
        self.assertFalse(hide_in_game.game_running(self.prefix, self.proc))

    def test_game_of_another_prefix_is_false(self):
        self.process(123, prefix=self.root / 'other')
        self.assertFalse(hide_in_game.game_running(self.prefix, self.proc))

    def test_game_without_socket_is_false(self):
        self.process(123, environ=b'')
        self.assertFalse(hide_in_game.game_running(self.prefix, self.proc))

    def test_unreadable_environment_is_unknown(self):
        folder = self.process(123)
        original = Path.read_bytes

        def read(path):
            if path.name == 'environ':
                raise PermissionError()
            return original(path)

        with patch.object(Path, 'read_bytes', read):
            self.assertIsNone(hide_in_game.game_running(self.prefix, self.proc))

    def test_unreadable_process_directory_is_unknown(self):
        class UnreadableProc:
            def iterdir(self):
                raise OSError()

        self.assertIsNone(hide_in_game.game_running(self.prefix, UnreadableProc()))

    def test_host_socket_for_guest_uses_nspid(self):
        host = self.proc / '999'
        host.mkdir()
        (host / 'status').write_text('Name:\tCrBrowserMain\nNSpid:\t999\t5877\n')
        (host / 'environ').write_bytes(self.environment(self.prefix))
        self.assertEqual(
            hide_in_game.host_socket_for_guest(5877, self.proc),
            os.fsencode(self.prefix / hide_in_game.SOCK_NAME),
        )

    def test_window_belongs_to_prefix(self):
        socket = os.fsencode(self.prefix / hide_in_game.SOCK_NAME)
        with patch.object(hide_in_game, 'window_pid', return_value=5877), \
             patch.object(hide_in_game, 'host_socket_for_guest', return_value=socket):
            self.assertTrue(hide_in_game.window_belongs_to_prefix('0x10', self.prefix))
            self.assertFalse(hide_in_game.window_belongs_to_prefix(
                '0x10', self.root / 'other'))

    def test_once_runs_without_opt_in(self):
        output = io.StringIO()
        with patch.dict(os.environ, {
            'RCLOL_HIDE_CLIENT_IN_GAME': '',
            'RCLOL_CLOSE_CLIENT_IN_GAME': '',
        }), patch.object(hide_in_game, 'target_prefix',
                         return_value=self.prefix), \
             patch.object(hide_in_game, 'game_running', return_value=True), \
             patch.object(hide_in_game.sys, 'stdout', output):
            self.assertEqual(hide_in_game.main(['--once']), 0)
        self.assertIn('match=running', output.getvalue())

    def test_target_windows_ignores_game_class(self):
        with patch.object(hide_in_game, 'xprop_root',
                          return_value=['0x10', '0x20', '0x30']), \
             patch.object(hide_in_game, 'window_class',
                          side_effect=[
                              'wm_class(string) = "leagueclientux"',
                              'wm_class(string) = "leagueoflegends"',
                              'wm_class(string) = "riot client"',
                          ]), \
             patch.object(hide_in_game, 'window_belongs_to_prefix', return_value=True):
            self.assertEqual(hide_in_game.target_windows(self.prefix), [0x10, 0x30])

    def test_riot_client_only_flag_limits_the_target_classes(self):
        # main() narrows the module's classes; patch.object restores them afterwards
        with patch.object(hide_in_game, 'TARGET_CLASSES', hide_in_game.TARGET_CLASSES), \
             patch.dict(os.environ, {'RCLOL_HIDE_CLIENT_IN_GAME': '', 'RCLOL_CLOSE_CLIENT_IN_GAME': '',
                                     'RCLOL_CLIENT_IN_GAME_TRICKS': ''}):
            hide_in_game.main(['--riot-client-only'])
            self.assertEqual(hide_in_game.TARGET_CLASSES, ('riot client',))
            with patch.object(hide_in_game, 'xprop_root', return_value=['0x10', '0x30']), \
                 patch.object(hide_in_game, 'window_class', side_effect=[
                     'wm_class(string) = "leagueclientux"',
                     'wm_class(string) = "riot client"',
                 ]), \
                 patch.object(hide_in_game, 'window_belongs_to_prefix', return_value=True):
                self.assertEqual(hide_in_game.target_windows(self.prefix), [0x30])

    def test_close_mode_can_also_hide_the_riot_client(self):
        source = (ROOT / 'tools/run-riot-client.sh').read_text()
        close = source[source.index('closeLeagueClientDuringGame=true -> close helper'):source.index('   hide)')]
        self.assertIn('if [ "${RCLOL_HIDE_RIOT_CLIENT_IN_GAME:-0}" = 1 ]; then', close)
        self.assertIn('hide-league-client-in-game.py" --detach --riot-client-only &', close)

    def test_target_windows_requires_this_prefix(self):
        with patch.object(hide_in_game, 'xprop_root',
                          return_value=['0x10', '0x20']), \
             patch.object(hide_in_game, 'window_class',
                          return_value='wm_class(string) = "leagueclientux"'), \
             patch.object(hide_in_game, 'window_belongs_to_prefix',
                          side_effect=[True, False]):
            self.assertEqual(hide_in_game.target_windows(self.prefix), [0x10])

    def test_target_windows_caches_non_empty_classes(self):
        cache = {}
        with patch.object(hide_in_game, 'xprop_root', return_value=['0x10']), \
             patch.object(hide_in_game, 'window_class',
                          return_value='wm_class(string) = "leagueclientux"') as classes, \
             patch.object(hide_in_game, 'window_belongs_to_prefix', return_value=True):
            self.assertEqual(hide_in_game.target_windows(self.prefix, cache), [0x10])
            self.assertEqual(hide_in_game.target_windows(self.prefix, cache), [0x10])
        self.assertEqual(classes.call_count, 1)

    def test_missing_wm_class_is_not_cached_as_a_class(self):
        with patch.object(hide_in_game, 'xprop_value',
                          return_value='WM_CLASS:  not found.'):
            self.assertEqual(hide_in_game.window_class('0x10'), '')

    def test_unmap_failure_removes_the_window_from_hidden(self):
        class FailingX11(FakeX11):
            def unmap(self, window):
                raise RuntimeError('unmap failed')

        watcher = hide_in_game.Watcher(
            self.prefix, FailingX11(),
            window_source=lambda _prefix, _cache: [0xabc],
            state_source=lambda _prefix: True,
            sleep=lambda _seconds: None,
            out=io.StringIO(),
        )
        watcher._hide_new()
        self.assertEqual(watcher.hidden, set())

    def test_interrupted_unmap_keeps_the_window_tracked(self):
        shown = []

        class InterruptingX11:
            def unmap(self, window):
                raise KeyboardInterrupt

            def show(self, window):
                shown.append(window)

        watcher = hide_in_game.Watcher(
            self.prefix, InterruptingX11(),
            window_source=lambda _prefix, _cache: [0xabc],
            state_source=lambda _prefix: True,
            sleep=lambda _seconds: None,
            out=io.StringIO(),
        )
        with self.assertRaises(KeyboardInterrupt):
            watcher._hide_new()
        self.assertEqual(watcher.hidden, {0xabc})
        watcher.restore()
        self.assertEqual(shown, [0xabc])

    def test_log_failure_does_not_discard_an_unmapped_window(self):
        class BrokenOutput:
            def write(self, _text):
                raise BrokenPipeError

            def flush(self):
                pass

        watcher = hide_in_game.Watcher(
            self.prefix, FakeX11(),
            window_source=lambda _prefix, _cache: [0xabc],
            state_source=lambda _prefix: True,
            sleep=lambda _seconds: None,
            out=BrokenOutput(),
        )
        watcher._hide_new()
        self.assertEqual(watcher.hidden, {0xabc})

    def test_restore_log_failure_still_shows_the_window(self):
        class BrokenOutput:
            def write(self, _text):
                raise BrokenPipeError

            def flush(self):
                pass

        x11 = FakeX11()
        watcher = hide_in_game.Watcher(
            self.prefix, x11,
            window_source=lambda _prefix, _cache: [0xabc],
            state_source=lambda _prefix: True,
            sleep=lambda _seconds: None,
            out=BrokenOutput(),
        )
        watcher.hidden = {0xabc}
        watcher._show_all('test')
        self.assertEqual(x11.shown, [0xabc])
        self.assertEqual(watcher.hidden, set())

    def test_idle_timeout_leaves_after_client_disappears(self):
        states = iter([True, False, False])
        windows = iter([[0xabc], [], []])
        times = iter([0.0, 10.0, 700.0])
        x11 = FakeX11()
        output = io.StringIO()
        watcher = hide_in_game.Watcher(
            self.prefix, x11,
            window_source=lambda _prefix, _cache: next(windows),
            state_source=lambda _prefix: next(states),
            sleep=lambda _seconds: None,
            out=output,
            clock=lambda: next(times),
        )
        self.assertEqual(watcher.run(max_steps=3), 0)
        self.assertEqual(x11.unmapped, [0xabc])
        self.assertEqual(x11.shown, [0xabc])
        self.assertIn('no client or match', output.getvalue())

    def test_hides_then_shows_on_match_end(self):
        states = iter([True, True, False])
        x11 = FakeX11()
        output = io.StringIO()
        watcher = hide_in_game.Watcher(
            self.prefix, x11,
            window_source=lambda _prefix, _cache: [0xabc],
            state_source=lambda _prefix: next(states),
            sleep=lambda _seconds: None,
            out=output,
        )
        watcher.run(max_steps=3)
        self.assertEqual(x11.unmapped, [0xabc])
        self.assertEqual(x11.shown, [0xabc])
        self.assertIn('hidden 0xabc', output.getvalue())
        self.assertIn('the match ended', output.getvalue())
        self.assertIn('shown 0xabc', output.getvalue())

    def test_unreadable_state_restores_the_window(self):
        states = iter([True, None])
        x11 = FakeX11()
        output = io.StringIO()
        watcher = hide_in_game.Watcher(
            self.prefix, x11,
            window_source=lambda _prefix, _cache: [0xdef],
            state_source=lambda _prefix: next(states),
            sleep=lambda _seconds: None,
            out=output,
        )
        watcher.run(max_steps=2)
        self.assertEqual(x11.unmapped, [0xdef])
        self.assertEqual(x11.shown, [0xdef])
        self.assertIn('unreadable /proc state', output.getvalue())

    def test_no_game_does_not_touch_windows(self):
        x11 = FakeX11()
        watcher = hide_in_game.Watcher(
            self.prefix, x11,
            window_source=lambda _prefix, _cache: [0xabc],
            state_source=lambda _prefix: False,
            sleep=lambda _seconds: None,
            out=io.StringIO(),
        )
        watcher.run(max_steps=1)
        self.assertEqual(x11.unmapped, [])
        self.assertEqual(x11.shown, [])

    def test_main_is_disabled_by_default(self):
        with patch.dict(os.environ, {
            'RCLOL_HIDE_CLIENT_IN_GAME': '',
            'RCLOL_CLOSE_CLIENT_IN_GAME': '',
        }):
            self.assertEqual(hide_in_game.main([]), 0)

    def test_close_mode_takes_precedence(self):
        with patch.dict(os.environ, {
            'RCLOL_HIDE_CLIENT_IN_GAME': '1',
            'RCLOL_CLOSE_CLIENT_IN_GAME': '1',
        }):
            self.assertEqual(hide_in_game.main([]), 0)

    def test_helper_starts_after_the_installed_client_check(self):
        source = (ROOT / 'tools/run-riot-client.sh').read_text()
        self.assertLess(source.index('Riot Client is not installed in'),
                        source.index('RCLOL_CLIENT_IN_GAME_TRICKS'))

    def test_master_tricks_switch_enables_hide(self):
        class FakeX11:
            def close(self):
                pass

        watcher = Mock()
        with patch.dict(os.environ, {
            'RCLOL_CLIENT_IN_GAME_TRICKS': '1',
            'RCLOL_HIDE_CLIENT_IN_GAME': '',
            'RCLOL_CLOSE_CLIENT_IN_GAME': '',
        }), patch.object(hide_in_game, 'target_prefix',
                         return_value=self.prefix), \
             patch.object(hide_in_game, 'X11', return_value=FakeX11()), \
             patch.object(hide_in_game, 'Watcher', return_value=watcher):
            self.assertEqual(hide_in_game.main([]), 0)
        watcher.run.assert_called_once_with()
        watcher.restore.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
