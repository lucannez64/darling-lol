"""EVFILT_WRITE must report the free space of the write buffer.

Copyout stored ioctl(TIOCOUTQ) in `dst->data`: the bytes still queued, which
is 0 for an idle connection and nothing at all for a pipe, where the ioctl
fails; the int also landed in a 64-bit field. kevent(2) documents the value as
the space left in the write buffer, so a writable descriptor must report a
positive amount: the send buffer size less the queued bytes for a socket, the
pipe capacity less the unread bytes for a pipe. Backport of upstream
darling-libkqueue c2534d2.
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


class WriteSpaceTests(unittest.TestCase):
    def setUp(self):
        self.text = new_side('src/linux/write.c')

    def test_copyout_reports_the_space_left(self):
        self.assertIn('dst->data = write_space_avail(dst->ident);', self.text)
        self.assertNotIn('ioctl(dst->ident, TIOCOUTQ, &dst->data)', self.text)

    def test_space_is_computed_per_descriptor_kind(self):
        start = self.text.index('write_space_avail(int fd)\n{')
        body = self.text[start:self.text.index('\n}', start)]
        self.assertIn('return (queued < PIPE_BUFFER_SIZE) ? (PIPE_BUFFER_SIZE - queued) : 0;', body)
        self.assertIn('getsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, &sl)', body)
        self.assertIn('return (sndbuf > queued) ? (sndbuf - queued) : 0;', body)


if __name__ == '__main__':
    unittest.main()
