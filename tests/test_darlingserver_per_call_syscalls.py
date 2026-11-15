"""darlingserver must not pay fixed syscalls and locks on every call.

The main loop used to read the wake-up eventfd and re-arm it in epoll on every
pass, whether or not it had fired, and every `current_thread()` lookup took the
thread's read-write lock to learn that it is not impersonating anyone. With the
League client decoding video that is tens of thousands of passes a second.

The loop now relies on how a one-shot epoll registration behaves, so the second
half of this file checks that behaviour against the running kernel.
"""
import os
import re
import select
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def patched(path):
    """The hunks of `path` as the patch leaves them (context and added lines)."""
    sections = (ROOT / 'patches/darlingserver.patch').read_text().split('diff --git ')
    section = next(s for s in sections if s.startswith('a/' + path + ' b/'))
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class MainLoopTests(unittest.TestCase):
    def setUp(self):
        self.loop = patched('src/server.cpp')

    def test_eventfd_is_read_only_after_it_fired(self):
        self.assertRegex(self.loop, r'if \(wakeupFired\) \{\s*wakeupFired = false;\s*'
                                    r'eventfd_t value;\s*eventfd_read\(_wakeupFD, &value\);')
        self.assertEqual(self.loop.count('eventfd_read(_wakeupFD'), 1)

    def test_epoll_registration_changes_only_when_the_wanted_state_does(self):
        self.assertRegex(self.loop, r'if \(wantWakeup != wakeupArmed\) \{\s*'
                                    r'if \(epoll_ctl\(_epollFD, EPOLL_CTL_MOD, _wakeupFD, &settings\) < 0\)')
        self.assertIn('wakeupArmed = wantWakeup;', self.loop)

    def test_a_fired_wakeup_is_recorded_as_disarmed(self):
        # One-shot: the kernel disarms it when it fires, so the loop must re-arm it.
        self.assertRegex(self.loop, r'event->data\.ptr == &_wakeupFD\) \{[^}]*'
                                    r'wakeupArmed = false;\s*wakeupFired = true;')

    def test_the_wakeup_stays_wanted_while_replies_are_queued(self):
        self.assertIn('bool wantWakeup = _canWrite || !_outbox.empty();', self.loop)


class ImpersonationLookupTests(unittest.TestCase):
    def test_lookup_skips_the_lock_when_not_impersonating(self):
        body = patched('src/thread.cpp')
        flag = body.index('if (!_isImpersonating.load(std::memory_order_acquire))')
        self.assertLess(flag, body.index('std::shared_lock lock(_rwlock);\n\treturn _impersonating;'))

    def test_flag_is_written_with_the_pointer_under_the_lock(self):
        self.assertRegex(patched('src/thread.cpp'),
                         r'std::unique_lock lock\(_rwlock\);\s*oldThread = _impersonating;\s*'
                         r'_impersonating = thread;\s*_isImpersonating\.store\(!!thread,')


@unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'eventfd'), 'needs Linux eventfd')
class OneShotWakeupKernelTests(unittest.TestCase):
    """The kernel behaviour the loop depends on."""

    def setUp(self):
        self.wakeup = os.eventfd(0, os.EFD_NONBLOCK)
        self.other = os.eventfd(0, os.EFD_NONBLOCK)
        self.epoll = select.epoll()
        self.epoll.register(self.wakeup, select.EPOLLIN | select.EPOLLONESHOT)
        self.epoll.register(self.other, select.EPOLLIN)

    def tearDown(self):
        self.epoll.close()
        os.close(self.wakeup)
        os.close(self.other)

    def fired(self):
        return {fd for fd, _ in self.epoll.poll(0)}

    def test_stays_armed_across_passes_woken_by_something_else(self):
        for _ in range(3):
            os.eventfd_write(self.other, 1)
            self.assertEqual(self.fired(), {self.other})
            os.eventfd_read(self.other)
        os.eventfd_write(self.wakeup, 1)
        self.assertEqual(self.fired(), {self.wakeup})

    def test_fires_once_then_needs_rearming_and_reports_an_unread_count(self):
        os.eventfd_write(self.wakeup, 1)
        self.assertEqual(self.fired(), {self.wakeup})
        os.eventfd_write(self.wakeup, 1)
        self.assertEqual(self.fired(), set(), 'disarmed after firing')
        # Re-arming with a count still pending wakes at once: no write is lost.
        self.epoll.modify(self.wakeup, select.EPOLLIN | select.EPOLLONESHOT)
        self.assertEqual(self.fired(), {self.wakeup})

    def test_skipped_read_cannot_lose_a_write(self):
        # A producer writes while the loop is busy and the loop does not read:
        # the registration is still armed, so the next wait returns immediately.
        os.eventfd_write(self.wakeup, 1)
        os.eventfd_write(self.wakeup, 1)
        self.assertEqual(self.fired(), {self.wakeup})
        self.assertEqual(os.eventfd_read(self.wakeup), 2)


if __name__ == '__main__':
    unittest.main()
