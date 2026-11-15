"""Translate an AIR kernel whose inlined helper keeps an alloca with llvm.lifetime markers.

always-inline wraps an inlined helper's allocas in llvm.lifetime.start/end, and SROA only
drops them for allocas it promotes. A dynamically indexed local array stays an alloca, so its
markers reach Iridium's call dispatch, which must treat them as no-ops. The pinned Indium
revision is exported from the local component checkout, the shipped patch is applied, and the
repo's offline translator is built natively against the host LLVM. No game, client or guest
process is involved. Skipped when the checkout or LLVM is missing.
"""
from pathlib import Path
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import llvm_tools  # noqa: E402

COMPONENT = 'src/external/metal/deps/indium'
CHECKOUT = ROOT / 'build/darling-src' / COMPONENT
FIXTURE = ROOT / 'tests/fixtures/iridium-lifetime-markers.ll'
FUNCTION_STORAGE = 7


def pinned_revision():
    manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
    entries = [entry for entry in manifest['components'] if entry['path'] == COMPONENT]
    return entries[0]['revision']


def metallib(bitcode, name):
    """The minimal one-kernel metallib layout Iridium's AIR::Library reads."""
    def tag(label, payload):
        return label + struct.pack('<H', len(payload)) + payload
    group = (tag(b'NAME', name.encode() + b'\0') + tag(b'TYPE', bytes([2])) +
             tag(b'MDSZ', struct.pack('<Q', len(bitcode))) +
             tag(b'OFFT', struct.pack('<QQQ', 0, 0, 0)) + b'ENDT')
    functions = struct.pack('<II', 1, len(group) + 4) + group
    bitcode_offset = 88 + len(functions)
    header = (b'MTLB' + struct.pack('<HHHBBHH', 0x8001, 1, 2, 0, 0x81, 14, 0) +
              struct.pack('<QQQQQQQQQ', bitcode_offset + len(bitcode), 88, len(functions),
                          0, 0, 0, 0, bitcode_offset, len(bitcode)))
    return header + functions + bitcode


def has_local_array(module):
    """Whether the module declares a Function-storage OpVariable of an array type (the alloca)."""
    words = struct.unpack(f'<{len(module) // 4}I', module)
    assert words[0] == 0x07230203, 'not a SPIR-V module'
    arrays, pointers, variables = set(), {}, []
    index = 5
    while index < len(words):
        opcode, count = words[index] & 0xffff, words[index] >> 16
        operands = words[index + 1:index + count]
        if opcode == 28:  # OpTypeArray
            arrays.add(operands[0])
        elif opcode == 32:  # OpTypePointer
            pointers[operands[0]] = (operands[1], operands[2])
        elif opcode == 59:  # OpVariable
            variables.append((operands[0], operands[2]))
        index += count
    return any(storage == FUNCTION_STORAGE and pointers.get(type_id, (None, None))[1] in arrays
               for type_id, storage in variables)


def prerequisites():
    if not (shutil.which('clang++') and shutil.which('git') and llvm_tools.find('llvm-config')):
        return 'clang++, git and llvm-config are required'
    if not (CHECKOUT / '.git').exists():
        return 'the Indium component checkout is not prepared'
    found = subprocess.run(['git', '-C', str(CHECKOUT), 'cat-file', '-e',
                            pinned_revision() + '^{commit}'], capture_output=True)
    return None if found.returncode == 0 else 'the pinned Indium revision is not in the checkout'


SKIP = prerequisites()


@unittest.skipIf(SKIP, SKIP)
class IridiumLifetimeMarkersTest(unittest.TestCase):
    def test_retained_alloca_with_lifetime_markers_translates(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            tree = work / 'indium'
            tree.mkdir()
            archive = subprocess.run(['git', '-C', str(CHECKOUT), 'archive', pinned_revision()],
                                     capture_output=True, check=True).stdout
            subprocess.run(['tar', '-x', '-C', str(tree)], input=archive, check=True)
            subprocess.run(['git', 'apply', str(ROOT / 'patches/metal--deps--indium.patch')],
                           cwd=tree, check=True)

            library = Path(llvm_tools.config('--libdir')) / llvm_tools.config('--link-shared',
                                                                               '--libnames')
            translator = work / 'translate'
            subprocess.run(['clang++', '-O0', '-std=c++17', '-w',
                            f'-DHOST_LLVM_LIBNAME="{library}"',
                            # Iridium with the translation cache (#211) refuses to build without an identity.
                            '-DIRIDIUM_SOURCE_HASH="host-test-translator-identity"',
                            '-I' + llvm_tools.config('--includedir'),
                            '-I' + str(tree / 'include'), '-I' + str(tree / 'private-include'),
                            str(ROOT / 'tools/riot-air-translate-check.cpp'),
                            *[str(tree / 'src/iridium' / f'{name}.cpp')
                              for name in ('air', 'dynamic-llvm', 'iridium', 'spirv')],
                            '-ldl', '-o', str(translator)], check=True)

            assembler = Path(llvm_tools.config('--bindir')) / 'llvm-as'
            bitcode = work / 'probe.bc'
            subprocess.run([str(assembler), str(FIXTURE), '-o', str(bitcode)], check=True)
            (work / 'in').mkdir()
            (work / 'out').mkdir()
            (work / 'in/probe.bin').write_bytes(metallib(bitcode.read_bytes(), 'probe'))
            run = subprocess.run([str(translator), str(work / 'in'), str(work / 'out')],
                                 capture_output=True, text=True, timeout=60)
            self.assertIn('1 passed, 0 failed', run.stdout, run.stdout + run.stderr)

            # The fixture only covers the markers while the helper's alloca survives the passes.
            module = (work / 'out/probe.bin.spv').read_bytes()
            self.assertTrue(has_local_array(module), 'the helper alloca was promoted away')


if __name__ == '__main__':
    unittest.main()
