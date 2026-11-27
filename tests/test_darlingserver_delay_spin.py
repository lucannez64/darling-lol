"""Short kernel delays must spin, as XNU does, instead of blocking.

`ml_delay_should_spin` always said no, so `delay()` blocked the microthread.
XNU spins for delays under 10 microseconds. Its callers back off from a
contended lock that way while holding state that blocking would break:
`clear_wait_internal` can run on the very thread whose wait it is clearing,
and blocking there replaced that thread's wait. Backport of upstream
darlingserver e20b832.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darlingserver.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the darlingserver patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class DelaySpinTests(unittest.TestCase):
    def test_delays_under_ten_microseconds_spin(self):
        text = new_side('duct-tape/src/stubs.c')
        start = text.index('boolean_t ml_delay_should_spin(uint64_t interval) {')
        body = text[start:text.index('\n};', start)]
        self.assertNotIn('return FALSE;', body)
        self.assertIn('nanoseconds_to_absolutetime(10 * NSEC_PER_USEC, &threshold);', body)
        self.assertIn('return interval < threshold;', body)


if __name__ == '__main__':
    unittest.main()
