"""Creating a thread must not read the status of every thread of the process.

To find the host thread ID of a new guest thread, darlingserver read
/proc/<pid>/task/<tid>/status for every thread of the process and kept going
after the match. Each creation cost time proportional to the thread count
(6 ms with 400 threads against 0.2 ms with none), all of it on the server's
single event loop, so a process that creates many threads stalled every other
guest call. Registered threads are skipped and the scan stops at the match.
Backport of upstream darlingserver 6aad2ad.
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


class ThreadCreationScanTests(unittest.TestCase):
    def setUp(self):
        text = new_side('src/thread.cpp')
        start = text.index('std::to_string(process->id()) + "/task")) {')
        self.scan = text[start:text.index("if we can't determine the thread id from procfs", start)]

    def test_registered_threads_are_skipped_before_reading_status(self):
        skip = self.scan.index('if (scanRegistry.lookupEntryByID(candidate).has_value()) {')
        self.assertLess(skip, self.scan.index('std::ifstream statusFile(entry.path() / "status");'))

    def test_scan_stops_at_the_match(self):
        self.assertIn('_tid = candidate;', self.scan)
        self.assertRegex(self.scan, r'if \(_tid != -1\) \{\s*break;\s*\}')


if __name__ == '__main__':
    unittest.main()
