#!/usr/bin/env python3
"""Check the process names Darling reports, in a throwaway private prefix.

MVG's tamper report names the parent process; Darling used to answer the Linux
comm, "mldr", for every guest process. This builds tests/security/probe/process_name.c
under a long executable name, runs it in a fresh private prefix (never the game
prefix, no client or game) with the installed user overlays, and checks every
name against macOS truncation rules:

  sysctl KERN_PROC p_comm         first MAXCOMLEN (16) characters
  pbsi_comm / pbi_comm            first MAXCOMLEN - 1 (15) characters
  pbi_name / proc_name()          first 2 * MAXCOMLEN - 1 (31) characters

and that neither the probe nor its parent is reported as "mldr".

--kernel PATH tests a libsystem_kernel.dylib that is not installed yet: the
installed RuntimeLibraries are copied into the build directory with PATH in place
of libsystem_kernel.dylib.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

MAXCOMLEN = 16
NAME = 'process-name-probe-long-executable'

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--kernel', type=Path, help='libsystem_kernel.dylib to test instead of the installed one')
args = parser.parse_args()

out = root / 'build/process-name-check'
out.mkdir(parents=True, exist_ok=True)
flags = {}
flags_make = root / 'build/darling-src/build/src/external/cfnetwork/src/CMakeFiles/CFNetwork.dir/flags.make'
for line in flags_make.read_text().splitlines():
    if line.startswith('C_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
config_path = root / 'build/config.json'
config = json.loads(config_path.read_text()) if config_path.exists() else {}
sdk = config.get('sdk', '/usr/local/libexec/darling')
probe = out / NAME
subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                '-c', str(root / 'tests/security/probe/process_name.c'), '-o', str(out / 'probe.o')], check=True)
subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                '-isysroot', sdk, str(out / 'probe.o'), '-lSystem', '-o', str(probe)], check=True)

runtime = Path.home() / '.darling/Users' / config.get('guest_user', os.environ['USER'])
libraries = runtime / 'RuntimeLibraries'
work = Path(tempfile.mkdtemp(prefix='run-', dir=out))
if args.kernel:
    libraries = work / 'RuntimeLibraries'
    shutil.copytree(runtime / 'RuntimeLibraries', libraries, symlinks=True)
    shutil.copy2(args.kernel, libraries / 'libsystem_kernel.dylib')

env = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'XDG_RUNTIME_DIR',
       'DBUS_SESSION_BUS_ADDRESS', 'LANG') if k in os.environ}
env['DPREFIX'] = str(work / 'prefix')
command = ['darling', 'shell', 'env',
           'DYLD_FRAMEWORK_PATH=/Volumes/SystemRoot' + str(runtime / 'RuntimeFrameworks'),
           'DYLD_LIBRARY_PATH=/Volumes/SystemRoot' + str(libraries),
           '/Volumes/SystemRoot' + str(probe)]
try:
    log_path = out / 'darling.log'
    with log_path.open('w') as log:
        result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120)
    text = log_path.read_text(errors='replace')
    # Two reports: the probe (parent = Darling's shell), then its spawned child (parent = the probe).
    reports, position = [], 0
    while len(reports) < 2:
        start = text.find('{', position)
        if start < 0:
            raise SystemExit(f'Expected two JSON reports; inspect {log_path}')
        decoded, length = json.JSONDecoder().raw_decode(text[start:])
        reports.append(decoded)
        position = start + length
    report, child = reports
    print(json.dumps({'probe': report, 'child': child}, indent=2))
    if result.returncode:
        raise SystemExit(f'Probe process failed; inspect {log_path}')
finally:
    # DPREFIX names the private prefix, so this stops only its darlingserver.
    try:
        stopped = subprocess.run(['darling', 'shutdown'], env=env, capture_output=True, timeout=25)
        if stopped.returncode == 0:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print('Private prefix retained:', work)
    except (OSError, subprocess.SubprocessError):
        print('Private prefix retained:', work)

expected = {
    'sysctl_p_comm': NAME[:MAXCOMLEN],
    'pbsi_comm': NAME[:MAXCOMLEN - 1],
    'pbi_comm': NAME[:MAXCOMLEN - 1],
    'pbi_name': NAME[:2 * MAXCOMLEN - 1],
    'proc_name': NAME[:2 * MAXCOMLEN - 1],
}
failures = [f'{label} {key}: {view[key]!r}, expected {value!r}'
            for label, view in (('self', report['self']), ('child self', child['self']),
                                ('child parent', child['parent']))
            for key, value in expected.items() if view[key] != value]
failures += [f'{label} {key} is "mldr"'
             for label, view in (('parent', report['parent']), ('child parent', child['parent']))
             for key in expected if view[key] == 'mldr']
for failure in failures:
    print('FAIL', failure)
if failures:
    sys.exit(1)
print('PASS: process names follow macOS rules; nothing reports "mldr"')
