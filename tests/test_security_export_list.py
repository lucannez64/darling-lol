"""Offline contract for regenerating and relinking Security's export list.

Security.exp-in #includes other .exp-in files. An incremental build must regenerate
Security.<arch>.exp when any of them changes, and must relink Security when the list
changes; otherwise a newly exported symbol is compiled but dropped at link time.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CMAKE = 'OSX/CMakeLists.txt'


def section(path):
    text = (ROOT / 'patches/security.patch').read_text()
    match = re.search(rf'^diff --git \w/{re.escape(path)} \w/{re.escape(path)}\n', text, re.M)
    if not match:
        return ''
    return text[match.end():].split('\ndiff --git ', 1)[0]


def added(path):
    return '\n'.join(line[1:] for line in section(path).splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class SecurityExportListTests(unittest.TestCase):
    def test_export_list_depends_on_every_included_file(self):
        source = added(CMAKE)
        for included in (
            '${CMAKE_CURRENT_SOURCE_DIR}/../Security.exp-in',
            '${CMAKE_CURRENT_SOURCE_DIR}/../CSSMOID.exp-in',
            '${CMAKE_CURRENT_SOURCE_DIR}/sec/Security/SecExports.exp-in',
            '${CMAKE_CURRENT_SOURCE_DIR}/sec/Security/SecAccessControlExports.exp-in',
            '${CMAKE_CURRENT_SOURCE_DIR}/../keychain/SecureObjectSync/SOSExports.exp-in',
        ):
            self.assertIn(included, source)

    def test_both_architectures_use_the_full_list(self):
        self.assertEqual(len(re.findall(r'^\+\t\t\t\$\{SECURITY_EXP_IN_DEPENDS\}$', section(CMAKE), re.M)), 2)

    def test_security_relinks_when_the_export_list_changes(self):
        # add_dependencies only orders targets; without LINK_DEPENDS the regenerated
        # list never reaches the linked Security.
        source = added(CMAKE)
        for arch in ('64BIT', '32BIT'):
            self.assertIn(
                f'set_property(TARGET Security_${{APPLE_ARCH_{arch}}}_firstpass Security_${{APPLE_ARCH_{arch}}}\n'
                f'\t\tAPPEND PROPERTY LINK_DEPENDS ${{CMAKE_CURRENT_BINARY_DIR}}/Security.${{APPLE_ARCH_{arch}}}.exp',
                source)


if __name__ == '__main__':
    unittest.main()
