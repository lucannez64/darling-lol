"""The compatibility-language check that validate.py runs on tracked files."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('compat_language', ROOT / 'scripts/compat_language.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def flagged(text, name='docs/EXAMPLE.md'):
    return compat.errors(name, text)


class CompatLanguage(unittest.TestCase):
    def test_framing_word_fails_anywhere(self):
        self.assertEqual(len(flagged('This build is unbannable.')), 1)
        self.assertEqual(len(flagged('A stealth launcher.')), 1)

    def test_loaded_word_needs_risk_context(self):
        self.assertEqual(flagged('The X11 compositor bypass stays on.'), [])
        self.assertEqual(flagged('Set RCLOL_SKIP_PREFLIGHT=1 to bypass the gate.'), [])
        self.assertEqual(len(flagged('This spoofs the telemetry.')), 1)
        self.assertEqual(len(flagged('An anti-cheat bypass.')), 1)

    def test_negated_non_goal_passes(self):
        self.assertEqual(flagged('The overrides are not intended to bypass anti-cheat.'), [])
        self.assertEqual(flagged('It is not, and will not be described as, a way to\n'
                                 'satisfy or evade an anti-cheat.'), [])

    def test_negation_in_another_sentence_does_not_count(self):
        self.assertEqual(len(flagged('Do not edit generated files.\nStealth telemetry is enabled.')), 1)
        self.assertEqual(len(flagged('Do not edit generated files. Stealth telemetry is enabled.')), 1)

    def test_error_names_file_and_line(self):
        [error] = flagged('fine\nAn unbannable build.', name='README.md')
        self.assertTrue(error.startswith('README.md:2: use compatibility language'))

    def test_policy_files_are_exempt(self):
        for name in compat.EXEMPT:
            self.assertEqual(flagged('Avoid stealth framing. An unbannable build.', name=name), [])

    def test_exempt_files_exist(self):
        for name in compat.EXEMPT:
            self.assertTrue((ROOT / name).is_file(), name)


if __name__ == '__main__':
    unittest.main()
