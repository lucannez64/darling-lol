"""The stamp tools color their status icons the way the launcher does.

`scripts/update.sh` paints `✔` green and `✖` red only on a terminal without
NO_COLOR; a log, a pipe or a test must stay free of escape codes.
"""
import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('console', ROOT / 'tools/console.py')
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class FakePipe(io.StringIO):
    def isatty(self):
        return False


class StatusIconTests(unittest.TestCase):
    def test_success_is_green_on_a_terminal(self):
        stream = FakeTerminal()
        console.ok('install stamp: up to date', stream)
        self.assertEqual('\033[32m✔\033[0m install stamp: up to date\n',
                         stream.getvalue())

    def test_failure_is_red_on_a_terminal(self):
        stream = FakeTerminal()
        console.fail('install stamp: cannot record outputs', stream)
        self.assertEqual('\033[31m✖\033[0m install stamp: cannot record outputs\n',
                         stream.getvalue())

    def test_a_pipe_keeps_the_line_plain(self):
        for stream in (FakePipe(), io.StringIO()):
            self.assertFalse(console.supports_color(stream))
            console.ok('configure stamp: up to date', stream)
            console.fail('configure stamp: stale format', stream)
            self.assertNotIn('\033', stream.getvalue())

    def test_no_color_wins_even_on_a_terminal(self):
        stream = FakeTerminal()
        with mock.patch.dict(os.environ, {'NO_COLOR': '1'}):
            console.ok('sdk signing stamp: up to date', stream)
        self.assertEqual('✔ sdk signing stamp: up to date\n', stream.getvalue())

    def test_missing_isatty_counts_as_a_pipe(self):
        class Bare:
            def write(self, text):
                pass

        self.assertFalse(console.supports_color(Bare()))


if __name__ == '__main__':
    unittest.main()
