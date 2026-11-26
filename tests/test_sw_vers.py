"""sw_vers matches the macOS 11+ interface measured on macOS 15.5; source-only, never starts Darling."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darling.patch'


def added_file(path):
    """Return the added/kept lines of path's hunks in patches/darling.patch."""
    text = PATCH.read_text()
    start = text.index('+++ b/' + path + '\n')
    end = text.find('\ndiff --git ', start)
    section = text[start:end if end != -1 else len(text)]
    return '\n'.join(line[1:] for line in section.splitlines()[1:]
                     if line[:1] in ('+', ' ') and not line.startswith('+++'))


class SwVersTests(unittest.TestCase):
    def setUp(self):
        self.source = added_file('src/tools/sw_vers.c')

    def test_default_output_uses_macos_tab_layout(self):
        # macOS 15.5: "ProductName:\t\tmacOS\nProductVersion:\t\t15.5\nBuildVersion:\t\t24F74\n"
        for label in ('"ProductName:\\t\\t"', '"ProductVersion:\\t\\t"', '"BuildVersion:\\t\\t"',
                      '"ProductVersionExtra:\\t"'):
            self.assertIn(label, self.source)
        self.assertNotIn('ProductName:    %@', self.source)

    def test_options_and_usage_match_macos(self):
        self.assertIn('getopt_long_only', self.source)
        for option in ('"help"', '"productName"', '"productVersion"', '"productVersionExtra"',
                       '"buildVersion"'):
            self.assertIn(option, self.source)
        self.assertIn('"Usage: sw_vers [--help|--productName|--productVersion|'
                      '--productVersionExtra|--buildVersion]\\n"', self.source)
        # --help prints the usage on stdout and succeeds; an unknown option fails.
        self.assertIn('fputs(usage_text, stdout);\n\t\treturn 0;', self.source)
        self.assertIn("if (ch == '?') {\n\t\t\tfputs(usage_text, stdout);\n\t\t\treturn 1;", self.source)


if __name__ == '__main__':
    unittest.main()
