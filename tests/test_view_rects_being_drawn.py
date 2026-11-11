"""Offline contract for -[NSView getRectsBeingDrawn:count:] with no opaque ancestor."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION = 'AppKit/NSView.m'


def patch_lines(path, prefixes):
    marker = f'diff --git a/{path} b/{path}\n'
    section = (ROOT / 'patches/cocotron.patch').read_text().split(marker, 1)[1]
    section = section.split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line[:1] in prefixes and not line.startswith('+++'))


def added_source(path):
    return patch_lines(path, ('+',))


def patched_source(path):
    # Context lines too: the ancestor queries themselves are unchanged lines.
    return patch_lines(path, (' ', '+'))


class RectsBeingDrawnTests(unittest.TestCase):
    # opaqueAncestor returns nil when no view up the chain is opaque (an Electron
    # autofill popup); a message to nil writes nothing, so the out-parameters
    # must start empty or NSZoneCalloc gets a stack-garbage count.
    def test_ancestor_out_parameters_start_empty(self):
        source = added_source(IMPLEMENTATION)
        self.assertEqual(source.count('const NSRect *ancestorRects = NULL;'), 2)
        self.assertEqual(source.count('NSInteger ancestorRectsCount = 0;'), 2)

    def test_count_is_ignored_when_no_rects_came_back(self):
        source = added_source(IMPLEMENTATION)
        self.assertIn('_rectsBeingRedrawnCount = ancestorRects ? ancestorRectsCount : 0;', source)
        self.assertIn('if (ancestorRects == NULL)\n                    ancestorRectsCount = 0;', source)

    def test_ivar_is_no_longer_the_out_parameter(self):
        source = added_source(IMPLEMENTATION)
        self.assertIsNone(re.search(r'count:\s*&_rectsBeingRedrawnCount\]', source))

    def test_ancestor_queries_are_kept(self):
        source = patched_source(IMPLEMENTATION)
        self.assertEqual(source.count('[opaqueAncestor getRectsBeingDrawn: &ancestorRects'), 2)
        self.assertEqual(source.count('count: &ancestorRectsCount];'), 2)


if __name__ == '__main__':
    unittest.main()
