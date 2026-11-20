"""Project skills: harness layout, frontmatter and identical copies."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRS = ('.opencode/skills', '.claude/skills')
SKILL = 'open-pr'
CANONICAL = f'.opencode/skills/{SKILL}/SKILL.md'


def frontmatter(text):
    """Return the frontmatter fields of a SKILL.md file."""
    if not text.startswith('---\n'):
        return None
    header = text.split('---\n', 2)[1]
    return dict(line.split(':', 1) for line in header.splitlines() if ':' in line)


class SkillLayoutTests(unittest.TestCase):
    def test_every_harness_ships_the_skill(self):
        for directory in SKILL_DIRS:
            with self.subTest(directory=directory):
                path = ROOT / directory / SKILL / 'SKILL.md'
                self.assertTrue(path.is_file(), f'{path} is missing')

    def test_copies_are_identical(self):
        reference = (ROOT / CANONICAL).read_bytes()
        for directory in SKILL_DIRS:
            with self.subTest(directory=directory):
                copy = ROOT / directory / SKILL / 'SKILL.md'
                self.assertEqual(copy.read_bytes(), reference,
                                 f'{copy} drifted from {CANONICAL}')

    def test_frontmatter_names_the_directory(self):
        for directory in SKILL_DIRS:
            with self.subTest(directory=directory):
                path = ROOT / directory / SKILL / 'SKILL.md'
                fields = frontmatter(path.read_text(encoding='utf-8'))
                self.assertIsNotNone(fields, f'{path} has no frontmatter')
                self.assertEqual(fields['name'].strip(), path.parent.name)
                self.assertTrue(fields['description'].strip())

    def test_entrypoint_points_at_the_canonical_copy(self):
        guide = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn(CANONICAL, guide)


if __name__ == '__main__':
    unittest.main()
