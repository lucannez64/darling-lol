"""Check fixture traffic and private-prefix cleanup with a fake darling executable."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class URLSessionRunnerTests(unittest.TestCase):
    def test_fixture_and_shutdown_use_only_a_private_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / 'darling'
            trace = root / 'trace.jsonl'
            executable.write_text(f'#!{sys.executable}\n' + f'TRACE = {str(trace)!r}\n' + '''
import json, os, sys, urllib.request
with open(TRACE, 'a') as stream:
    stream.write(json.dumps({'args':sys.argv[1:], 'prefix':os.environ['DPREFIX']}) + '\\n')
if sys.argv[1] == 'shutdown':
    raise SystemExit(0)
assert urllib.request.urlopen(sys.argv[2]).read() == b'darling-session-ok'
try:
    urllib.request.urlopen(sys.argv[3], timeout=2)
except OSError:
    pass
else:
    raise AssertionError('reserved error endpoint unexpectedly accepted a connection')
print('LIFECYCLE_PASS: fixture')
print('URLSESSION_PASS: fixture')
''')
            executable.chmod(0o755)
            probe = root / 'probe'
            probe.write_text('fake probe; never executed')
            log = root / 'probe.log'
            result = subprocess.run([sys.executable, str(ROOT / 'tools/riot-urlsession-check.py'),
                                     '--probe', str(probe), '--log', str(log)],
                                    env=dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH']),
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]['args'], ['shutdown'])
            self.assertEqual(calls[0]['prefix'], calls[1]['prefix'])
            self.assertNotEqual(Path(calls[0]['prefix']), Path.home() / '.darling')
            self.assertFalse(Path(calls[0]['prefix']).exists())
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
