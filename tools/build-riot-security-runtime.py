#!/usr/bin/env python3
"""Relink real Security with Darling's built-in requirement parser wired up."""
from pathlib import Path
import shlex
import shutil
import subprocess

root = Path(__file__).resolve().parent.parent
component = root / 'build/darling-src/build/src/external/security/OSX'
parser = component / 'libsecurity_codesigning'
flags = {}
for line in (parser / 'CMakeFiles/security_codesigning.dir/flags.make').read_text().splitlines():
    if line.startswith('CXX_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
# Preserve the archive member's basename when replacing it.
archive = root / 'build/riot-security/libsecurity_codesigning.a'
archive.parent.mkdir(exist_ok=True)
shutil.copy2(parser / 'libsecurity_codesigning.a', archive)
ar = shlex.split((parser / 'CMakeFiles/security_codesigning.dir/link.txt').read_text())[0]
for name in ['reqparser', 'StaticCode', 'SecStaticCode', 'SecRequirement', 'notarization']:
    obj = archive.parent / (name + '.cpp.o')
    subprocess.run(['clang++', *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'],
                    *flags['CXX_FLAGS'], '-c', str(root / ('build/darling-src/src/external/security/OSX/libsecurity_codesigning/lib/' + name + '.cpp')),
                    '-o', str(obj)], check=True)
    subprocess.run([ar, 'rs', str(archive), str(obj)], check=True)
link = shlex.split((component / 'CMakeFiles/Security_x86_64.dir/link.txt').read_text())
link[link.index('libsecurity_codesigning/libsecurity_codesigning.a')] = str(archive)
link[link.index('-o') + 1] = str(root / 'build/riot-Security')
subprocess.run(link, cwd=component, check=True)
