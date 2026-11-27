"""A mutex waiter resumed without being woken must leave the wait queue.

`dtape_mutex_unlock` takes the thread it wakes off the queue, but a thread
waiting in `dtape_mutex_lock` can also resume without being woken (its call
is resumed to handle a signal, the case `dtape_condvar_wait` already handled).
Its link stayed queued, and the next turn of the wait loop inserted it again,
corrupting the TAILQ. The waiter now removes its link if it is still queued.
Backport of upstream darlingserver 129ea07.
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


class MutexRequeueTests(unittest.TestCase):
    def setUp(self):
        text = new_side(LOCKS)
        # The first hunk is dtape_mutex_lock(); its signature is in the hunk header.
        start = text.index('dtape_mutex_assert(mutex, false);')
        self.body = text[start:text.index('\n};', start)]

    def test_queue_lock_is_taken_once_before_the_loop(self):
        self.assertLess(self.body.index('libsimple_lock_lock(&mutex->dtape_queue_lock);'),
                        self.body.index('while (true) {'))

    def test_waiter_unlinks_itself_after_resuming(self):
        after = self.body[self.body.index('dtape_hooks->thread_suspend(thread->context, NULL, NULL, &mutex->dtape_queue_lock);'):]
        self.assertRegex(
            after,
            r'libsimple_lock_lock\(&mutex->dtape_queue_lock\);\s*dtape_mutex_link_t\* link;\s*'
            r'TAILQ_FOREACH\(link, &mutex->dtape_queue_head, link\) \{\s*'
            r'if \(link == &thread->mutex_link\) \{\s*'
            r'TAILQ_REMOVE\(&mutex->dtape_queue_head, link, link\);\s*break;',
        )


if __name__ == '__main__':
    unittest.main()
