"""Select the automatic client-in-game mode from LCULocalPreferences.yaml."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'client_in_game_tricks', ROOT / 'tools/client-in-game-tricks.py')
tricks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tricks)


class ClientInGameTricksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'LCULocalPreferences.yaml'

    def write(self, optimization=None, close=None):
        lines = ['lol-user-experience:', '    data:']
        if optimization is not None:
            lines.append(f'        potatoModeEnabled: {optimization}')
        if close is not None:
            lines.append(f'        closeLeagueClientDuringGame: {close}')
        self.path.write_text('\n'.join(lines) + '\n')

    def test_close_when_both_preferences_are_true(self):
        self.write(True, True)
        self.assertEqual(tricks.mode(self.path), 'close')

    def test_hide_when_optimizations_are_on_and_close_is_false(self):
        self.write(True, False)
        self.assertEqual(tricks.mode(self.path), 'hide')

    def test_none_when_close_preference_is_missing_or_invalid(self):
        self.write(True)
        self.assertEqual(tricks.mode(self.path), 'none')
        for value in ('maybe', '"true', 'true#note', '"true"junk'):
            self.write(True, value)
            self.assertEqual(tricks.mode(self.path), 'none', value)

    def test_optimizations_off_close_only_when_close_preference_is_true(self):
        self.write(False, True)
        self.assertEqual(tricks.mode(self.path), 'close')
        self.write(False, False)
        self.assertEqual(tricks.mode(self.path), 'none')

    def test_optimizations_missing_close_only_when_close_preference_is_true(self):
        self.write(None, True)
        self.assertEqual(tricks.mode(self.path), 'close')
        self.write(None, False)
        self.assertEqual(tricks.mode(self.path), 'none')
        self.assertEqual(tricks.mode(self.path.with_name('missing.yaml')), 'none')

    def test_last_duplicate_value_wins_and_quotes_are_accepted(self):
        self.path.write_text(
            'lol-user-experience:\n'
            '    data:\n'
            '        potatoModeEnabled: false\n'
            '        closeLeagueClientDuringGame: false\n'
            '        potatoModeEnabled: "true"\n'
            '        closeLeagueClientDuringGame: "true"\n')
        self.assertEqual(tricks.mode(self.path), 'close')

    def test_keys_outside_the_named_section_are_ignored(self):
        self.path.write_text(
            'lol-user-experience:\n'
            '    data:\n'
            '        potatoModeEnabled: true\n'
            '        closeLeagueClientDuringGame: false\n'
            'other-section:\n'
            '    potatoModeEnabled: true\n'
            '    closeLeagueClientDuringGame: true\n')
        self.assertEqual(tricks.mode(self.path), 'hide')

    def test_inline_yaml_comments_are_ignored(self):
        self.path.write_text(
            'lol-user-experience:\n'
            '    data:\n'
            '        potatoModeEnabled: true # client optimizations\n'
            '        closeLeagueClientDuringGame: "true" # close during matches\n')
        self.assertEqual(tricks.mode(self.path), 'close')

    def test_second_read_failure_fails_closed(self):
        self.write(True, False)
        text = self.path.read_text()
        with patch.object(Path, 'read_text', side_effect=[text, OSError()]):
            self.assertEqual(tricks.mode(self.path), 'none')

    def test_main_prints_mode(self):
        self.write(True, False)
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(tricks.main([str(self.path)]), 0)
        self.assertEqual(output.getvalue().strip(), 'hide')


if __name__ == '__main__':
    unittest.main()
