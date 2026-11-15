"""Compile the shipped shader translation cache against an inert translator."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/metal--deps--indium.patch'


def section(name):
    marker = f'diff --git a/{name} b/{name}\n'
    return PATCH.read_text().split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def postimages(name):
    # Extract each postimage hunk separately: never silently join a source gap.
    hunks = re.split(r'(?m)^@@ [^\n]*\n', section(name))[1:]
    return [''.join(line[1:] for line in hunk.splitlines(keepends=True)
                    if line.startswith((' ', '+'))) for hunk in hunks]


def translation_cache():
    start, end = '\tstruct LibraryCacheKey {', '\n}\n\nvoid Indium::PrivateDevice::initPipelineCache() {'
    matches = [text[text.index(start):text.index(end, text.index(start))]
               for text in postimages('src/indium/device.cpp') if start in text and end in text]
    if len(matches) != 1:
        raise AssertionError('expected one complete patched translation cache')
    return matches[0]


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class TranslationCacheChecks(unittest.TestCase):
    def test_other_translators_and_damaged_entries_are_translated_again(self):
        fixture = (ROOT / 'tests/fixtures/indium-translation-cache.cpp').read_text()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            source = path / 'check.cpp'
            source.write_text(fixture.replace('/* ACTUAL_PATCHED_CACHE */', translation_cache()))
            build = subprocess.run(['c++', '-std=c++17', '-O1', str(source), '-o', str(path / 'check')],
                                   capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            cache = path / 'cache'
            cache.mkdir()
            result = subprocess.run([str(path / 'check'), str(cache)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TranslatorIdentityChecks(unittest.TestCase):
    def test_the_identity_names_the_sources_and_the_loaded_llvm(self):
        added = ''.join(postimages('src/iridium/iridium.cpp'))
        self.assertIn('#error "IRIDIUM_SOURCE_HASH is not defined', added)
        self.assertIn('static_assert(sizeof(IRIDIUM_SOURCE_HASH) > 16', added)
        self.assertIn('std::string(IRIDIUM_SOURCE_HASH) + "-llvm" + llvmVersion', added)
        self.assertIn('if (llvmVersion.empty()) return {};', added)
        loaded = ''.join(postimages('src/iridium/dynamic-llvm.cpp'))
        self.assertIn('loadedVersionString = std::to_string(major) + "." + std::to_string(minor) + "." + '
                      'std::to_string(patch);', loaded)
        # The cache asks the translator that is loaded, and no entry name is a constant version any more.
        device = ''.join(postimages('src/indium/device.cpp'))
        self.assertIn('const std::string identity = Iridium::translatorIdentity();', device)
        self.assertNotIn('iridium-v3', device)

    def test_every_build_of_iridium_hashes_the_sources_it_compiles(self):
        cmake = ''.join(postimages('CMakeLists.txt'))
        self.assertIn('string(SHA256 IRIDIUM_SOURCE_HASH "${iridium_identity_input}")', cmake)
        self.assertIn('set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS', cmake)
        self.assertIn('"IRIDIUM_SOURCE_HASH=\\"${IRIDIUM_SOURCE_HASH}\\""', cmake)
        runtime = (ROOT / 'tools/build-riot-indium-runtime.py').read_text()
        self.assertIn('iridium_identity.replace_definition(', runtime)
        native = (ROOT / 'tools/build-riot-air-check.py').read_text()
        self.assertIn('iridium_identity.definition(indium)', native)

    def test_the_hash_follows_content_and_names_and_replaces_a_stale_definition(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('iridium_identity', ROOT / 'tools/iridium_identity.py')
        identity = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(identity)
        with tempfile.TemporaryDirectory() as folder:
            indium = Path(folder)
            with self.assertRaises(SystemExit):
                identity.source_hash(indium)
            for name in ('include/iridium/iridium.hpp', 'private-include/iridium/air.hpp',
                         'src/iridium/air.cpp', 'src/indium/device.cpp'):
                (indium / name).parent.mkdir(parents=True, exist_ok=True)
                (indium / name).write_text(name)
            first = identity.source_hash(indium)
            self.assertRegex(first, r'^[0-9a-f]{64}$')
            (indium / 'src/indium/device.cpp').write_text('not part of the translator')
            self.assertEqual(identity.source_hash(indium), first)
            (indium / 'private-include/iridium/air.hpp').write_text('changed')
            second = identity.source_hash(indium)
            self.assertNotEqual(second, first)
            (indium / 'src/iridium/air.cpp').rename(indium / 'src/iridium/air2.cpp')
            self.assertNotEqual(identity.source_hash(indium), second)
            stale = ['-DDARLING', '-DIRIDIUM_SOURCE_HASH="' + first + '"']
            self.assertEqual(identity.replace_definition(stale, indium),
                             ['-DDARLING', identity.definition(indium)])


if __name__ == '__main__':
    unittest.main()
