#!/usr/bin/env python3
"""Build the standalone NSURLSession probe against a configured Darling tree."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--build-dir', type=Path, default=root / 'build/darling-src/build')
parser.add_argument('--sdk', type=Path)
args = parser.parse_args()
config_file = root / 'build/config.json'
config = json.loads(config_file.read_text()) if config_file.exists() else {}
sdk = args.sdk or Path(config.get('sdk', '/usr/local/libexec/darling'))
flags = {}
flags_file = args.build_dir / 'src/external/cfnetwork/src/CMakeFiles/CFNetwork.dir/flags.make'
for line in flags_file.read_text().splitlines():
    if line.startswith('C_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
output = root / 'build'
output.mkdir(exist_ok=True)
obj = output / 'riot-urlsession-check.o'
subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                '-c', str(root / 'tools/riot-urlsession-check.m'), '-o', str(obj)], check=True)
subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                '-isysroot', str(sdk), str(obj), '-framework', 'Foundation',
                '-framework', 'CFNetwork', '-lobjc', '-lSystem',
                '-o', str(output / 'riot-urlsession-check')], check=True)
print('Built', output / 'riot-urlsession-check')
