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


def quoted_scalar(value):
    """Return True when value is a quoted scalar with valid escapes."""
    quote, body = value[0], value[1:]
    if not body.endswith(quote):
        return False
    inner = body[:-1]
    index = 0
    while index < len(inner):
        char = inner[index]
        if quote == '"' and char == '\\':
            index += 2
            continue
        if char == quote:
            if quote == "'" and inner[index + 1:index + 2] == "'":
                index += 2
                continue
            return False
        index += 1
    return index == len(inner)


def frontmatter(text):
    """Return the frontmatter fields, or None when the header is malformed.

    CI installs no YAML library, so the few scalars a skill needs are checked
    against the plain-scalar and quoting rules: a value with a colon and a
    space must be quoted, a value that starts with a YAML indicator must be
    quoted, and a quoted value must close with its own quote and escape any
    copy of that quote inside.
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
            if not quoted_scalar(value):
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


class FrontmatterTests(unittest.TestCase):
    def test_rejects_unescaped_quotes(self):
        text = '---\nname: x\ndescription: "Open "review" comments"\n---\n'
        self.assertIsNone(frontmatter(text))

    def test_accepts_escaped_quotes(self):
        text = '---\nname: x\ndescription: "Open \\"review\\" comments"\n---\n'
        self.assertIsNotNone(frontmatter(text))

    def test_accepts_single_quoted_escapes(self):
        text = "---\nname: x\ndescription: 'it''s fine'\n---\n"
        self.assertIsNotNone(frontmatter(text))

    def test_rejects_unterminated_quotes(self):
        text = '---\nname: x\ndescription: "unterminated\n---\n'
        self.assertIsNone(frontmatter(text))


if __name__ == '__main__':
    unittest.main()
