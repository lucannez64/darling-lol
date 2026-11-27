"""A successful EV_RECEIPT change must not fail for lack of output space.

When the event list was full or absent, kevent() returned -1 for a change that
had succeeded, with whatever errno was left over, and stopped applying the
remaining changes. A successful receipt needs output space only to be
reported, so the remaining changes are now applied; real registration errors
still fail, with their own errno. Backport of upstream darling-libkqueue
3813b45.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/libkqueue.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the libkqueue patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class ReceiptWithoutOutputTests(unittest.TestCase):
    def test_only_failed_changes_return_an_error(self):
        text = new_side('src/common/kevent.c')
        self.assertRegex(
            text,
            r'nret\+\+;\s*\} else if \(status != 0\) \{\s*errno = status;\s*return \(-1\);\s*\}',
        )


if __name__ == '__main__':
    unittest.main()
