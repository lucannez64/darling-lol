#!/usr/bin/env python3
"""Check Iridium's LLVM major-version guard without Darling or a game process."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llvm_tools

root = Path(__file__).resolve().parent.parent
indium = root / 'build/darling-src/src/external/metal/deps/indium'
include = Path(llvm_tools.config('--includedir'))
header = (include / 'llvm/Config/llvm-config.h').read_text()
major = int(re.search(r'^#define LLVM_VERSION_MAJOR (\d+)', header, re.MULTILINE)[1])
with tempfile.TemporaryDirectory(prefix='iridium-llvm-version-') as directory:
    folder = Path(directory)
    library_source = folder / 'llvm.c'
    library_source.write_text('''
void LLVMGetVersion(unsigned* major, unsigned* minor, unsigned* patch) {
    *major = TEST_LLVM_MAJOR; *minor = 0; *patch = 0;
}
''')
    probe = folder / 'probe.cpp'
    probe.write_text('''
#include <iridium/dynamic-llvm.hpp>
#include <cstdio>
#include <exception>
int main() {
    try {
        if (!Iridium::DynamicLLVM::init()) return 2;
        Iridium::DynamicLLVM::finit();
        puts("LLVM version matched");
        return 0;
    } catch (const std::exception& error) {
        fprintf(stderr, "%s\\n", error.what());
        return 1;
    }
}
''')
    # Keep the loaded path fixed so the same compiled probe tests both majors.
    library = folder / 'libLLVM-test.so'
    executable = folder / 'probe'
    subprocess.run([
        'clang++', '-std=c++17', '-Wno-deprecated-declarations',
        '-DHOST_LLVM_LIBNAME="' + str(library) + '"', '-I' + str(include),
        '-I' + str(indium / 'include'), '-I' + str(indium / 'private-include'),
        str(probe), str(indium / 'src/iridium/dynamic-llvm.cpp'),
        '-ldl', '-o', str(executable),
    ], check=True)
    for loaded in (major, major - 1, major + 1):
        subprocess.run(['cc', '-shared', '-fPIC', f'-DTEST_LLVM_MAJOR={loaded}',
                        str(library_source), '-o', str(library)], check=True)
        result = subprocess.run([str(executable)], text=True, capture_output=True)
        if loaded == major:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode == 1, result.stderr
            assert f'built with LLVM {major}, loaded LLVM {loaded}' in result.stderr, result.stderr
        print(f'PASS: headers {major}, runtime {loaded}', flush=True)
    library.unlink()
    result = subprocess.run([str(executable)], capture_output=True)
    assert result.returncode == 2
    print('PASS: missing library fails initialization')
