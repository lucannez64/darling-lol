"""A guest signal handler must run with the mask it asked for, and see the fault address.

sig_masks holds the Darwin-numbered mask given to sigaction(), but sigexc
applied it as a Linux sigset, so the wrong signals were blocked while the
handler ran (SIGUSR2, for one, was not). On x86 the exception state also
reported the instruction address as the fault address; it now carries the
faulting access's address from siginfo, as on macOS. Backport of upstream
darling-xnu ea2abb4.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/xnu.patch'
EMU = 'darling/src/libsystem_kernel/emulation/'


def new_side(path):
    """Context and added lines of `path`'s section in the xnu patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in the patch, found {len(matches)}')
    return '\n'.join(line[1:] for line in matches[0].splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


class SignalMaskTests(unittest.TestCase):
    def test_handler_mask_is_converted_from_darwin_numbering(self):
        text = new_side(EMU + 'src/linux_premigration/signal/sigexc.c')
        self.assertNotIn('linux_sigset_t set = sig_masks[linux_signum];', text)
        self.assertRegex(text, r'sigset_t bsd_mask = sig_masks\[linux_signum\];\s*linux_sigset_t set;\s*sigset_bsd_to_linux\(&bsd_mask, &set\);')

    def test_x86_fault_address_comes_from_siginfo(self):
        text = new_side(EMU + 'src/conversion/signal/sigaction.c')
        self.assertIn('bc.uc_mcontext->es.faultvaddr = (unsigned long)info->si_addr;', text)


if __name__ == '__main__':
    unittest.main()
