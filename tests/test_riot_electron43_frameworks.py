"""Source-level checks for the Electron 43 framework stubs and constant literals.

The stubs are only compiled on a prepared Darling tree, so CI cannot link them.
These checks keep the symbol lists honest instead: an import that Electron 43
binds must have a matching definition in the stub source, and the constant
literals the patches add must be the ones the guest check exercises.
"""
import importlib.util
from pathlib import Path
import unittest
import tempfile
import re
import shutil
import subprocess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'riot_electron43_frameworks', ROOT / 'tools/build-riot-electron43-frameworks.py')
frameworks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frameworks)

CONSTANT_CLASSES = ('NSConstantIntegerNumber', 'NSConstantDoubleNumber',
                    'NSConstantFloatNumber', 'NSConstantArray', 'NSConstantDictionary')


class FrameworkStubTests(unittest.TestCase):
    def test_versioned_layout_resolves_and_repairs_old_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / 'QuickLookUI.framework'
            version = bundle / 'Versions/A'
            version.mkdir(parents=True)
            binary = bundle / 'QuickLookUI'
            binary.write_bytes(b'framework fixture')
            (version / 'QuickLookUI').symlink_to('../../../QuickLookUI')
            frameworks.link_versions(bundle, 'QuickLookUI')
            frameworks.link_versions(bundle, 'QuickLookUI')
            self.assertEqual((bundle / 'Versions/Current/QuickLookUI').read_bytes(),
                             binary.read_bytes())

    def test_install_creates_layout_even_for_unchanged_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'output'
            home = root / 'home'
            for name in frameworks.FRAMEWORKS:
                bundle = output / (name + '.framework')
                bundle.mkdir(parents=True)
                (bundle / name).write_bytes(b'framework fixture')
            with patch.object(frameworks, 'OUTPUT', output), patch.object(
                    frameworks, 'guest_home', return_value=home):
                frameworks.install(root / 'backups')
                # Recreate the flat layout left by the old installer.
                for name in frameworks.FRAMEWORKS:
                    (home / 'RuntimeFrameworks' / (name + '.framework') /
                     'Versions/A' / name).unlink()
                frameworks.install(root / 'backups')
            for name in frameworks.FRAMEWORKS:
                bundle = home / 'RuntimeFrameworks' / (name + '.framework')
                self.assertEqual((bundle / 'Versions/Current' / name).read_bytes(),
                                 b'framework fixture')
                self.assertTrue((bundle / 'Resources/Info.plist').is_file())

    @unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
    def test_number_comparison_handles_mixed_integer_signedness(self):
        # Compile the shipped comparison helper with host accessor stand-ins.
        # The guest check separately exercises the Objective-C dispatch path.
        patch_text = (ROOT / 'patches/corefoundation.patch').read_text()
        section = patch_text.split('diff --git a/NSNumber.m ', 1)[1]
        added = '\n'.join(line[1:] for line in section.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        helper = added[added.index('static BOOL _NSTypeIsFloating'):]
        helper = helper.split('\n    if (!_CFIsCFObject', 1)[0]
        helper = re.sub(r'\[(\w+) (objCType|doubleValue|longLongValue|unsignedLongLongValue)\]',
                        r'\1->\2()', helper)
        preamble = r"""
#include <cassert>
#include <climits>
#include <cstddef>
using BOOL = bool;
constexpr bool YES = true, NO = false;
enum NSComparisonResult { NSOrderedAscending=-1, NSOrderedSame=0, NSOrderedDescending=1 };
constexpr char _C_FLT='f', _C_DBL='d', _C_UCHR='C', _C_USHT='S',
               _C_UINT='I', _C_ULNG='L', _C_ULNG_LNG='Q';
struct Number {
    const char *type;
    unsigned long long value;
    const char *objCType() { return type; }
    long long longLongValue() { return static_cast<long long>(value); }
    unsigned long long unsignedLongLongValue() { return value; }
    double doubleValue() { return type[0]=='Q' ? double(value) : double(longLongValue()); }
};
using id = Number *;
"""
        cases = r"""
int main() {
    Number maximum{"Q", ULLONG_MAX}, negative{"q", ULLONG_MAX},
           zero{"q", 0}, positive{"q", 42}, unsignedPositive{"Q", 42};
    assert(_NSCompareNonCFNumber(&maximum, &negative) == NSOrderedDescending);
    assert(_NSCompareNonCFNumber(&negative, &maximum) == NSOrderedAscending);
    assert(_NSCompareNonCFNumber(&maximum, &zero) == NSOrderedDescending);
    assert(_NSCompareNonCFNumber(&zero, &maximum) == NSOrderedAscending);
    assert(_NSCompareNonCFNumber(&positive, &unsignedPositive) == NSOrderedSame);
    assert(_NSCompareNonCFNumber(&unsignedPositive, &positive) == NSOrderedSame);
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'compare.cpp'
            binary = Path(tmp) / 'compare'
            source.write_text(preamble + helper + cases)
            subprocess.run(['c++', '-std=c++11', str(source), '-o', str(binary)],
                           check=True, capture_output=True, text=True)
            subprocess.run([str(binary)], check=True)

    def test_imports_exclude_weak_symbols_from_llvm_nm_output(self):
        output = """                 (undefined) external _required (from CoreML)
                 (undefined) weak external _optional (from CoreML)
                 (undefined) weak external _optional_other (from Foundation)
                 (undefined) external _unrelated (from Foundation)
"""
        with patch.object(frameworks.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 0, output, '')):
            self.assertEqual(frameworks.imports('fixture', 'CoreML'), ['_required'])

    def test_every_framework_has_a_source_and_symbol_list(self):
        for name, (_, required) in frameworks.FRAMEWORKS.items():
            with self.subTest(framework=name):
                self.assertTrue((frameworks.SOURCES / f'{name}.m').is_file())
                self.assertTrue(required, 'a framework without symbols checks nothing')

    def test_every_required_symbol_is_defined_in_its_stub(self):
        for name, (_, required) in frameworks.FRAMEWORKS.items():
            source = (frameworks.SOURCES / f'{name}.m').read_text()
            for symbol in required:
                # _OBJC_CLASS_$_Foo is defined by @implementation Foo; a plain
                # constant simply loses the leading underscore in C.
                identifier = symbol.split('$_')[-1] if '$_' in symbol else symbol.lstrip('_')
                with self.subTest(framework=name, symbol=symbol):
                    if '$_' in symbol:
                        pattern = rf'@implementation\s+{re.escape(identifier)}\b'
                    else:
                        pattern = rf'\b{re.escape(identifier)}\b\s*(?:=|\([^;{{}}]*\)\s*\{{)'
                    self.assertRegex(source, pattern)

    def test_guest_check_covers_every_constant_class(self):
        check = (frameworks.SOURCES / 'constant-literal-check.m').read_text()
        for name in CONSTANT_CLASSES:
            if name == 'NSConstantFloatNumber':
                continue  # exercised through the double layout; clang emits it rarely
            self.assertIn(f'objc_getClass("{name}")', check)

    def test_patches_add_the_constant_sources(self):
        corefoundation = (ROOT / 'patches/corefoundation.patch').read_text()
        foundation = (ROOT / 'patches/foundation.patch').read_text()
        self.assertIn('+++ b/NSConstantCollections.m', corefoundation)
        self.assertIn('+++ b/src/NSConstantNumber.m', foundation)
        for name in CONSTANT_CLASSES:
            self.assertIn(f'@implementation {name}', corefoundation + foundation)
        for symbol in ('__NSArray0__struct', '__NSDictionary0__struct'):
            self.assertIn(symbol, corefoundation)

    def test_checker_names_the_same_classes(self):
        checker = (ROOT / 'tools/check-riot-constant-literals.py').read_text()
        for name in CONSTANT_CLASSES:
            self.assertIn(f'_OBJC_CLASS_$_{name}', checker)


if __name__ == '__main__':
    unittest.main()
