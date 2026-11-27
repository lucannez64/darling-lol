"""Source-level checks for the NSCalendarIdentifier constants.

Foundation code compares calendar identifiers with the CoreFoundation values
(kCFGregorianCalendar is "gregorian"), so the constants the CoreFoundation
patch defines must hold Apple's string values, not their own names.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/corefoundation.patch'

EXPECTED = {
    'NSCalendarIdentifierGregorian': 'gregorian',
    'NSCalendarIdentifierISO8601': 'iso8601',
    'NSCalendarIdentifierBuddhist': 'buddhist',
    'NSCalendarIdentifierChinese': 'chinese',
    'NSCalendarIdentifierCoptic': 'coptic',
    'NSCalendarIdentifierEthiopicAmeteAlem': 'ethiopic-amete-alem',
    'NSCalendarIdentifierEthiopicAmeteMihret': 'ethiopic',
    'NSCalendarIdentifierHebrew': 'hebrew',
    'NSCalendarIdentifierIndian': 'indian',
    'NSCalendarIdentifierIslamic': 'islamic',
    'NSCalendarIdentifierIslamicCivil': 'islamic-civil',
    'NSCalendarIdentifierIslamicTabular': 'islamic-tbla',
    'NSCalendarIdentifierIslamicUmmAlQura': 'islamic-umalqura',
    'NSCalendarIdentifierJapanese': 'japanese',
    'NSCalendarIdentifierPersian': 'persian',
    'NSCalendarIdentifierRepublicOfChina': 'roc',
}

ADDED = re.compile(
    r'^\+\s*(?:FOUNDATION_EXPORT\s+)?const\s+NSCalendarIdentifier\s+'
    r'(NSCalendarIdentifier\w+)\s*=\s*@"([^"]*)"\s*;', re.M)


class NSCalendarIdentifierTests(unittest.TestCase):
    def setUp(self):
        self.added = ADDED.findall(PATCH.read_text(encoding='utf-8'))

    def test_constants_have_corefoundation_values(self):
        self.assertEqual(dict(self.added), EXPECTED)
        self.assertEqual(len(self.added), len(EXPECTED), 'duplicate definition')

    def test_no_constant_equals_its_own_name(self):
        for name, value in self.added:
            self.assertNotEqual(name, value)


if __name__ == '__main__':
    unittest.main()
