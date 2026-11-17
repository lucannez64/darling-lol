#!/usr/bin/env python3
"""Build the changed X11 backend and CoreFoundation and compare AppKit's idle wait.

Runs tools/riot-appkit-idle-wait-check.m through Darling twice in an isolated
overlay of the per-user frameworks: with the polling wait
(RCLOL_APPKIT_PORT_WAIT=0) and with the Mach port wait. Needs prepared sources,
an existing CMake build, Darling, and the per-user runtime overlays. Installs
nothing, opens no windows and does not touch Riot or League.
"""
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
config = json.loads((root / 'build/config.json').read_text())
build = root / 'build/darling-src/build'
subprocess.run(['cmake', '--build', str(build), '--target', 'CoreFoundation_x86_64'], check=True,
               stdout=subprocess.DEVNULL)
subprocess.run(['python3', str(root / 'tools/build-riot-x11-runtime.py')], check=True,
               stdout=subprocess.DEVNULL)

appkit = build / 'src/external/cocotron/AppKit'
flags = {}
for line in (appkit / 'CMakeFiles/AppKit.dir/flags.make').read_text().splitlines():
    if line.startswith('C_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
probe = root / 'build/riot-appkit-idle-wait-check'
subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'], '-O2',
                '-fblocks', '-Wno-macro-redefined', '-c',
                str(root / 'tools/riot-appkit-idle-wait-check.m'), '-o', str(probe) + '.o'],
               check=True)
subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                '-isysroot', config['sdk'], str(probe) + '.o', '-lSystem', '-framework', 'AppKit',
                '-framework', 'Foundation', '-framework', 'CoreFoundation', '-lobjc',
                '-o', str(probe)], check=True)

guest_home = config['guest_home']
installed = Path(config['prefix']) / guest_home.lstrip('/') / 'RuntimeFrameworks'
failed = False
with tempfile.TemporaryDirectory(prefix='appkit-idle-wait-', dir=root / 'build') as directory:
    overlay = Path(directory)
    for name in ('AppKit', 'CoreFoundation'):
        shutil.copytree(installed / f'{name}.framework', overlay / f'{name}.framework',
                        symlinks=True, ignore=shutil.ignore_patterns('*.before-*'))
    shutil.copy2(root / 'build/riot-X11', overlay /
                 'AppKit.framework/Versions/C/Resources/Backends/X11.backend/Contents/MacOS/X11')
    shutil.copy2(build / 'src/external/corefoundation/CoreFoundation_x86_64',
                 overlay / 'CoreFoundation.framework/Versions/A/CoreFoundation')
    for wait in ('0', '1'):
        env = dict(os.environ,
                   DYLD_FRAMEWORK_PATH='/Volumes/SystemRoot' + str(overlay) + ':' + guest_home +
                   '/RuntimeFrameworks',
                   DYLD_LIBRARY_PATH=guest_home + '/RuntimeLibraries',
                   RCLOL_APPKIT_PORT_WAIT=wait)
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(['darling', 'shell', '/Volumes/SystemRoot' + str(probe)],
                                        env=env, stdout=output, stderr=subprocess.STDOUT,
                                        timeout=60)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
            output.seek(0)
            lines = [line.decode(errors='replace').rstrip() for line in output
                     if line.startswith(b'APPKIT_IDLE_WAIT')]
        label = 'polling' if wait == '0' else 'port wait'
        print(f'{label}: ' + ('\n'.join(lines) or f'no probe output (exit {code})'))
        failed |= code != 0 or not lines
raise SystemExit(1 if failed else 0)
