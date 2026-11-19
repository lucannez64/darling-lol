"""A microthread's mutex try-lock must hold the queue lock while it reads the owner.

`dtape_mutex_try_lock` used to read and set `dtape_owner` without taking
`dtape_queue_lock`, then released that queue lock anyway. A holder without an
active thread owns the mutex by holding the queue lock with the owner left at 0,
so the try-lock succeeded alongside it and dropped its lock: two threads edited
the same waitq (`waitq_lock_try` routes here, for example from
`waitq_pull_thread_locked`) and darlingserver crashed or lost a wakeup after a
long match (issue #256). Backport of upstream darlingserver 2b7a7fd.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darlingserver.patch'
LOCKS = 'duct-tape/src/locks.c'


def new_side(path):
    """Context and added lines of `path`'s section in the darlingserver patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class MicrothreadTryLockTests(unittest.TestCase):
    def setUp(self):
        text = new_side(LOCKS)
        start = text.index('dtape_mutex_assert(mutex, false);')
        self.body = text[start:text.index('\n};', start)]

    def test_queue_lock_is_taken_before_the_owner_is_read(self):
        take = self.body.index('if (!libsimple_lock_try_lock(&mutex->dtape_queue_lock)) {')
        self.assertLess(take, self.body.index('mutex->dtape_owner == 0'))
        # Contended queue lock: fail without touching the owner or unlocking.
        self.assertRegex(self.body[take:], r'^if \(!libsimple_lock_try_lock\(&mutex->dtape_queue_lock\)\) \{\s*return FALSE;\s*\}')

    def test_every_path_releases_the_queue_lock_it_took(self):
        owner = self.body.index('mutex->dtape_owner == 0')
        tail = self.body[owner:]
        self.assertEqual(tail.count('libsimple_lock_unlock(&mutex->dtape_queue_lock);'), 2)
        failed = tail.index('// lock not acquired')
        self.assertRegex(tail[failed:], r'libsimple_lock_unlock\(&mutex->dtape_queue_lock\);\s*return FALSE;\s*$')


if __name__ == '__main__':
    unittest.main()
