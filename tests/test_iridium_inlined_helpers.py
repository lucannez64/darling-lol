"""Translate AIR kernels whose inlined internal helpers use what only a helper body used to hold.

Before helpers were inlined, a kernel that called one failed to translate, so these lowering
paths were never reached from a helper: comparisons, arithmetic and conversions on i1 (a SPIR-V
boolean, which the integer instructions reject), a constant GEP into a nested array global,
i8/i16 constants, and a store to the first element of a local array through the array's pointer.
Every fixture must translate to a module spirv-val accepts, or fail to translate; none may yield
a module spirv-val rejects. The translator is built as in test_iridium_lifetime_markers. No game,
client or guest process is involved. Skipped when the checkout, LLVM or spirv-val is missing.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import llvm_tools  # noqa: E402
import test_iridium_lifetime_markers as lifetime  # noqa: E402

FIXTURES = ROOT / 'tests/fixtures'
# fixture -> the SPIR-V instructions its helper body must lower to
VALID = {
    'iridium-bool-compare': ('OpLogicalEqual', 'OpLogicalNotEqual'),
    'iridium-bool-vector-compare': ('OpLogicalEqual', 'OpLogicalNotEqual', 'OpLogicalAnd',
                                    'OpLogicalOr', 'OpBitwiseAnd', 'OpSelect'),
    'iridium-bool-convert': ('OpSelect', 'OpBitwiseAnd', 'OpINotEqual'),
    'iridium-bool-logic': ('OpLogicalAnd', 'OpLogicalOr', 'OpLogicalNotEqual', 'OpPhi'),
    'iridium-constant-gep': ('OpAccessChain',),
    'iridium-narrow-constants': ('OpTypeInt 8 1', 'OpTypeInt 16 1'),
    'iridium-lifetime-markers': ('OpAccessChain',),
}
# fixture -> the error its translation must stop with
REJECTED = {
    'iridium-bool-ordered-compare': 'Unsupported AIR boolean comparison',
}

SKIP = lifetime.SKIP or (None if shutil.which('spirv-val') and shutil.which('spirv-dis')
                         else 'spirv-val and spirv-dis are required')


@unittest.skipIf(SKIP, SKIP)
class IridiumInlinedHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        work = Path(cls.folder.name)
        tree = work / 'indium'
        tree.mkdir()
        archive = subprocess.run(['git', '-C', str(lifetime.CHECKOUT), 'archive',
                                  lifetime.pinned_revision()], capture_output=True, check=True).stdout
        subprocess.run(['tar', '-x', '-C', str(tree)], input=archive, check=True)
        subprocess.run(['git', 'apply', str(ROOT / 'patches/metal--deps--indium.patch')],
                       cwd=tree, check=True)
        library = Path(llvm_tools.config('--libdir')) / llvm_tools.config('--link-shared',
                                                                           '--libnames')
        cls.translator = work / 'translate'
        subprocess.run(['clang++', '-O0', '-std=c++17', '-w',
                        f'-DHOST_LLVM_LIBNAME="{library}"',
                            # Iridium with the translation cache (#211) refuses to build without an identity.
                            '-DIRIDIUM_SOURCE_HASH="host-test-translator-identity"',
                        '-I' + llvm_tools.config('--includedir'),
                        '-I' + str(tree / 'include'), '-I' + str(tree / 'private-include'),
                        str(ROOT / 'tools/riot-air-translate-check.cpp'),
                        *[str(tree / 'src/iridium' / f'{name}.cpp')
                          for name in ('air', 'dynamic-llvm', 'iridium', 'spirv')],
                        '-ldl', '-o', str(cls.translator)], check=True)
        cls.assembler = Path(llvm_tools.config('--bindir')) / 'llvm-as'

    def translate(self, fixture):
        """Translate one fixture alone; returns (translator run, path of the module it wrote)."""
        work = Path(tempfile.mkdtemp(dir=self.folder.name))
        bitcode = work / 'probe.bc'
        subprocess.run([str(self.assembler), str(FIXTURES / f'{fixture}.ll'), '-o', str(bitcode)],
                       check=True)
        (work / 'in').mkdir()
        (work / 'out').mkdir()
        (work / 'in/probe.bin').write_bytes(lifetime.metallib(bitcode.read_bytes(), 'probe'))
        run = subprocess.run([str(self.translator), str(work / 'in'), str(work / 'out')],
                             capture_output=True, text=True, timeout=60)
        return run, work / 'out/probe.bin.spv'

    def test_helper_bodies_translate_to_valid_spirv(self):
        for fixture, instructions in VALID.items():
            with self.subTest(fixture=fixture):
                run, module = self.translate(fixture)
                self.assertIn('1 passed, 0 failed', run.stdout, run.stdout + run.stderr)
                checked = subprocess.run(['spirv-val', str(module)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                # The fixture only covers a lowering while the passes leave its instruction in.
                listing = subprocess.run(['spirv-dis', '--raw-id', str(module)],
                                         capture_output=True, text=True, check=True).stdout
                for instruction in instructions:
                    self.assertIn(instruction, listing)

    def test_unsupported_helper_bodies_fail_to_translate(self):
        for fixture, reason in REJECTED.items():
            with self.subTest(fixture=fixture):
                run, module = self.translate(fixture)
                self.assertIn('0 passed, 1 failed', run.stdout, run.stdout + run.stderr)
                self.assertIn(reason, run.stdout)
                self.assertFalse(module.exists(), 'a module was written for unsupported input')


if __name__ == '__main__':
    unittest.main()
