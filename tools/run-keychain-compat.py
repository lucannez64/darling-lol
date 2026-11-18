#!/usr/bin/env python3
"""Build and run the software Keychain probe twice in one private prefix."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import uuid


def guest_path(path):
    return '/Volumes/SystemRoot' + str(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    framework_choice = parser.add_mutually_exclusive_group()
    framework_choice.add_argument('--security', type=Path, help='override the build-tree Security dylib')
    framework_choice.add_argument('--installed', action='store_true', help='test the installed SDK baseline')
    parser.add_argument('--output', type=Path, help='override the report directory')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = (args.output or root / 'build/keychain-compat').resolve()
    out.mkdir(parents=True, exist_ok=True)
    source = root / 'tests/security/probe/keychain_compat.m'
    config = json.loads((root / 'build/config.json').read_text())
    sdk = Path(config.get('sdk', '/usr/local/libexec/darling'))
    installed = sdk / 'System/Library/Frameworks/Security.framework/Versions/A/Security'
    built = root / 'build/darling-src/build/src/external/security/OSX/Security'
    security = installed if args.installed else (args.security or built).resolve()
    security_hash = hashlib.sha256(security.read_bytes()).hexdigest()
    flags_file = (root / 'build/darling-src/build/src/external/cfnetwork/src'
                  / 'CMakeFiles/CFNetwork.dir/flags.make')
    flags = {}
    for line in flags_file.read_text().splitlines():
        if line.startswith('C_') and ' = ' in line:
            name, value = line.split(' = ', 1)
            flags[name] = shlex.split(value)
    subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                    '-c', str(source), '-o', str(out / 'probe.o')], check=True)
    subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                    '-isysroot', str(sdk), str(out / 'probe.o'), '-framework', 'Security',
                    '-framework', 'Foundation', '-framework', 'CoreFoundation', '-lobjc',
                    '-lSystem', '-o', str(out / 'probe')], check=True)

    # The prefix is part of Darling's Unix socket path, which has a short limit.
    private = Path(tempfile.mkdtemp(prefix='darling-keychain-', dir='/tmp'))
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'XDG_RUNTIME_DIR',
           'DBUS_SESSION_BUS_ADDRESS', 'LANG') if k in os.environ}
    env['DPREFIX'] = str(private / 'prefix')
    command = ['darling', 'shell', 'env']
    if not args.installed:
        frameworks = private / 'Frameworks'
        framework = frameworks / 'Security.framework'
        destination = framework / 'Versions/A/Security'
        destination.parent.mkdir(parents=True)
        shutil.copy2(security, destination)
        (framework / 'Security').symlink_to('Versions/A/Security')
        command.append('DYLD_FRAMEWORK_PATH=' + guest_path(frameworks))
    command.append(guest_path(out / 'probe'))
    report = {'schema': 1, 'security': str(security),
              'security_sha256': security_hash,
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'runs': {}, 'failures': 0}
    identifier = uuid.uuid4().hex
    try:
        for mode in ('create', 'reopen'):
            logfile = out / f'{mode}.log'
            run = {'exit_code': None}
            try:
                with logfile.open('w') as log:
                    result = subprocess.run([*command, mode, identifier], env=env, stdout=log,
                                            stderr=subprocess.STDOUT, timeout=60)
                run['exit_code'] = result.returncode
                for line in logfile.read_text(errors='replace').splitlines():
                    if line.startswith('{'):
                        candidate = json.loads(line)
                        if candidate.get('probe') == 'keychain_compat' and candidate.get('mode') == mode:
                            run.update(candidate)
                            break
                if 'failures' not in run:
                    run['error'] = 'No probe report'
                elif not args.installed and run.get('security_image') != guest_path(destination):
                    run['error'] = 'Probe loaded a different Security framework'
                elif result.returncode != (1 if run['failures'] else 0):
                    run['error'] = 'Probe exit status disagrees with its report'
            except (subprocess.TimeoutExpired, OSError, ValueError) as error:
                run['error'] = str(error)
            report['runs'][mode] = run
            report['failures'] += run.get('failures', 0) + int('error' in run)
        create, reopen = (report['runs'][mode] for mode in ('create', 'reopen'))
        key_ids = create.get('key_ids', {})
        persistent = (len(key_ids) == 2 and len(set(key_ids.values())) == 2
                      and key_ids == reopen.get('key_ids') and create['pid'] != reopen['pid'])
        report['same_keys_across_processes'] = persistent
        report['failures'] += int(not persistent)
    finally:
        try:
            stopped = subprocess.run(['darling', 'shutdown'], env=env, capture_output=True,
                                     text=True, timeout=25)
            (out / 'shutdown.log').write_text(stopped.stdout + stopped.stderr)
            if stopped.returncode == 0 or 'Darling container is not running' in stopped.stderr:
                # Darling leaves an empty root-owned overlay work directory behind.
                shutil.rmtree(private, ignore_errors=True)
                leftovers = [path.name for path in private.iterdir()] if private.exists() else []
                if leftovers and leftovers != ['prefix.workdir']:
                    report['retained_private_prefix'] = str(private)
            else:
                report['retained_private_prefix'] = str(private)
        except (OSError, subprocess.SubprocessError) as error:
            report['retained_private_prefix'] = str(private)
            report['shutdown_error'] = str(error)
    rendered = json.dumps(report, indent=2) + '\n'
    (out / 'darling.json').write_text(rendered)
    print(rendered, end='')
    return int(report['failures'] != 0)


if __name__ == '__main__':
    raise SystemExit(main())
