"""POSIX_SPAWN_CLOEXEC_DEFAULT must leave Darling's own descriptors alone.

The flag marked every open descriptor close-on-exec, including the child's
darlingserver RPC socket and process lifetime pipe. The lifetime pipe is passed
on to mldr across execve (on kernels without pidfds), so closing it made the
new image fail to start. Descriptors in the guard table, which are exactly
Darling's own, are now skipped. Backport of upstream darling-xnu 814c501.
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


class SpawnCloexecDefaultTests(unittest.TestCase):
    def test_guarded_descriptors_keep_their_flags(self):
        text = new_side(EMU + 'src/xnu_syscall/bsd/impl/process/posix_spawn.c')
        self.assertIn('#include <darling/emulation/common/guarded/table.h>', text)
        skip = text.index('if (guard_table_check(fd, guard_flag_prevent_close)) {')
        self.assertLess(skip, text.index('if (desc && desc->factp) {', skip))


if __name__ == '__main__':
    unittest.main()
