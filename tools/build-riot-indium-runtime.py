#!/usr/bin/env python3
"""Build the paired Iridium shader translator and Indium runtime overrides."""
from pathlib import Path
import os
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llvm_tools
root = Path(__file__).resolve().parent.parent
folder = root / 'build/darling-src/build/src/external/metal/deps/indium'
# Keep the validated LLVM major locally when a system update replaces it.
pinned_llvm = root / 'build/llvm22/usr'
resource_include = subprocess.check_output(['clang++', '-print-resource-dir'], text=True).strip() + '/include'
def current_includes(values):
    return [resource_include if value.startswith('/usr/lib/clang/') and value.endswith('/include') else value for value in values]
iridium_flags = {}
for line in (folder / 'CMakeFiles/iridium.dir/flags.make').read_text().splitlines():
    if line.startswith('CXX_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        iridium_flags[key] = shlex.split(value)
iridium_flags['CXX_INCLUDES'] = current_includes(iridium_flags['CXX_INCLUDES'])
# RIOT_IRIDIUM_SYSTEM_LLVM=1 ignores the pin and builds against the host LLVM, as a fresh checkout does.
if (pinned_llvm / 'lib/libLLVM-22.so').exists() and os.environ.get('RIOT_IRIDIUM_SYSTEM_LLVM') != '1':
    llvm_include = pinned_llvm / 'include'
    llvm_library = pinned_llvm / 'lib/libLLVM-22.so'
else:
    # Do not combine cached CMake include paths with a newer llvm-config library.
    llvm_include = Path(llvm_tools.config('--includedir'))
    llvm_libdir = Path(llvm_tools.config('--libdir'))
    llvm_names = shlex.split(llvm_tools.config('--link-shared', '--libnames'))
    if len(llvm_names) != 1:
        raise SystemExit('Iridium requires one shared LLVM library, got: ' + repr(llvm_names))
    llvm_library = llvm_libdir / llvm_names[0]
iridium_flags['CXX_INCLUDES'].insert(0, '-I' + str(llvm_include))
iridium_flags['CXX_DEFINES'] = [value for value in iridium_flags['CXX_DEFINES'] if not value.startswith('-DHOST_LLVM_LIBNAME=')]
iridium_flags['CXX_DEFINES'].append('-DHOST_LLVM_LIBNAME="' + str(llvm_library) + '"')
print(f'Iridium LLVM headers: {llvm_include}; library: {llvm_library}', flush=True)
iridium_link = shlex.split((folder / 'CMakeFiles/iridium.dir/link.txt').read_text())
for name in ('air', 'dynamic-llvm', 'iridium', 'spirv'):
    obj = root / f'build/riot-iridium-{name}.o'
    subprocess.run(['clang++', *iridium_flags['CXX_DEFINES'], *iridium_flags['CXX_INCLUDES'],
                    *iridium_flags['CXX_FLAGS'], '-O2', '-c', str(root / ('build/darling-src/src/external/metal/deps/indium/src/iridium/' + name + '.cpp')),
                    '-o', str(obj)], check=True)
    iridium_link[iridium_link.index('CMakeFiles/iridium.dir/src/iridium/' + name + '.cpp.o')] = str(obj)
iridium_link[iridium_link.index('-o') + 1] = str(root / 'build/riot-libiridium.dylib')
subprocess.run(iridium_link, cwd=folder, check=True)
if '--iridium-only' in sys.argv:
    sys.exit(0)
flags = {}
for line in (folder / 'CMakeFiles/indium.dir/flags.make').read_text().splitlines():
    if line.startswith('CXX_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
flags['CXX_INCLUDES'] = current_includes(flags['CXX_INCLUDES'])
link = shlex.split((folder / 'CMakeFiles/indium.dir/link.txt').read_text())
# Rebuild every object: private encoder layouts are shared with command-buffer factories.
indium_names = [Path(arg).name.removesuffix('.cpp.o') for arg in link
                if arg.startswith('CMakeFiles/indium.dir/src/indium/') and arg.endswith('.cpp.o')]
for name in indium_names:
    obj = root / f'build/riot-indium-{name}.o'
    subprocess.run(['clang++', *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'],
                    *flags['CXX_FLAGS'], '-O2', '-c', str(root / ('build/darling-src/src/external/metal/deps/indium/src/indium/' + name + '.cpp')),
                    '-o', str(obj)], check=True)
    link[link.index('CMakeFiles/indium.dir/src/indium/' + name + '.cpp.o')] = str(obj)
link[link.index('-o') + 1] = str(root / 'build/riot-libindium.dylib')
subprocess.run(link, cwd=folder, check=True)
