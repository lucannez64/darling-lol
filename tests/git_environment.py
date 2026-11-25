"""Keep a test's Git fixture bound to its own temporary repository and config.

A Git hook exports location variables such as GIT_DIR and GIT_WORK_TREE, and
the caller's global or system Git config can require commit signing or other
settings. The fixtures that use this guard run `git init`, `git add` and
`git commit` inside a temporary directory; without it those commands would
target the repository that started the hook and read the caller's config.
"""
import os
import tempfile
from pathlib import Path
from unittest import mock


def isolate(testcase):
    """Use an empty Git config and no inherited Git variables until cleanup."""
    folder = tempfile.TemporaryDirectory(prefix='git-config-')
    testcase.addCleanup(folder.cleanup)
    config = Path(folder.name) / 'gitconfig'
    config.touch()
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith('GIT_')}
    environment['GIT_CONFIG_GLOBAL'] = str(config)
    environment['GIT_CONFIG_NOSYSTEM'] = '1'
    patched = mock.patch.dict(os.environ, environment, clear=True)
    patched.start()
    testcase.addCleanup(patched.stop)
