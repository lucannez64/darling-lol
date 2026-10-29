#!/usr/bin/env python3
"""Build Darling shader probes, or a native offline translator with --native."""
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llvm_tools
root = Path(__file__).resolve().parent.parent
if '--native' in sys.argv:
    indium = root / 'build/darling-src/src/external/metal/deps/indium'
    llvm_include = llvm_tools.config('--includedir')
    llvm_libdir = Path(llvm_tools.config('--libdir'))
    llvm_names = shlex.split(llvm_tools.config('--link-shared', '--libnames'))
    if len(llvm_names) != 1:
        raise SystemExit('Iridium requires one shared LLVM library, got: ' + repr(llvm_names))
    llvm_library = llvm_libdir / llvm_names[0]
    print(f'Iridium LLVM headers: {llvm_include}; library: {llvm_library}', flush=True)
    subprocess.run(['clang++', '-O2', '-std=c++17',
                    '-DHOST_LLVM_LIBNAME="' + str(llvm_library) + '"', '-I' + llvm_include,
                    '-I' + str(indium / 'include'), '-I' + str(indium / 'private-include'),
                    str(root / 'tools/riot-air-translate-check.cpp'),
                    *[str(indium / 'src/iridium' / (name + '.cpp'))
                      for name in ('air', 'dynamic-llvm', 'iridium', 'spirv')],
                    '-ldl', '-o', str(root / 'build/riot-air-translate-check-native')], check=True)
    sys.exit(0)
folder = root / 'build/darling-src/build/src/external/metal'
flags = {}
for line in (folder / 'CMakeFiles/Metal.dir/flags.make').read_text().splitlines():
    if line.startswith('CXX_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
obj = root / 'build/riot-air-corpus-check.o'
subprocess.run(['clang++', *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'], *flags['CXX_FLAGS'],
                '-c', str(root / 'tools/riot-air-corpus-check.mm'), '-o', str(obj)], check=True)
subprocess.run(['clang++', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                '-isysroot', '@DARLING_SDK@', str(obj), '-lSystem', '-lc++', '-lobjc',
                '-framework', 'Foundation', str(root / 'build/riot-Metal'), '-o',
                str(root / 'build/riot-air-corpus-check')], check=True)

obj = root / 'build/riot-air-translate-check.o'
subprocess.run(['clang++', *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'], *flags['CXX_FLAGS'],
                '-c', str(root / 'tools/riot-air-translate-check.cpp'), '-o', str(obj)], check=True)
subprocess.run(['clang++', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                '-isysroot', '@DARLING_SDK@', str(obj), '-lSystem', '-lc++',
                str(root / 'build/riot-libiridium.dylib'), '-o',
                str(root / 'build/riot-air-translate-check')], check=True)
