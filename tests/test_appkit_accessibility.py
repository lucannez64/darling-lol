"""Check the AppKit accessibility constant added by the Cocotron patch."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AppKitAccessibilityChecks(unittest.TestCase):
    def test_full_screen_button_uses_macos_attribute_name(self):
        patch = (ROOT / 'patches/cocotron.patch').read_text()
        expected = {
            'AppKit/include/AppKit/NSAccessibilityConstants.h':
                '+APPKIT_EXPORT NSString *const NSAccessibilityFullScreenButtonAttribute;\n',
            'AppKit/NSAccessibility.m':
                '+NSString *const NSAccessibilityFullScreenButtonAttribute =\n'
                '+        @"AXFullScreenButton";\n',
        }
        for path, added in expected.items():
            with self.subTest(path=path):
                _, _, section = patch.partition(f'diff --git a/{path} b/{path}\n')
                self.assertIn(added, section.split('\ndiff --git ', 1)[0])


if __name__ == '__main__':
    unittest.main()
