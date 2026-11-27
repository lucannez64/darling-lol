"""launchd must accept anonymous jobs for every Linux PID.

job_new_anonymous() rejected any PID of 100000 or more, XNU's PID_MAX. Under
Darling the guest PIDs are Linux PIDs, which go up to PID_MAX_LIMIT (4194304)
on 64-bit kernels, the default pid_max of current distributions. Once the host
handed out PIDs past 99999, launchd refused to track processes it had not
started. Backport of upstream darling 62d5c1b8.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darling.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the darling patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class LaunchdPidMaxTests(unittest.TestCase):
    def test_darling_limit_is_linux_pid_max_limit(self):
        text = new_side('src/launchd/src/core.c')
        guard = text.index('#ifndef DARLING\n\tif (anonpid >= 100000) {')
        darling = text[guard:]
        branch = darling[darling.index('#else'):darling.index('#endif')]
        self.assertRegex(branch, r'if \(anonpid >= 4194304\) \{')
        self.assertIsNone(re.search(r'anonpid >= 100000', branch))


if __name__ == '__main__':
    unittest.main()
