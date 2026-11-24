#!/usr/bin/env python3
"""Build the CoreGraphics display and cursor compatibility override."""
from pathlib import Path
import shlex
import subprocess
root = Path(__file__).resolve().parent.parent
folder = root / 'build/darling-src/build/src/external/cocotron/CoreGraphics'
flags = {}
for line in (folder / 'CMakeFiles/CoreGraphics.dir/flags.make').read_text().splitlines():
    if line.startswith('C_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
link = shlex.split((folder / 'CMakeFiles/CoreGraphics.dir/link.txt').read_text())
for name in ('CGDirectDisplay', 'CGSubWindow', 'DarlingDisplayTiming'):
    suffix = 'c' if name == 'DarlingDisplayTiming' else 'm'
    obj = root / f'build/riot-{name}.o'
    subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                    '-O2', '-Wno-macro-redefined', '-c',
                    str(root / f'build/darling-src/src/external/cocotron/CoreGraphics/{name}.{suffix}'),
                    '-o', str(obj)], check=True)
    old = f'CMakeFiles/CoreGraphics.dir/{name}.{suffix}.o'
    if old in link:
        link[link.index(old)] = str(obj)
    else:
        link.append(str(obj))
link[link.index('-o') + 1] = str(root / 'build/riot-CoreGraphics')
subprocess.run(link, cwd=folder, check=True)
