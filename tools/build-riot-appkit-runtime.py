#!/usr/bin/env python3
"""Rebuild NSWindow's tracking dispatch against the configured AppKit build."""
from pathlib import Path
import shlex
import subprocess

project = Path(__file__).resolve().parent.parent
framework = project / 'build/darling-src/build/src/external/cocotron/AppKit'
flags = {}
for line in (framework / 'CMakeFiles/AppKit.dir/flags.make').read_text().splitlines():
    if line.startswith('C_') and ' = ' in line:
        name, value = line.split(' = ', 1)
        flags[name] = shlex.split(value)
link = shlex.split((framework / 'CMakeFiles/AppKit.dir/link.txt').read_text())
for name in ['NSResponder', 'NSApplication', 'NSWindow', 'NSDisplay', 'NSView', 'NSThemeFrame', 'NSTextInput', 'NSBitmapImageRep', 'NSSpellChecker', 'NSEvent.subproj/NSEvent', 'NSEvent.subproj/NSEvent_mouse']:
    obj = project / f'build/riot-{Path(name).name}.o'
    subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                    '-O3', '-march=native', '-mtune=generic', '-Wno-macro-redefined', '-c',
                    str(project / f'build/darling-src/src/external/cocotron/AppKit/{name}.m'),
                    '-o', str(obj)], check=True)
    link[link.index(f'CMakeFiles/AppKit.dir/{name}.m.o')] = str(obj)
link[link.index('-o') + 1] = str(project / 'build/riot-AppKit')
subprocess.run(link, cwd=framework, check=True)
for name in ['riot-event-coalescing-check', 'riot-scroll-event-check', 'riot-appkit-pump-check', 'riot-window-layout-check']:
    probe = project / f'build/{name}.o'
    subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                    '-O3', '-march=native', '-mtune=generic', '-fblocks', '-Wno-macro-redefined', '-c',
                    str(project / f'tools/{name}.m'), '-o', str(probe)], check=True)
    subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                    '-isysroot', '@DARLING_SDK@', str(probe), '-lSystem',
                    '-framework', 'AppKit', '-framework', 'Foundation', '-framework',
                    'CoreGraphics', '-lobjc', '-o', str(project / f'build/{name}')], check=True)
