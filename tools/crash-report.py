#!/usr/bin/env python3
"""Bundle the diagnostics needed to explain a Darling/League crash or hang.

The bundle never starts or stops anything. It collects the host report from
`doctor.py`, the Riot/Darling process table, the tail of the runtime and
launcher logs, recent kernel messages and the last core dumps, and writes one
gzipped tarball that can be attached to an issue.

Home directory, user name and host name are replaced by placeholders by
default; environment variables are an allowlist, so tokens are never included.

Usage:
    python3 tools/crash-report.py
    python3 tools/crash-report.py --out ~/reports --prefix ~/.darling
    python3 tools/crash-report.py --no-redact --json
"""
import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import doctor  # noqa: E402  (same directory as this script)

LOG_TAIL_LINES = 4000
KERNEL_PATTERN = re.compile(
    r'i915|amdgpu|radeon|\bxe\b|drm|vulkan|GPU HANG|out of memory|oom-kill|segfault|traps:', re.I)
PROCESS_NAMES = (
    'darlingserver', 'mldr', 'launchd', 'RiotClientServices', 'LeagueClientUx',
    'LeagueClient', 'League of Legends', 'CrBrowserMain', 'CrGpuMain', 'CrRendererMain',
    'CrUtilityMain', 'wine', 'gamescope', 'Xwayland',
)


def read_tail(path, limit=LOG_TAIL_LINES):
    try:
        with open(path, 'r', errors='replace') as handle:
            lines = handle.readlines()
    except OSError as error:
        return None, str(error)
    return ''.join(lines[-limit:]), None


def process_snapshot(proc_root=Path('/proc')):
    """`ps`-like table for this user plus every zombie, without calling ps."""
    rows = []
    uid = os.getuid()
    for entry in Path(proc_root).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_text()
            # Field 22 onwards follows the last ')' because comm may contain spaces.
            rest = stat.rsplit(')', 1)[1].split()
            state = rest[0]
            ppid = int(rest[1])
            owner = entry.stat().st_uid
            if owner != uid and state != 'Z':
                continue
            comm = (entry / 'comm').read_text().strip()
            argv = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
        except (OSError, ValueError, IndexError):
            continue
        rows.append({
            'pid': int(entry.name),
            'ppid': ppid,
            'state': state,
            'comm': comm,
            'argv': argv[:300],
        })
    return sorted(rows, key=lambda row: (row['pid']))


def relevant_processes(rows):
    """Processes worth reading first: the runtime, the client and leftovers."""
    selected = []
    for row in rows:
        if row['state'] == 'Z':
            selected.append(row)
        elif any(name in row['comm'] or name in row['argv'] for name in PROCESS_NAMES):
            selected.append(row)
    return selected


def format_processes(rows, relevant):
    lines = ['# all processes owned by this user, plus zombies',
             'pid\tppid\tstate\tcomm\targv']
    for row in rows:
        lines.append('%s\t%s\t%s\t%s\t%s' % (row['pid'], row['ppid'], row['state'], row['comm'], row['argv']))
    lines.append('')
    lines.append('# runtime, client and zombie processes')
    for row in relevant:
        lines.append('%s\t%s\t%s\t%s\t%s' % (row['pid'], row['ppid'], row['state'], row['comm'], row['argv']))
    return '\n'.join(lines) + '\n'


def run_command(command, timeout=10):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)
    if result.returncode != 0:
        return (result.stdout or ''), (result.stderr or 'exit %d' % result.returncode).strip()
    return result.stdout, None


def kernel_messages(which):
    if not which('dmesg'):
        return '# dmesg is not installed; kernel messages are unavailable\n'
    output, error = run_command(['dmesg', '--ctime'])
    if error and not output:
        return '# dmesg is not readable: %s\n' % error
    lines = [line for line in output.splitlines() if KERNEL_PATTERN.search(line)]
    return '\n'.join(lines[-300:]) + '\n' if lines else '# no graphics-related kernel messages\n'


def coredumps(which):
    if not which('coredumpctl'):
        return '# coredumpctl is not installed\n'
    output, error = run_command(['coredumpctl', '--no-pager', 'list', '-n', '10'])
    if error and not output:
        return '# coredumpctl failed: %s\n' % error
    return output or '# no core dumps\n'


def display_state(which, env):
    if env.get('WAYLAND_DISPLAY') and which('hyprctl'):
        output, error = run_command(['hyprctl', 'monitors', '-j'])
        clients, client_error = run_command(['hyprctl', 'clients', '-j'])
        return '# hyprctl monitors\n%s\n# hyprctl clients\n%s\n%s' % (
            output or error, clients or client_error, '')
    if which('xrandr') and env.get('DISPLAY'):
        output, error = run_command(['xrandr', '--query'])
        if output:
            return output
    return '# no display tool available; DISPLAY=%r WAYLAND_DISPLAY=%r\n' % (
        env.get('DISPLAY'), env.get('WAYLAND_DISPLAY'))


def scrub(text, home=None, username=None, hostname=None):
    if not text:
        return text
    replacements = [
        (str(home or Path.home()), '~'),
        (username or os.environ.get('USER') or '', '<user>'),
        (hostname or socket.gethostname(), '<host>'),
    ]
    for value, placeholder in replacements:
        if value:
            text = text.replace(value, placeholder)
    return text


def bundle_name(label=None):
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime())
    return 'lol-report-%s%s.tar.gz' % (stamp, '-' + label if label else '')


