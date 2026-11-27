"""A failed fork() or vfork() must return -1 and set errno.

__darling_bsd_syscall reports a failure as -errno, but the fork and vfork
stubs tested the result with an unsigned "not below 0" compare, which every
value passes. A failed fork therefore returned to the caller as a successful
parent with a negative "pid". The stubs now treat -4095..-1 as an error and
negate it into errno, as the other syscall stubs do. Backport of upstream
darling-xnu c4d6e1f.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/xnu.patch'


def new_side(path):
    """Context and added lines of `path`'s section in the xnu patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class ForkFailureTests(unittest.TestCase):
    def test_fork_checks_for_negative_errno(self):
        text = new_side('gen/bsdsyscalls/___fork.S')
        self.assertRegex(text, r'cmpl\t\$-4095, %eax\n\tjb\tL1\n\tnegl\t%eax')
        self.assertRegex(text, r'cmpq\t\$-4095, %rax\n\tjb\tL1\n\tnegq\t%rax')
        self.assertNotIn('jnb L1', text)

    def test_vfork_checks_for_negative_errno(self):
        text = new_side('gen/bsdsyscalls/___vfork.S')
        self.assertRegex(text, r'cmpq\t\$-4095, %rax\n\tjb\tL1\n\tnegq\t%rax')
        self.assertNotIn('jnb L1', text)


if __name__ == '__main__':
    unittest.main()
