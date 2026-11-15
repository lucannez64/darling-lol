"""darlingserver's microthread switch must not make signal-mask syscalls.

glibc's getcontext() and setcontext() each call rt_sigprocmask, and every call
from a guest goes through two of each. darlingserver never changes a signal
mask, so the patch switches contexts with its own copies that skip it.

A wrong switch breaks every guest call, so the second half of this file builds
the assembly out of the patch and runs it through the patterns the server uses.
"""
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / 'tests/fixtures/darlingserver_context_harness.c'


def patch_sections():
    return (ROOT / 'patches/darlingserver.patch').read_text().split('diff --git ')


def patched(path):
    """The hunks of `path` as the patch leaves them (context and added lines)."""
    section = next(s for s in patch_sections() if s.startswith('a/' + path + ' b/'))
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


def context_assembly():
    body = patched('src/thread.cpp')
    match = re.search(r'// DSERVER-FAST-CONTEXT-BEGIN\n__asm__\(R"asm\(\n([\s\S]*?)\)asm"\);\n'
                      r'// DSERVER-FAST-CONTEXT-END', body)
    if not match:
        raise AssertionError('the context switch assembly is not in the src/thread.cpp patch')
    return match.group(1)


class ContextSwitchSourceTests(unittest.TestCase):
    def setUp(self):
        self.thread = patched('src/thread.cpp')

    def test_no_syscall_in_the_switch(self):
        assembly = context_assembly()
        self.assertNotIn('syscall', assembly)
        for symbol in ('dserver_getcontext', 'dserver_setcontext'):
            self.assertIn(symbol + ':', assembly)

    def test_every_switch_in_the_patch_goes_through_the_wrappers(self):
        after = self.thread.split('// DSERVER-FAST-CONTEXT-END', 1)[1]
        after = after.split('#endif', 1)[1]  # past the wrapper definitions
        self.assertEqual(re.findall(r'(?<![\w.])(?:get|set)context\(&', after), [])
        self.assertGreaterEqual(len(re.findall(r'dsGetContext\(&', after)), 5)
        self.assertGreaterEqual(len(re.findall(r'dsSetContext\(&', after)), 10)

    def test_getcontext_is_declared_returns_twice(self):
        # Without it the compiler may keep values in registers across the second return.
        self.assertRegex(self.thread, r'int dserver_getcontext\(ucontext_t\* context\) '
                                      r'__attribute__\(\(returns_twice\)\);')

    def test_layout_is_checked_at_compile_time(self):
        for field, offset in (('gregs[REG_RSP]', 160), ('gregs[REG_RIP]', 168),
                              ('fpregs', 224), ('__fpregs_mem', 424)):
            suffix = field if field.startswith('__') else 'uc_mcontext.' + field
            self.assertIn(f'static_assert(offsetof(ucontext_t, {suffix}) == {offset},', self.thread)

    def test_glibc_switch_can_be_selected_again(self):
        self.assertIn('getenv("DSERVER_GLIBC_CONTEXT") != nullptr', self.thread)
        self.assertRegex(self.thread, r'#else\n#define dsGetContext\(context\) getcontext\(context\)\n'
                                      r'#define dsSetContext\(context\) setcontext\(context\)')

    def test_the_patch_adds_no_signal_mask_change(self):
        # Upstream darlingserver has none either; if the patch adds one, skipping the mask is no longer free.
        added = '\n'.join(line for section in patch_sections()
                          for line in section.splitlines() if line.startswith('+'))
        self.assertEqual(re.findall(r'\b(?:pthread_sigmask|sigprocmask)\s*\(', added), [])


@unittest.skipUnless(platform.machine() == 'x86_64' and platform.system() == 'Linux'
                     and platform.libc_ver()[0] == 'glibc' and shutil.which('cc'),
                     'needs cc on x86-64 glibc Linux')
class ContextSwitchRunTests(unittest.TestCase):
    def build(self, directory, *flags):
        assembly = Path(directory) / 'context.S'
        assembly.write_text(context_assembly() + '\n\t.section .note.GNU-stack,"",@progbits\n')
        binary = Path(directory) / ('harness' + ''.join(flags))
        subprocess.run(['cc', *flags, '-pthread', '-o', str(binary), str(HARNESS), str(assembly), '-lm'],
                       check=True, capture_output=True, text=True)
        return binary

    def test_switch_patterns_at_each_optimisation_level(self):
        with tempfile.TemporaryDirectory() as directory:
            for flag in ('-O0', '-O2', '-O3'):
                with self.subTest(flag=flag):
                    result = subprocess.run([str(self.build(directory, flag))],
                                            capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(result.stdout.startswith('OK '), result.stdout)

    @unittest.skipUnless(shutil.which('strace'), 'needs strace')
    def test_no_signal_mask_syscalls_while_switching(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self.build(directory, '-O2')
            result = subprocess.run(['strace', '-f', '-c', '-e', 'trace=rt_sigprocmask', str(binary), '20000'],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = re.search(r'^\s*[\d.]+\s+[\d.]+\s+\d+\s+(\d+)\s+(?:\d+\s+)?rt_sigprocmask$',
                              result.stderr, re.M)
            # 20000 calls with glibc's switch would be 80000; thread start and the
            # harness's own pthread_sigmask checks account for a handful.
            self.assertLess(int(calls.group(1)) if calls else 0, 50, result.stderr)


if __name__ == '__main__':
    unittest.main()
