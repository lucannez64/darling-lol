"""Project skills: harness layout, frontmatter and identical copies."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRS = ('.opencode/skills', '.claude/skills')
ENTRYPOINT = 'AGENTS.md'


def skill_paths(directory):
    return sorted((ROOT / directory).glob('*/SKILL.md'))


def skill_names(directory):
    return {path.parent.name for path in skill_paths(directory)}


def frontmatter(text):
    """Return the frontmatter fields, or None when the header is malformed.

    CI installs no YAML library, so the few scalars a skill needs are checked
    against the plain-scalar rules: a value with a colon and a space must be
    quoted, as must a value that starts with a YAML indicator.
    """
    if not text.startswith('---\n'):
        return None
    header, separator, _body = text[4:].partition('\n---\n')
    if not separator:
        return None
    fields = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        if line[0] in ' \t':
            return None
        key, colon, value = line.partition(':')
        value = value.strip()
        if not colon or not key.strip() or not value:
            return None
        if value[0] in '"\'':
            if len(value) < 2 or value[-1] != value[0]:
                return None
        elif ': ' in value or value.endswith(':') or value[0] in '&*?|>!%@`{[':
            return None
        fields[key.strip()] = value
    return fields


class SkillLayoutTests(unittest.TestCase):
    def test_every_harness_ships_the_same_skills(self):
        reference = skill_names(SKILL_DIRS[0])
        self.assertTrue(reference, 'no project skills found')
        for directory in SKILL_DIRS[1:]:
            with self.subTest(directory=directory):
                self.assertEqual(skill_names(directory), reference)

    def test_copies_are_identical(self):
        for directory in SKILL_DIRS[1:]:
            for name in sorted(skill_names(SKILL_DIRS[0])):
                with self.subTest(skill=name, directory=directory):
                    canonical = ROOT / SKILL_DIRS[0] / name / 'SKILL.md'
                    copy = ROOT / directory / name / 'SKILL.md'
                    self.assertEqual(copy.read_bytes(), canonical.read_bytes(),
                                     f'{copy} drifted from {canonical}')

    def test_frontmatter_names_the_directory(self):
        for directory in SKILL_DIRS:
            for path in skill_paths(directory):
                with self.subTest(path=str(path.relative_to(ROOT))):
                    fields = frontmatter(path.read_text(encoding='utf-8'))
                    self.assertIsNotNone(fields, f'{path} has malformed frontmatter')
                    self.assertEqual(fields['name'].strip('"\''), path.parent.name)
                    description = fields['description'].strip('"\'')
                    self.assertTrue(description, f'{path} has an empty description')
                    self.assertLessEqual(len(description), 1024)

    def test_entrypoint_points_at_every_canonical_copy(self):
        guide = (ROOT / ENTRYPOINT).read_text(encoding='utf-8')
        for name in sorted(skill_names(SKILL_DIRS[0])):
            with self.subTest(skill=name):
                self.assertIn(f'{SKILL_DIRS[0]}/{name}/SKILL.md', guide)


if __name__ == '__main__':
    unittest.main()
