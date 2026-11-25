"""The Git fixture guard keeps inherited GIT_* variables out of a test."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import git_environment  # noqa: E402


class GitEnvironmentTests(unittest.TestCase):
    def test_isolate_hides_inherited_location_variables(self):
        decoy = {'GIT_DIR': '/decoy/.git', 'GIT_WORK_TREE': '/decoy'}
        with mock.patch.dict(os.environ, decoy):
            inner = unittest.TestCase()
            git_environment.isolate(inner)
            self.assertNotIn('GIT_DIR', os.environ)
            self.assertNotIn('GIT_WORK_TREE', os.environ)
            # The rest of the caller's environment survives the isolation.
            self.assertIn('PATH', os.environ)
            inner.doCleanups()
            self.assertEqual('/decoy/.git', os.environ['GIT_DIR'])

    def test_isolate_leaves_a_clean_environment_alone(self):
        saved = {name: value for name, value in os.environ.items()
                 if not name.startswith('GIT_')}
        with mock.patch.dict(os.environ, saved, clear=True):
            inner = unittest.TestCase()
            git_environment.isolate(inner)
            self.assertEqual(saved, dict(os.environ))
            inner.doCleanups()
