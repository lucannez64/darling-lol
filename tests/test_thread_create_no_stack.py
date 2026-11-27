"""A thread whose stack cannot be allocated must not start, and must report EAGAIN.

__darling_thread_create went on with a NULL pthread structure when
dthread_structure_allocate failed (no stack to run the thread on), and
bsdthread_create returned the structure pointer as is, so a NULL result told
libpthread the thread had started. mldr now returns NULL without starting a
thread, and bsdthread_create turns that into EAGAIN, the error pthread_create()
documents for a resource shortage. Backport of upstream darling d75da881 and
darling-xnu b2eff8b.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def new_side(patch, path):
    """Context and added lines of `path`'s section in `patch`."""
    sections = (ROOT / 'patches' / patch).read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in {patch}, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class ThreadCreateWithoutStackTests(unittest.TestCase):
    def test_mldr_does_not_start_a_thread_without_a_stack(self):
        text = new_side('darling.patch', 'src/startup/mldr/elfcalls/threads.c')
        alloc = text.index('pth = dthread_structure_allocate(stack_size, DEFAULT_DTHREAD_GUARD_SIZE, (void**)&args.stack_addr);')
        self.assertRegex(text[alloc:], r'^[^\n]*\n\s*if \(pth == NULL\) \{[^}]*pthread_attr_destroy\(&attr\);\s*return NULL;')

    def test_bsdthread_create_reports_eagain(self):
        text = new_side('xnu.patch', 'darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/bsdthread/bsdthread_create.c')
        self.assertIn('void* created = __darling_thread_create(', text)
        self.assertRegex(text, r'if \(created == NULL\)\s*return -EAGAIN;')


if __name__ == '__main__':
    unittest.main()
