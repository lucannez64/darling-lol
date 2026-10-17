"""Verify menu launch diagnostics without starting Darling or a client."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DesktopRunTests(unittest.TestCase):
    def test_private_log_and_arguments(self):
        with tempfile.TemporaryDirectory(prefix='desktop runner ') as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/desktop-run.sh', root / 'scripts/desktop-run.sh')
            run = root / 'scripts/run.sh'
            run.write_text('#!/bin/sh\nprintf "stdout:%s\\n" "$1"\nprintf "stderr\\n" >&2\nexit 7\n')
            run.chmod(0o755)
            (root / 'build').mkdir()
            log = root / 'build/desktop-launch.log'
            log.write_text('previous\n')
            log.chmod(0o644)
            result = subprocess.run(['sh', str(root / 'scripts/desktop-run.sh'), 'two words'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout, '')
            self.assertEqual(result.stderr, '')
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertEqual(log.read_text(), 'previous\nstdout:two words\nstderr\n')


if __name__ == '__main__':
    unittest.main()
