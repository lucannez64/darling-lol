"""Stop at a stub fetcher: verify installer routing without network or runtime use."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerRegions(unittest.TestCase):
    def test_aliases_and_platform_codes_reach_the_expected_cdn_region(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            (root / 'build/runtime/tools').mkdir(parents=True)
            shutil.copy(ROOT / 'scripts/installer.sh', root / 'scripts')
            (root / 'scripts/configure.py').write_text('')
            # installer.sh resolves the prefix through the rendered
            # runtime-paths.sh before it fetches anything.
            shutil.copy(ROOT / 'tools/runtime_paths.py', root / 'build/runtime/tools')
            (root / 'build/runtime/tools/runtime-paths.sh').write_text(
                (ROOT / 'tools/runtime-paths.sh').read_text()
                .replace('@DPREFIX@', str(root / 'prefix'))
                .replace('@GUEST_HOME@', '/Users/guest')
                .replace('@PROJECT_ROOT@', str(root)))
            (root / 'build/runtime/tools/fetch-installer.sh').write_text(
                'printf "%s" "$1" > "$TEST_REGION_LOG"\nexit 23\n')
            log = root / 'region'
            env = dict(os.environ, TEST_REGION_LOG=str(log))
            env.pop('DARLING_WORKSPACE', None)
            env.pop('DPREFIX', None)
            env.pop('RIOT_GUEST_HOME', None)
            for region, expected in [('lan', 'la1'), ('las', 'la2'), ('oce', 'oc1'),
                                     ('la1', 'la1'), ('la2', 'la2'), ('oc1', 'oc1'),
                                     ('euw', 'euw')]:
                with self.subTest(region=region):
                    log.unlink(missing_ok=True)
                    result = subprocess.run(['sh', str(root / 'scripts/installer.sh'), region],
                                            env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 23, result.stderr)
                    self.assertEqual(log.read_text(), expected)
                    # The stopped run gave its install lock back.
                    self.assertFalse((root / 'build/installer.lock').exists())