def build(prefix, out_dir, launch_log=None, label=None, redact=True,
          proc_root=Path('/proc'), which=None, env=None):
    """Write the bundle and return its path and manifest."""
    env = dict(os.environ if env is None else env)
    which = which or shutil.which
    prefix = Path(prefix or env.get('DPREFIX') or (Path.home() / '.darling')).expanduser()
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / bundle_name(label)

    facts = doctor.gather(prefix=prefix, env=env, proc_root=proc_root, which=which)
    checks = doctor.run_checks(facts)
    rows = process_snapshot(proc_root)
    relevant = relevant_processes(rows)

    dserver_log = Path(prefix) / 'private/var/log/dserver.log'
    log_text, log_error = read_tail(dserver_log)
    launch_text, launch_error = (read_tail(launch_log) if launch_log else (None, 'no launcher log'))

    entries = {
        'processes.txt': format_processes(rows, relevant),
        'logs/dserver.log.tail': log_text or '# unavailable: %s\n' % log_error,
        'logs/launch.log.tail': launch_text or '# unavailable: %s\n' % launch_error,
        'logs/kernel.txt': kernel_messages(which),
        'display.txt': display_state(which, env),
        'coredumps.txt': coredumps(which),
    }
    if redact:
        entries = {name: scrub(text, username=env.get('USER')) for name, text in entries.items()}
        checks = [
            dict(check, title=scrub(check['title'], username=env.get('USER')),
                 message=scrub(check['message'], username=env.get('USER')))
            for check in checks
        ]

    manifest = {
        'schema': doctor.SCHEMA,
        'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'host': {key: facts['host'][key] for key in ('distro', 'kernel', 'arch')},
        'python': platform.python_version(),
        'prefix': '<prefix>' if redact else facts['prefix'],
        'redacted': redact,
        'doctor': {'summary': doctor.summarize(checks), 'checks': checks},
        'processes': {'total': len(rows), 'relevant': len(relevant), 'zombies': sum(1 for row in rows if row['state'] == 'Z')},
        'files': sorted(entries),
    }
    if redact:
        manifest['host']['distro'] = scrub(facts['host']['distro'], username=env.get('USER'))

    with tempfile.TemporaryDirectory(prefix='lol-report-') as temporary:
        root = Path(temporary)
        (root / 'logs').mkdir()
        (root / 'report.json').write_text(json.dumps(manifest, indent=2) + '\n')
        for name, text in entries.items():
            (root / name).write_text(text)
        (root / 'README.txt').write_text(readme(manifest))
        with tarfile.open(archive, 'w:gz') as tar:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    tar.add(path, arcname=str(Path('lol-report') / path.relative_to(root)))

    return archive, manifest


def readme(manifest):
    return '''darling-lol crash report
========================

Created: %(created)s
Host:    %(distro)s, kernel %(kernel)s (%(arch)s)
Prefix:  %(prefix)s
Redacted: %(redacted)s

What is inside:
- report.json: the doctor checks, host summary and process counts
- processes.txt: this user's processes plus every zombie, and the subset that is
  part of the Darling runtime or the Riot/League client
- logs/dserver.log.tail: the last %(lines)d lines of the runtime log
- logs/launch.log.tail: the last lines the launcher wrote
- logs/kernel.txt: graphics-related kernel messages
- display.txt: monitors and windows (or an explanation when unavailable)
- coredumps.txt: recent systemd-coredump entries, if any

Tokens, passwords and other environment secrets are not collected. Home
directory, user name and host name are replaced with placeholders unless the
bundle was created with --no-redact.
''' % {
        'created': manifest['created'],
        'distro': manifest['host']['distro'],
        'kernel': manifest['host']['kernel'],
        'arch': manifest['host']['arch'],
        'prefix': manifest['prefix'],
        'redacted': manifest['redacted'],
        'lines': LOG_TAIL_LINES,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Bundle Darling/League crash and hang diagnostics.')
    parser.add_argument('--prefix', type=Path, help='Darling prefix (default: DPREFIX or ~/.darling)')
    parser.add_argument('--out', type=Path,
                        default=Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'darling-lol/reports',
                        help='directory for the tarball (default: ~/.cache/darling-lol/reports)')
    parser.add_argument('--launch-log', type=Path, help='launcher log to include')
    parser.add_argument('--label', help='short label added to the file name')
    parser.add_argument('--no-redact', action='store_true', help='keep the real home path, user and host name')
    parser.add_argument('--json', action='store_true', help='print the manifest')
    args = parser.parse_args(argv)

    launch_log = args.launch_log
    if launch_log is None:
        candidate = Path(__file__).resolve().parents[1] / 'build/lol-launch.log'
        launch_log = candidate if candidate.is_file() else None

    archive, manifest = build(
        prefix=args.prefix,
        out_dir=args.out,
        launch_log=launch_log,
        label=args.label,
        redact=not args.no_redact,
    )
    if args.json:
        print(json.dumps(manifest, indent=2))
    print('Report: %s (%s)' % (archive, human_size(archive.stat().st_size)))
    summary = manifest['doctor']['summary']
    print('%d ok, %d warning(s), %d failure(s); %d zombie process(es)' % (
        summary['ok'], summary['warn'], summary['fail'], manifest['processes']['zombies']))
    print('Attach this file to the issue; it contains no tokens or environment secrets.')
    return 0


def human_size(size):
    for unit in ('B', 'KiB', 'MiB'):
        if size < 1024:
            return '%.0f %s' % (size, unit)
        size /= 1024
    return '%.1f GiB' % size


if __name__ == '__main__':
    sys.exit(main())
