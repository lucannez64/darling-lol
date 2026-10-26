"""Offline contracts for the frameworks required by the 2026 Riot Client build."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

DARLING_FRAMEWORKS = {
    'src/frameworks/QuickLookUI/CMakeLists.txt': ['QLPreviewPanel.m', 'QLPreviewView.m'],
    'src/frameworks/Accessibility/CMakeLists.txt': ['ApplicationServices'],
    'src/frameworks/MediaPlayer/CMakeLists.txt': ['MPNowPlayingInfoCenter', 'MPRemoteCommandCenter'],
    'src/frameworks/CoreHaptics/CMakeLists.txt': ['CHHapticPattern'],
    'src/frameworks/CoreML/CMakeLists.txt': ['MLModel'],
}

FOUNDATION_NEW = 'src/NSConstantIntegerNumber.m'
COREFOUNDATION_NEW = 'NSConstantObjects.m'


def patch_section(patch, path):
    marker = f'diff --git a/{path} b/{path}\n'
    text = (ROOT / patch).read_text()
    if marker not in text:
        return None
    return text.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added_lines(section):
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class RiotClientFrameworkTests(unittest.TestCase):
    def test_new_framework_sources_are_present_and_manifested(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c['patch'] == 'patches/darling.patch')
        new_files = set(component['new_files'])

        for path in DARLING_FRAMEWORKS:
            self.assertIn(path, new_files)
            section = patch_section('patches/darling.patch', path)
            self.assertIsNotNone(section, path)
            self.assertIn('new file mode', section)

    def test_quartz_reexports_quicklookui(self):
        section = patch_section('patches/darling.patch', 'src/frameworks/Quartz/CMakeLists.txt')
        self.assertIsNotNone(section)
        self.assertIn('reexport(Quartz QuickLookUI', added_lines(section))
        self.assertNotIn('QuickLookUI/src/QLPreviewPanel.m', added_lines(section))

    def test_quicklookui_owns_the_preview_classes(self):
        section = patch_section('patches/darling.patch', 'src/frameworks/QuickLookUI/CMakeLists.txt')
        self.assertIsNotNone(section)
        source = added_lines(section)
        for name in DARLING_FRAMEWORKS['src/frameworks/QuickLookUI/CMakeLists.txt']:
            self.assertIn(name, source)

    def test_accessibility_reexports_application_services(self):
        section = patch_section('patches/darling.patch', 'src/frameworks/Accessibility/CMakeLists.txt')
        self.assertIsNotNone(section)
        self.assertIn('reexport(AccessibilityFramework ApplicationServices', added_lines(section))

    def test_mediaplayer_and_haptics_expose_required_symbols(self):
        for path, symbols in {
            'src/frameworks/MediaPlayer/CMakeLists.txt': ['MPMediaItemArtwork', 'MPNowPlayingInfoCenter'],
            'src/frameworks/CoreHaptics/CMakeLists.txt': ['CHHapticEvent', 'CHHapticPattern'],
            'src/frameworks/CoreML/CMakeLists.txt': ['MLModel', 'MLMultiArray'],
        }.items():
            section = patch_section('patches/darling.patch', path)
            self.assertIsNotNone(section)
            # The CMake lists are short; the source files carry the symbols.
            source_path = path.rsplit('/', 1)[0] + '/src/' + path.rsplit('/', 1)[0].rsplit('/', 1)[-1] + '.m'
            source_section = patch_section('patches/darling.patch', source_path)
            self.assertIsNotNone(source_section, source_path)
            source = added_lines(source_section)
            for symbol in symbols:
                self.assertIn(symbol, source)

    def test_constant_object_classes_are_exported(self):
        foundation = patch_section('patches/foundation.patch', FOUNDATION_NEW)
        self.assertIsNotNone(foundation)
        foundation_source = added_lines(foundation)
        for name in ('NSConstantIntegerNumber', 'NSConstantFloatNumber', 'NSConstantDoubleNumber'):
            self.assertIn(name, foundation_source)

        corefoundation = patch_section('patches/corefoundation.patch', COREFOUNDATION_NEW)
        self.assertIsNotNone(corefoundation)
        corefoundation_source = added_lines(corefoundation)
        for name in ('NSConstantArray', 'NSConstantDictionary'):
            self.assertIn(name, corefoundation_source)

        runtime = patch_section('patches/corefoundation.patch', 'CFRuntime.c')
        self.assertIsNotNone(runtime)
        self.assertIn('__NSArray0__struct', added_lines(runtime))

    def test_manifest_lists_constant_object_sources(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        by_patch = {c['patch']: c for c in manifest['components']}
        self.assertIn(FOUNDATION_NEW, by_patch['patches/foundation.patch']['new_files'])
        self.assertIn(COREFOUNDATION_NEW, by_patch['patches/corefoundation.patch']['new_files'])


if __name__ == '__main__':
    unittest.main()
