"""Darling's libc++.1.dylib must export std::filesystem (issue #285).

Apple's libc++ exports the std::__1::__fs::filesystem functions, and client
libraries bind them: libVoicemodUSDK.dylib (voice skins) and the Krisp
noise-suppression SDK. The Riot API opens the Voicemod library with
dlopen(RTLD_NOW), so one missing symbol fails the whole load ("DynamicLibrary
load failed ... OS error 22"). Darling's libcxx CMakeLists never built
src/filesystem; patches/libcxx.patch adds it to the shared library, and
tools/build-riot-cxx-runtime.py recompiles those objects for the -O3 overlay.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PATCH = ROOT / 'patches/libcxx.patch'


def added_lines():
    return [line[1:].strip() for line in PATCH.read_text().splitlines()
            if line.startswith('+') and not line.startswith('+++')]


class LibcxxPatch(unittest.TestCase):
    def test_filesystem_sources_build_as_cxx17(self):
        lines = added_lines()
        self.assertIn('src/filesystem/operations.cpp', lines)
        self.assertIn('src/filesystem/directory_iterator.cpp', lines)
        self.assertIn('target_compile_options(cxx_fs_obj PRIVATE -std=c++17)', lines)

    def test_filesystem_objects_link_into_the_shared_library_only(self):
        lines = added_lines()
        self.assertIn('add_darling_library(cxx SHARED $<TARGET_OBJECTS:cxx_obj> $<TARGET_OBJECTS:cxx_fs_obj>)',
                      lines)
        # dyld links cxx_static from cxx_sources; it must not pick up filesystem.
        self.assertFalse(any('add_darling_static_library' in line for line in lines))

    def test_compiler_rt_keeps_muloti4(self):
        # libcompiler_rt already exports ___muloti4; a second copy in libc++ would clash.
        self.assertFalse(any('int128_builtins.cpp' in line for line in added_lines()))

    def test_manifest_pins_the_component(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c['path'] == 'src/external/libcxx')
        self.assertEqual(component['patch'], 'patches/libcxx.patch')
        self.assertEqual(component['repository'], 'https://github.com/darlinghq/darling-libcxx.git')
        self.assertEqual(component['modified_files'], PATCH.read_text().count('diff --git '))
        self.assertEqual(component['new_files'], [])


def load_builder():
    sys.path.insert(0, str(ROOT / 'tools'))
    with mock.patch('subprocess.check_output', return_value='/usr/lib/clang/99\n'):
        spec = importlib.util.spec_from_file_location('build_riot_cxx_runtime',
                                                      ROOT / 'tools/build-riot-cxx-runtime.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class OverlayBuilder(unittest.TestCase):
    def test_filesystem_objects_are_recompiled_with_their_own_flags(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'build/darling-src/build/src/external/libcxx'
            source = root / 'build/darling-src/src/external/libcxx'
            for object_dir, std in (('cxx_obj.dir', '-std=c++11'), ('cxx_fs_obj.dir', '-std=c++11 -std=c++17')):
                (folder / 'CMakeFiles' / object_dir).mkdir(parents=True)
                (folder / 'CMakeFiles' / object_dir / 'flags.make').write_text(
                    'CXX_DEFINES = -DX=1\nCXX_INCLUDES = -Iinclude\nCXX_FLAGS = -O3 %s\n' % std)
            (folder / 'CMakeFiles/cxx.dir').mkdir()
            (folder / 'CMakeFiles/cxx.dir/link.txt').write_text(
                'clang++ -dynamiclib -o libc++.1.dylib CMakeFiles/cxx_obj.dir/src/vector.cpp.o '
                'CMakeFiles/cxx_fs_obj.dir/src/filesystem/operations.cpp.o\n')
            for relative in ('src/vector.cpp', 'src/filesystem/operations.cpp'):
                (source / relative).parent.mkdir(parents=True, exist_ok=True)
                (source / relative).write_text('// %s\n' % relative)

            runs = []
            with mock.patch.object(builder, 'root', root), \
                    mock.patch.object(builder.riot_build_gate, 'wait_until_free'), \
                    mock.patch.object(builder.subprocess, 'run', side_effect=lambda args, **_: runs.append(args)):
                builder.build('libcxx', ('cxx_obj.dir', 'cxx_fs_obj.dir'), 'cxx.dir', 'riot-libc++.1.dylib')

        compiles = {args[args.index('-c') + 1]: args for args in runs if '-c' in args}
        operations = compiles[str(source / 'src/filesystem/operations.cpp')]
        vector = compiles[str(source / 'src/vector.cpp')]
        self.assertEqual(operations[-1], str(root / 'build/riot-opt-libcxx-src_filesystem_operations.cpp.o'))
        self.assertIn('-std=c++17', operations)
        self.assertNotIn('-std=c++17', vector)
        link = runs[-1]
        self.assertIn(str(root / 'build/riot-opt-libcxx-src_filesystem_operations.cpp.o'), link)
        self.assertIn(str(root / 'build/riot-opt-libcxx-src_vector.cpp.o'), link)
        self.assertEqual(link[link.index('-o') + 1], str(root / 'build/riot-libc++.1.dylib'))

    def test_main_builds_the_filesystem_objects(self):
        text = (ROOT / 'tools/build-riot-cxx-runtime.py').read_text()
        self.assertIn('build("libcxx", ("cxx_obj.dir", "cxx_fs_obj.dir"), "cxx.dir", "riot-libc++.1.dylib")', text)


if __name__ == '__main__':
    unittest.main()
