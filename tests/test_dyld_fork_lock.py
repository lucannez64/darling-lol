"""The global dyld lock must stay usable in the child of a fork.

libdyld serializes dlopen(), dlsym(), dladdr() and the other dyld APIs with
one recursive mutex. fork() calls _dyld_atfork_prepare/_parent/_fork_child so
dyld can keep its locks consistent, but with dyld2 those hooks did nothing:
a fork that happened while another thread was inside a dyld API left the child
with the mutex held by a thread that does not exist there, and the child's
first dyld call waited forever. The hooks now hold the lock across fork and
give the child a fresh mutex at the forking thread's depth. Backport of
upstream darling-dyld 6c8ddff.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/dyld.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the dyld patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class DyldForkLockTests(unittest.TestCase):
    def test_fork_hooks_hold_and_reset_the_lock(self):
        text = new_side('src/dyldAPIsInLibSystem.cpp')
        prepare = text.index('return dyld3::_dyld_atfork_prepare();')
        parent = text.index('return dyld3::_dyld_atfork_parent();')
        child = text.index('return dyld3::_dyld_fork_child();')
        self.assertIn('dyldGlobalLockAcquire();', text[prepare:parent])
        self.assertIn('dyldGlobalLockRelease();', text[parent:child])
        self.assertIn('dyldGlobalLockResetInForkChild();', text[child:])

    def test_child_reset_restores_the_forking_threads_depth(self):
        text = new_side('src/dyldLock.cpp')
        start = text.index('void dyldGlobalLockResetInForkChild()')
        body = text[start:text.index('\n}', start)]
        self.assertIn('int depth = _dyld_global_lock_held - 1;', body)
        self.assertIn('sGlobalMutex = freshMutex;', body)
        self.assertRegex(body, r'while \(depth-- > 0\)\s*dyldGlobalLockAcquire\(\);')
        self.assertIn('extern void dyldGlobalLockResetInForkChild()', new_side('src/dyldLock.h'))


if __name__ == '__main__':
    unittest.main()
