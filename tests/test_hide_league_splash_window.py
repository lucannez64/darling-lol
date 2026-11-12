"""Exercise the opt-in League splash window hide without a live X11 server."""
import importlib.util
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'hide_league_splash', ROOT / 'tools/hide-league-splash-window.py')
hide_splash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hide_splash)


def attributes(width=512, height=216, override=1, mapped=2):
    return SimpleNamespace(
        width=width, height=height,
        override_redirect=override, map_state=mapped,
    )


class FakeX11:
    def __init__(self, windows=(), hints=None):
        self.windows = list(windows)
        self.hints = hints or {}
        self.unmapped = []

    def top_level_windows(self):
        return list(self.windows)

    def attributes(self, window):
        return self.hints[window][0]

    def class_hint(self, window):
        return self.hints[window][1]

    def unmap(self, window):
        self.unmapped.append(window)

    def close(self):
        pass


class SplashWindowTests(unittest.TestCase):
    def test_accepts_only_the_fixed_override_redirect_splash(self):
        self.assertTrue(hide_splash.is_splash(
            attributes(), b'LeagueClient', b'LeagueClient'))
        self.assertFalse(hide_splash.is_splash(
            attributes(width=1024), b'LeagueClient', b'LeagueClient'))
        self.assertFalse(hide_splash.is_splash(
            attributes(override=0), b'LeagueClient', b'LeagueClient'))
        self.assertFalse(hide_splash.is_splash(
            attributes(mapped=0), b'LeagueClient', b'LeagueClient'))
        self.assertFalse(hide_splash.is_splash(
            attributes(), b'LeagueClientUx', b'LeagueClientUx'))

    def test_finds_only_matching_top_level_windows(self):
        x11 = FakeX11([0x10, 0x20, 0x30], {
            0x10: (attributes(), (b'LeagueClient', b'LeagueClient')),
            0x20: (attributes(), (b'LeagueClientUx', b'LeagueClientUx')),
            0x30: (attributes(width=512, height=200), (b'LeagueClient', b'LeagueClient')),
        })
        self.assertEqual(hide_splash.find_splash_windows(x11), [0x10])

    def test_watcher_unmaps_and_reports_a_window_once(self):
        x11 = FakeX11(hints={})
        output = io.StringIO()
        watcher = hide_splash.Watcher(
            Path('/tmp/prefix'), x11,
            window_source=lambda _x11: [0xabc],
            client_source=lambda _prefix: True,
            sleep=lambda _seconds: None,
            out=output,
        )
        watcher.run(max_steps=2)
        self.assertEqual(x11.unmapped, [0xabc, 0xabc])
        self.assertEqual(output.getvalue().count('hidden 0xabc'), 1)

    def test_watcher_leaves_after_an_idle_client_session(self):
        output = io.StringIO()
        clock = iter([0, 30, 60, 90, 120, 150, 180])
        watcher = hide_splash.Watcher(
            Path('/tmp/prefix'), FakeX11(),
            window_source=lambda _x11: [],
            client_source=lambda _prefix: False,
            sleep=lambda _seconds: None,
            out=output,
            clock=lambda: next(clock),
        )
        self.assertEqual(watcher.run(), 0)
        self.assertIn('no client session; leaving', output.getvalue())

    def test_unreadable_process_state_keeps_watching(self):
        output = io.StringIO()
        watcher = hide_splash.Watcher(
            Path('/tmp/prefix'), FakeX11(),
            window_source=lambda _x11: [],
            client_source=lambda _prefix: None,
            sleep=lambda _seconds: None,
            out=output,
        )
        watcher.run(max_steps=3)
        self.assertNotIn('no client session', output.getvalue())

    def test_unreadable_process_state_does_not_hide(self):
        x11 = FakeX11()
        watcher = hide_splash.Watcher(
            Path('/tmp/prefix'), x11,
            window_source=lambda _x11: [0xabc],
            client_source=lambda _prefix: None,
            sleep=lambda _seconds: None,
            out=io.StringIO(),
        )
        watcher.run(max_steps=3)
        self.assertEqual(x11.unmapped, [])

    def test_once_does_not_hide_anything(self):
        x11 = FakeX11([0x10], {
            0x10: (attributes(), (b'LeagueClient', b'LeagueClient')),
        })
        output = io.StringIO()
        with patch.dict(os.environ, {'DPREFIX': '/tmp/prefix'}), \
             patch.object(hide_splash, 'X11', return_value=x11), \
             patch.object(hide_splash.sys, 'stdout', output):
            self.assertEqual(hide_splash.main(['--once']), 0)
        self.assertEqual(x11.unmapped, [])
        self.assertIn('1 splash window(s) 0x10', output.getvalue())

    def test_disabled_without_opt_in(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'RCLOL_HIDE_LEAGUE_SPLASH': ''}), \
             patch.object(hide_splash.sys, 'stdout', output):
            self.assertEqual(hide_splash.main([]), 0)
        self.assertIn('disabled; set RCLOL_HIDE_LEAGUE_SPLASH=1', output.getvalue())


if __name__ == '__main__':
    unittest.main()
