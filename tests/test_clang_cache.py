"""tools/clang-cache.py skips only compiles whose inputs are unchanged."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'tools/clang-cache.py'


@unittest.skipUnless(shutil.which('clang'), 'clang required')
class ClangCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / 'include').mkdir()
        self.header = self.root / 'include/value.h'
        self.header.write_text('#define VALUE 1\n')
        self.source = self.root / 'unit.c'
        self.source.write_text('#include "value.h"\nint value(void) { return VALUE; }\n')
        self.object = self.root / 'unit.o'
        # A logging wrapper stands in for the real compiler so every real run is counted.
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'compiles.log'
        wrapper = self.bin / 'clang'
        wrapper.write_text('#!/bin/sh\necho run >> "%s"\nexec "%s" "$@"\n' % (self.log, shutil.which('clang')))
        wrapper.chmod(0o755)

    def tearDown(self):
        self.folder.cleanup()

    def compile(self, *extra, **env):
        child = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        child.update(env)
        command = ['python3', str(CACHE), 'clang', '-I', 'include', *extra,
                   '-c', str(self.source), '-o', str(self.object)]
        subprocess.run(command, cwd=self.root, env=child, check=True)

    def runs(self):
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def test_unchanged_compile_is_skipped(self):
        self.compile()
        self.assertEqual(self.runs(), 1)
        self.assertTrue((self.root / 'unit.o.d').is_file())
        before = self.object.read_bytes()
        self.compile()
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.object.read_bytes(), before)

    def test_every_input_invalidates(self):
        self.compile()
        self.header.write_text('#define VALUE 2\n')
        self.compile()
        self.assertEqual(self.runs(), 2, 'header edit')
        self.source.write_text(self.source.read_text() + '\n')
        self.compile()
        self.assertEqual(self.runs(), 3, 'source edit')
        self.compile('-O2')
        self.assertEqual(self.runs(), 4, 'flag change')
        self.object.unlink()
        self.compile('-O2')
        self.assertEqual(self.runs(), 5, 'missing object')
        self.compile('-O2', CPATH=str(self.root))
        self.assertEqual(self.runs(), 6, 'include environment')
        self.compile('-O2', CPATH=str(self.root), RCLOL_CLANG_CACHE='0')
        self.assertEqual(self.runs(), 7, 'cache disabled')
        self.compile('-O2', CPATH=str(self.root))
        self.assertEqual(self.runs(), 7)

    def test_failed_compile_records_nothing(self):
        self.compile()
        self.source.write_text('int broken(void) { return }\n')
        child = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        result = subprocess.run(['python3', str(CACHE), 'clang', '-c', str(self.source), '-o', str(self.object)],
                                cwd=self.root, env=child, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'unit.o.clang-cache').exists())

    def test_links_and_other_commands_pass_through(self):
        self.compile()
        runs = self.runs()
        child = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        library = self.root / 'libunit.so'
        for _ in range(2):
            subprocess.run(['python3', str(CACHE), 'clang', '-shared', str(self.object), '-o', str(library)],
                           cwd=self.root, env=child, check=True)
        self.assertEqual(self.runs(), runs + 2)
        self.assertTrue(library.is_file())

    def test_install_script_puts_the_shims_first(self):
        script = (ROOT / 'tools/install-riot-gpu-runtime.sh').read_text()
        setup = script.index('clang_cache_bin=')
        self.assertLess(setup, script.index('build-m58f333-iosurface'))
        self.assertIn('tools/clang-cache.py', script)
        self.assertIn('PATH="$clang_cache_bin:$PATH"', script)
        self.assertIn('RCLOL_CLANG_CACHE_SHIMS', script)


if __name__ == '__main__':
    unittest.main()
