"""objc lock debugging must be opt-in, not follow DEBUG.

Darling's default build keeps DEBUG on (CMAKE_BUILD_TYPE stays empty because
Release breaks the objc4 debug macros). objc-os.h turned LOCKDEBUG on with
DEBUG, so every side-table spinlock taken by a retain or release recorded
itself in a lock-debug hash table, with a malloc per acquisition. LOCKDEBUG now
also needs -DDARLING_OBJC_LOCKDEBUG. Backport of upstream darling-objc4
efffe2c.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/objc4.patch'
OS_H = 'runtime/objc-os.h'


def new_side(path):
    """Context and added lines of `path`'s section in the objc4 patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class ObjcLockDebugOptInTests(unittest.TestCase):
    def test_lockdebug_needs_the_darling_opt_in(self):
        text = new_side(OS_H)
        self.assertRegex(
            text,
            r'#if DEBUG && defined\(DARLING_OBJC_LOCKDEBUG\)\n#\s+define LOCKDEBUG 1\n#else\n#\s+define LOCKDEBUG 0',
        )
        self.assertNotRegex(text, r'#if DEBUG\n#\s+define LOCKDEBUG 1')

    def test_no_patch_turns_the_opt_in_on(self):
        for patch in sorted((ROOT / 'patches').glob('*.patch')):
            added = [line for line in patch.read_text(errors='replace').splitlines()
                     if line.startswith('+') and not line.startswith('+++')]
            for line in added:
                code = line[1:].split('//', 1)[0]
                if code.lstrip().startswith(('*', '/*')):
                    continue
                self.assertFalse(
                    re.search(r'(-D|define\s+)DARLING_OBJC_LOCKDEBUG', code),
                    f'{patch.name} enables DARLING_OBJC_LOCKDEBUG: {line}',
                )


if __name__ == '__main__':
    unittest.main()
