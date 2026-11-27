"""EVFILT_READ must read a socket's FIONREAD count as an int.

FIONREAD stores an int, but copyout passed the 64-bit `dst->data`, which still
held the data given at registration. Only its low half was overwritten, so the
reported byte count could be huge, and a zero count (end of file) went
unnoticed when the upper half was not zero. Backport of upstream
darling-libkqueue 38b08e2.
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


class FionreadIntTests(unittest.TestCase):
    def test_count_goes_through_an_int(self):
        text = new_side('src/linux/read.c')
        self.assertNotIn('ioctl(dst->ident, FIONREAD, &dst->data)', text)
        start = text.index('if (ioctl(dst->ident, FIONREAD, &avail) < 0) {')
        self.assertIn('int avail;', text[:start])
        self.assertRegex(text[start:], r'dst->data = avail;\s*if \(avail == 0\)\s*dst->flags \|= EV_EOF;')


if __name__ == '__main__':
    unittest.main()
