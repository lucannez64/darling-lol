"""Exercise parallel TLV checking in tools/check-darwin-tlv-calls.py."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / 'tools/check-darwin-tlv-calls.py'

FAKE_OBJDUMP = '''#!/bin/sh
mode=$1
shift
for last; do path=$last; done
if [ "$mode" = "--macho" ]; then
  printf '  1 __thread_vars 00000010 0000000000002000 DATA\\n'
  exit 0
fi
printf '0000000000001000 <_fn>:\\n'
case "$(basename "$path")" in
  bad-*)
    printf '    1000: leaq 0xff9(%%rip), %%rax  # 0x2000\\n'
    printf '    1007: callq *(%%rax)\\n'
    ;;
  *)
    printf '    1000: leaq 0xff9(%%rip), %%rdi  # 0x2000\\n'
    printf '    1007: callq *(%%rdi)\\n'
    ;;
esac
'''


class CheckDarwinTlvCallsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='tlv-check-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.bindir = self.dir / 'bin'
        self.bindir.mkdir()
        objdump = self.bindir / 'llvm-objdump'
        objdump.write_text(FAKE_OBJDUMP)
        objdump.chmod(objdump.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ)
        self.env['PATH'] = f'{self.bindir}:{self.env["PATH"]}'

    def _macho(self, name):
        path = self.dir / name
        path.write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 16)
        return str(path)

    def test_parallel_and_serial_passes_on_clean_binaries(self):
        bins = [self._macho(f'good-{i}.dylib') for i in range(6)]
        for jobs in ('1', '4'):
            with self.subTest(jobs=jobs):
                res = subprocess.run(
                    [sys.executable, str(CHECKER), *bins],
                    env={**self.env, 'RCLOL_TLV_JOBS': jobs},
                    capture_output=True, text=True,
                )
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_parallel_reports_bad_binaries_in_deterministic_order(self):
        bins = [
            self._macho('bad-1.dylib'),
            self._macho('good-2.dylib'),
            self._macho('bad-3.dylib'),
            self._macho('bad-4.dylib'),
        ]
        res = subprocess.run(
            [sys.executable, str(CHECKER), *bins],
            env={**self.env, 'RCLOL_TLV_JOBS': '4'},
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 1)
        lines = [ln for ln in res.stdout.splitlines() if 'thread-local access' in ln]
        self.assertEqual(len(lines), 3)
        self.assertIn('bad-1.dylib', lines[0])
        self.assertIn('bad-3.dylib', lines[1])
        self.assertIn('bad-4.dylib', lines[2])


if __name__ == '__main__':
    unittest.main()
