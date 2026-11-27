"""A launchd subjob must inherit the parent job's stdout path.

job_new_subjob() copied j->stdinpath into nj->stdoutpath, so a subjob wrote
its output to the parent's input file, and crashed in strdup(NULL) when the
parent had a stdout path but no stdin path. Backport of upstream darling
3ec1dc01.
"""
from pathlib import Path
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


class LaunchdSubjobStdioTests(unittest.TestCase):
    def test_stdout_path_is_copied_from_stdout(self):
        text = new_side('src/launchd/src/core.c')
        self.assertRegex(text, r'if \(j->stdoutpath\) \{\s*nj->stdoutpath = strdup\(j->stdoutpath\);')
        self.assertNotIn('nj->stdoutpath = strdup(j->stdinpath);', text)


if __name__ == '__main__':
    unittest.main()
