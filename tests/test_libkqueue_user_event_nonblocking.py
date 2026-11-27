"""EVFILT_USER copyout must never block on its eventfd with the kqueue locked.

Copyout consumes a triggered user event by lowering (reading) its eventfd; our
Chromium change does that for every returned user knote. The eventfd was
blocking, so when two threads waiting on the same kqueue woke for one trigger,
the second read the already lowered eventfd and slept there holding the kqueue
lock. Triggering the event again needs that lock: the kqueue deadlocked. The
eventfd is now nonblocking, and a copyout that finds it already lowered drops
the event. Port of upstream darling-libkqueue 9f762df.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/libkqueue.patch'
USER_C = 'src/linux/user.c'


def new_side(path):
    """Context and added lines of `path`'s section in the libkqueue patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class UserEventNonblockingTests(unittest.TestCase):
    def setUp(self):
        self.text = new_side(USER_C)

    def test_eventfd_is_created_nonblocking(self):
        self.assertIn('evfd = eventfd(0, EFD_NONBLOCK);', self.text)
        self.assertNotIn('evfd = eventfd(0, 0);', self.text)

    def test_lower_reports_an_already_lowered_eventfd(self):
        start = self.text.index('eventfd_lower(int evfd)')
        body = self.text[start:self.text.index('\n}', start)]
        self.assertRegex(body, r'if \(errno == EAGAIN\)\s*return \(0\);')
        self.assertIn('while (n < 0 && errno == EINTR);', body)
        self.assertIn('return (1);', body)

    def test_copyout_drops_an_event_another_thread_consumed(self):
        start = self.text.index('int rv = eventfd_lower(src->kdata.kn_eventfd);')
        rest = self.text[start:]
        drop = rest.index('dst->filter = EVFILT_DROP;')
        self.assertLess(rest.index('if (rv == 0) {'), drop)
        self.assertLess(drop, rest.index('src->kev.fflags &= ~NOTE_TRIGGER;'))


if __name__ == '__main__':
    unittest.main()
