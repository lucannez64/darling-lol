"""Keep a test's Git fixture bound to its own temporary repository.

A Git hook exports location variables such as GIT_DIR and GIT_WORK_TREE. The
fixtures below run `git init`, `git add` and `git commit` inside a temporary
directory, and without this guard those commands would target the repository
that started the hook.
"""
import os
from unittest import mock


def isolate(testcase):
    """Drop the inherited GIT_* variables until the test cleans up."""
    environment = mock.patch.dict(
        os.environ,
        {name: value for name, value in os.environ.items()
         if not name.startswith('GIT_')},
        clear=True)
    environment.start()
    testcase.addCleanup(environment.stop)
