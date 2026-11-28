"""EVFILT_TIMER copyout must never block on its timerfd with the kqueue locked.

When two threads waiting on one kqueue woke for the same timer expiration, the
second read the timerfd after the first had reset it and slept until the next
expiration with the kqueue lock held: a whole period for a repeating timer,
forever for a one-shot one. The timerfd is now nonblocking, and a copyout that
finds nothing expired drops the event. Port of upstream darling-libkqueue
b0c901f.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/libkqueue.patch'
TIMER_C = 'src/linux/timer.c'


def new_side(path):
    """Context and added lines of `path`'s section in the libkqueue patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class TimerNonblockingTests(unittest.TestCase):
    def setUp(self):
        self.text = new_side(TIMER_C)

    def test_timerfd_is_created_nonblocking(self):
        self.assertIn('tfd = timerfd_create(LINUX_CLOCK_MONOTONIC, TFD_NONBLOCK);', self.text)
        self.assertNotIn('timerfd_create(LINUX_CLOCK_MONOTONIC, 0);', self.text)

    def test_copyout_drops_an_expiration_another_thread_consumed(self):
        start = self.text.index('n = read(src->data.pfd, &expired, sizeof(expired));')
        rest = self.text[start:]
        self.assertRegex(rest, r'\} while \(n < 0 && errno == EINTR\);\s*if \(n < 0 && errno == EAGAIN\) \{')
        drop = rest.index('dst->filter = EVFILT_DROP;')
        self.assertLess(drop, rest.index('if (n != sizeof(expired)) {'))


if __name__ == '__main__':
    unittest.main()
