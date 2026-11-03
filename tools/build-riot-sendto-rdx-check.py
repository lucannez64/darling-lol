#!/usr/bin/env python3
"""Build tools/riot-sendto-rdx-check.c against the configured SDK.

The probe prints the %rdx secondary return register after sendto(), send()
and write() inside the guest, so it can be compared with the host. Chromium
reports ERR_NETWORK_CHANGED (-21) when that register is nonzero after a
successful send, which is what the 142 client's local UI load and every TLS
handshake hit before the trampoline cleared it. Run it inside the guest:

    cp build/riot-sendto-rdx-check ~/.darling/Users/zero/rdxcheck
    darling ~/.darling/Users/zero/rdxcheck
"""
import json
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = 'riot-sendto-rdx-check'
FLAGS = ROOT / 'build/darling-src/build/src/external/cocotron/AppKit/CMakeFiles/AppKit.dir/flags.make'


def main():
    config = json.loads((ROOT / 'build/config.json').read_text())
    flags = {}
    for line in FLAGS.read_text().splitlines():
        if line.startswith('C_') and ' = ' in line:
            key, value = line.split(' = ', 1)
            flags[key] = shlex.split(value)
    # AppKit force-includes Foundation headers, which need Objective-C; this is
    # a plain C probe, so drop those pairs.
    cflags, skip = [], False
    for token in flags['C_FLAGS']:
        if skip:
            skip = False
            continue
        if token == '-include':
            skip = True
            continue
        cflags.append(token)
    obj = ROOT / f'build/{NAME}.o'
    binary = ROOT / f'build/{NAME}'
    subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *cflags,
                    '-O2', '-Wno-macro-redefined', '-c',
                    str(ROOT / f'tools/{NAME}.c'), '-o', str(obj)], check=True)
    subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                    '-isysroot', config['sdk'], str(obj), '-lSystem', '-o', str(binary)], check=True)
    print(f'Built {binary}')


if __name__ == '__main__':
    main()
