#!/usr/bin/env python3
"""Build and run the standalone SIOCGIFMEDIA probe inside Darling.

No Riot or League process is started. The probe only enumerates local
interfaces and issues SIOCGIFMEDIA, which is the call Vivox uses before
vx_req_connector_create.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'build/config.json'
SOURCE = ROOT / 'tools/riot-ifmedia-check.c'
OUTPUT = ROOT / 'build/riot-ifmedia-check'


def find_sdk():
    candidates = [
        ROOT / 'build/darling-src/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk',
        ROOT / 'build/darling-src/build/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk',
    ]
    for candidate in candidates:
        if (candidate / 'usr/include/sys/socket.h').is_file():
            return candidate
    raise SystemExit('MacOSX.sdk not found; run ./scripts/build.sh first')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guest-home', default=None,
                        help='guest home, e.g. /Users/example (default: build/config.json)')
    parser.add_argument('--library-path', default=None,
                        help='extra guest DYLD_LIBRARY_PATH entry, e.g. a rebuilt kernel overlay')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()

    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    guest_home = args.guest_home or config.get('guest_home')
    if not guest_home:
        guest_home = '/Users/' + os.environ.get('USER', '')
    sdk = find_sdk()

    clang = shutil.which('clang')
    if not clang:
        raise SystemExit('clang not found')
    OUTPUT.parent.mkdir(exist_ok=True)
    subprocess.run([
        clang, '-target', 'x86_64-apple-macosx11.0', '-D__DARWIN_UNIX03=0',
        '-fuse-ld=lld', '-nostdlib', '-isysroot', str(sdk),
        str(SOURCE), '-lSystem', '-o', str(OUTPUT),
    ], check=True)

    libraries = guest_home.rstrip('/') + '/RuntimeLibraries'
    if args.library_path:
        libraries = args.library_path.rstrip('/') + ':' + libraries
    guest_probe = '/Volumes/SystemRoot' + str(OUTPUT)
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'DISPLAY',
           'XAUTHORITY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'LANG') if key in os.environ}
    result = subprocess.run([
        'darling', 'shell', 'env', 'DYLD_LIBRARY_PATH=' + libraries,
        guest_probe,
    ], env=env, timeout=args.timeout)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
