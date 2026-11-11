#!/usr/bin/env python3
"""Build/run the identity-consistency probe in a fresh Darling prefix.

Never touches the game prefix and launches no game or client: it compiles a
small Mach-O and runs it in a throwaway private prefix with the installed user
overlays, exactly like run-foundation-compat.py. Anomalies are expected on
Darling (that is the measurement), so this runner never fails on them; it fails
only if the probe produced no report.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
out = root / 'build/identity-consistency'
out.mkdir(parents=True, exist_ok=True)
flags = {}
flags_make = root / 'build/darling-src/build/src/external/cfnetwork/src/CMakeFiles/CFNetwork.dir/flags.make'
for line in flags_make.read_text().splitlines():
    if line.startswith('C_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
source = root / 'tests/security/probe/identity_consistency.m'
config_path = root / 'build/config.json'
config = json.loads(config_path.read_text()) if config_path.exists() else {}
sdk = config.get('sdk', '/usr/local/libexec/darling')
subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                '-c', str(source), '-o', str(out / 'probe.o')], check=True)
subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                '-isysroot', sdk, str(out / 'probe.o'), '-framework', 'Foundation',
                '-lobjc', '-lSystem', '-o', str(out / 'probe')], check=True)
private = Path(tempfile.mkdtemp(prefix='private-', dir=out))
env = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'XDG_RUNTIME_DIR',
       'DBUS_SESSION_BUS_ADDRESS', 'LANG') if k in os.environ}
env['DPREFIX'] = str(private / 'prefix')
runtime = Path.home() / '.darling/Users' / config.get('guest_user', os.environ['USER'])
command = ['darling', 'shell', 'env',
           'DYLD_FRAMEWORK_PATH=/Volumes/SystemRoot' + str(runtime / 'RuntimeFrameworks'),
           'DYLD_LIBRARY_PATH=/Volumes/SystemRoot' + str(runtime / 'RuntimeLibraries'),
           '/Volumes/SystemRoot' + str(out / 'probe')]
try:
    with (out / 'darling.log').open('w') as log:
        result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=60)
    text = (out / 'darling.log').read_text(errors='replace')
    start = text.find('{')
    if start < 0:
        raise RuntimeError('No JSON report; inspect build/identity-consistency/darling.log')
    report, _ = json.JSONDecoder().raw_decode(text[start:])
    report['collection'] = {'environment': 'Darling private prefix with installed user overlays',
                            'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    (out / 'darling.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if result.returncode:
        raise SystemExit('Probe process failed; inspect build/identity-consistency/darling.log')
finally:
    try:
        stopped = subprocess.run(['darling', 'shutdown'], env=env, capture_output=True, timeout=25)
        if stopped.returncode == 0:
            shutil.rmtree(private)
        else:
            print('Private prefix retained:', private)
    except (OSError, subprocess.SubprocessError):
        print('Private prefix retained:', private)
