"""Source-level checks for the sandbox ABI that Electron 43 / Chromium 150 uses.

Darling cannot run the guest in CI, so these checks read the patches instead:
the compiled-profile layout and the extension token signatures are the parts
Chromium depends on, and they are easy to drift back to the old placeholders.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darling.patch'


def added(path):
    """Lines the patch adds to one file, with the '+' marker removed."""
    text = PATCH.read_text()
    for marker in (f'diff --git i/{path} w/{path}\n', f'diff --git a/{path} b/{path}\n'):
        if marker not in text:
            continue
        section = text.split(marker, 1)[1].split('\ndiff --git ', 1)[0]
        return '\n'.join(line[1:] for line in section.splitlines()
                         if line.startswith('+') and not line.startswith('+++'))
    return ''


class SandboxABITests(unittest.TestCase):
    def test_compiled_profile_has_apple_layout(self):
        source = added('src/libsandbox/src/sandbox.c')
        struct = re.search(r'struct darling_sandbox_profile \{(.*?)\};', source, re.S)
        self.assertIsNotNone(struct, 'compiled profile struct is missing')
        fields = re.findall(r'\b\w+\s*\*?\s*(\w+);', struct.group(1))
        self.assertEqual(['builtin', 'data', 'size'], fields,
                         'Chromium reads {builtin, data, size} in that order')

    def test_compile_entry_points_take_params_and_error(self):
        header = added('src/libsandbox/include/sandbox/sandbox.h')
        for name in ('sandbox_compile_entitlements', 'sandbox_compile_file',
                     'sandbox_compile_named', 'sandbox_compile_string'):
            self.assertRegex(
                header,
                r'void\*\s*%s\([^)]*void\* params,\s*char\*\* error\)' % name,
                '%s must keep Apple\'s params/error parameters' % name)
        self.assertIn('int sandbox_apply(void* profile);', header)

    def test_extension_tokens_match_sandbox_spi(self):
        header = added('src/sandbox/sandbox.h')
        for name, second in (('sandbox_extension_issue_file', 'const char *path'),
                             ('sandbox_extension_issue_mach', 'const char *name')):
            declaration = re.search(r'([^\n]*%s\([^;]*\);)' % name, header)
            self.assertIsNotNone(declaration, name + ' is not declared')
            signature = declaration.group(1)
            self.assertTrue(signature.startswith('char *'), signature)
            self.assertIn('const char *extension_class', signature)
            self.assertIn(second, signature)
            self.assertIn('uint32_t flags', signature)
        self.assertRegex(header, r'int64_t\s+sandbox_extension_consume\(const char \*token\);')
        self.assertRegex(header, r'int\s+sandbox_extension_release\(int64_t handle\);')

    def test_extension_stubs_return_tokens_and_accept_them(self):
        source = added('src/libsandbox/src/sandbox.c')
        self.assertIn('return strdup("darling-lol-stub");', source)
        self.assertRegex(source, r'int64_t\s+sandbox_extension_consume\(const char \*token\)')
        self.assertIn('return token != NULL;', source)
        self.assertNotIn('sandbox_extension_next_token',
                         'integer token counters are not Apple ABI')


if __name__ == '__main__':
    unittest.main()
