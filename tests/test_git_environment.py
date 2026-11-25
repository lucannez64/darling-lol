"""The Git fixture guard keeps inherited Git variables and config out of a test."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import git_environment  # noqa: E402

CONFIG_VARIABLES = ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')


def config_state():
    """The fixture config variables exactly as the caller has them now."""
    return {name: os.environ[name] for name in CONFIG_VARIABLES if name in os.environ}


class GitEnvironmentTests(unittest.TestCase):
    def test_isolate_hides_inherited_location_variables(self):
        decoy = {'GIT_DIR': '/decoy/.git', 'GIT_WORK_TREE': '/decoy'}
        with mock.patch.dict(os.environ, decoy):
            inner = unittest.TestCase()
            before = config_state()
            git_environment.isolate(inner)
            self.assertNotIn('GIT_DIR', os.environ)
            self.assertNotIn('GIT_WORK_TREE', os.environ)
            # The rest of the caller's environment survives the isolation.
            self.assertIn('PATH', os.environ)
            inner.doCleanups()
            self.assertEqual('/decoy/.git', os.environ['GIT_DIR'])
            # Cleanup restores the caller's state, inherited or absent.
            self.assertEqual(before, config_state())

    def test_isolate_uses_an_empty_fixture_config(self):
        inherited = {'GIT_CONFIG_GLOBAL': '/decoy/gitconfig',
                     'GIT_CONFIG_NOSYSTEM': '0'}
        with mock.patch.dict(os.environ, inherited):
            inner = unittest.TestCase()
            git_environment.isolate(inner)
            config = Path(os.environ['GIT_CONFIG_GLOBAL'])
            self.assertTrue(config.is_file())
            self.assertEqual('', config.read_text())
            self.assertEqual('1', os.environ['GIT_CONFIG_NOSYSTEM'])
            inner.doCleanups()
            self.assertEqual(inherited, config_state())

    def test_isolate_leaves_a_clean_environment_alone(self):
        saved = {name: value for name, value in os.environ.items()
                 if not name.startswith('GIT_')}
        with mock.patch.dict(os.environ, saved, clear=True):
            inner = unittest.TestCase()
            git_environment.isolate(inner)
            added = set(os.environ) - set(saved)
            self.assertEqual({'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM'}, added)
            inner.doCleanups()
            self.assertEqual(saved, dict(os.environ))

    @unittest.skipUnless(shutil.which('git'), 'Git required')
    def test_isolate_ignores_the_callers_global_config(self):
        # A signing requirement in the caller's config would abort the commit.
        with tempfile.TemporaryDirectory(prefix='git-config-test-') as tmp:
            poisoned = Path(tmp) / 'gitconfig'
            poisoned.write_text('[commit]\n\tgpgsign = true\n')
            with mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(poisoned)}):
                inner = unittest.TestCase()
                git_environment.isolate(inner)
                repo = Path(tmp) / 'repo'
                repo.mkdir()
                (repo / 'file').write_text('fixture\n')
                commands = (
                    ['init', '-q'],
                    ['add', 'file'],
                    ['-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                     'commit', '-q', '-m', 'fixture'],
                )
                for arguments in commands:
                    subprocess.run(['git', '-C', str(repo), *arguments],
                                   check=True, capture_output=True)
                inner.doCleanups()
