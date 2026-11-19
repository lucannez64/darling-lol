"""Unit coverage for the CMakeCache.txt warning in scripts/build.sh.

build.sh runs for real in a temporary repo, with fake python3 and cmake
binaries first on PATH that only record argv, and RCLOL_SKIP_PREFLIGHT=1.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'scripts/build.sh'
SH = shutil.which('sh')

STUB = '''#!/bin/sh
printf '%s\\n' "$*" >> "${BUILD_STUB_LOG:?}"
printf '%s\\n' "$PWD" >> "${BUILD_STUB_LOG:?}.pwd"
'''


class BuildCacheWarningTests(unittest.TestCase):
    def setUp(self):
        if SH is None:
            raise unittest.SkipTest('sh is unavailable')
        tmp = tempfile.TemporaryDirectory(prefix='build-cache-warning ')
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.repo = folder / 'repo'
        self.bindir = folder / 'bin'
        self.log = folder / 'stubs.log'
        (self.repo / 'scripts').mkdir(parents=True)
        self.bindir.mkdir()
        shutil.copy2(BUILD, self.repo / 'scripts' / 'build.sh')
        for name in ('python3', 'cmake'):
            stub = self.bindir / name
            stub.write_text(STUB)
            stub.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(RCLOL_SKIP_PREFLIGHT='1', BUILD_STUB_LOG=str(self.log))
        self.env['PATH'] = f'{self.bindir}:{self.env["PATH"]}'
        self.cache = self.repo / 'build/darling-src/build/CMakeCache.txt'

    def write_cache(self, *entries):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text('\n'.join(entries) + '\n')

    def run_build(self):
        return subprocess.run(
            [SH, str(self.repo / 'scripts' / 'build.sh')],
            capture_output=True, text=True, check=False, cwd=self.repo, env=self.env,
        )

    def test_warns_on_missing_filepath_entries(self):
        self.write_cache('LIBXML2_LIBRARY:FILEPATH=/nao/existe/libxml2.so')
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/nao/existe/libxml2.so', result.stderr)
        self.assertIn('no rule to make target', result.stderr)

    def test_silent_on_healthy_cache(self):
        existing = self.repo / 'exists.so'
        existing.write_text('')
        self.write_cache(f'GOOD_LIB:FILEPATH={existing}')
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')

    def test_silent_without_cache(self):
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')

    def test_all_zero_prepare_jobs_use_the_fallback(self):
        for value in ('0', '00', '000'):
            self.env['RCLOL_PREPARE_JOBS'] = value
            result = self.run_build()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('scripts/prepare.py --jobs 8', self.log.read_text())
            self.log.unlink()

    def test_configures_with_the_physical_path_through_a_symlink(self):
        # CMake records the logical $PWD in every target's flags, so a checkout
        # built once through a symlink (~/dev -> ~/Development) and once
        # without recompiled everything. Both routes must configure the same path.
        link = self.repo.parent / 'link'
        link.symlink_to(self.repo.name)
        self.env['PWD'] = str(link)
        result = subprocess.run(
            [SH, str(link / 'scripts' / 'build.sh')],
            capture_output=True, text=True, check=False, cwd=link, env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = set(Path(f'{self.log}.pwd').read_text().splitlines())
        self.assertEqual(seen, {os.path.realpath(self.repo)})

    def test_ignores_notfound_and_relative_paths(self):
        self.write_cache('X:FILEPATH=X-NOTFOUND', 'Y:FILEPATH=relativo/p')
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')

    def test_matches_variable_names_with_dash_and_dot(self):
        self.write_cache('Some-Dashed.var:FILEPATH=/nao/existe')
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/nao/existe', result.stderr)

    def test_never_modifies_cache_or_build_dir(self):
        self.write_cache('LIBXML2_LIBRARY:FILEPATH=/nao/existe/libxml2.so')
        before = self.cache.read_bytes()
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cache.read_bytes(), before)
        # The fake cmake was still invoked, for both configure and build.
        argv = self.log.read_text().splitlines()
        self.assertTrue(any(line.startswith('-S ') for line in argv), argv)
        self.assertTrue(any(line.startswith('--build ') for line in argv), argv)


if __name__ == '__main__':
    unittest.main()
