#!/usr/bin/env python3
"""Build tools/riot-network-change-check.m against the configured SDK.

The probe asks whether Darling's SystemConfiguration raises a reachability or
dynamic-store notification on its own, which is what would make Chromium abort
an in-flight request with ERR_NETWORK_CHANGED. Run it inside the guest (the host path must be under the prefix):

    cp build/riot-network-change-check ~/.darling/Users/zero/netcheck
    darling "$HOME/.darling/Users/zero/netcheck"
"""
import json
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = 'riot-network-change-check'
# Reuse AppKit's configured flags: they include the Darwin headers and the
# Foundation/CoreFoundation include roots, without CFNetwork's forced prefixes.
FLAGS = ROOT / 'build/darling-src/build/src/external/cocotron/AppKit/CMakeFiles/AppKit.dir/flags.make'


def configuration():
    return json.loads((ROOT / 'build/config.json').read_text())


def main():
    config = configuration()
    flags = {}
    for line in FLAGS.read_text().splitlines():
        if line.startswith('C_') and ' = ' in line:
            key, value = line.split(' = ', 1)
            flags[key] = shlex.split(value)
    obj = ROOT / f'build/{NAME}.o'
    binary = ROOT / f'build/{NAME}'
    subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                    '-O2', '-fblocks', '-Wno-macro-redefined', '-c',
                    str(ROOT / f'tools/{NAME}.m'), '-o', str(obj)], check=True)
    subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                    '-isysroot', config['sdk'], str(obj),
                    '-lSystem', '-lobjc',
                    '-framework', 'SystemConfiguration', '-framework', 'CoreFoundation',
                    '-o', str(binary)], check=True)
    print(f'Built {binary}')
    print('Run inside the guest:')
    print('  cp %s ~/.darling/Users/zero/netcheck' % binary)
    print('  darling "$HOME/.darling/Users/zero/netcheck"')


if __name__ == '__main__':
    main()
