#!/usr/bin/env python3
"""Bundle the diagnostics needed to explain a Darling/League crash or hang.

The bundle never starts or stops anything. It collects the host report from
`doctor.py`, the Riot/Darling process table, the tail of the runtime and
launcher logs, recent kernel messages and the last core dumps, and writes one
gzipped tarball that can be attached to an issue.

Home directory, user name and host name are replaced by placeholders by
default. Environment variables are not collected, and credentials in command
lines are masked.

Usage:
    python3 tools/crash-report.py
    python3 tools/crash-report.py --out ~/reports --prefix ~/.darling
    python3 tools/crash-report.py --no-redact --json
"""
import argparse
from collections import deque
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
# Credential names may be qualified, e.g. --riotclient-auth-token.
SENSITIVE = re.compile(
    r'(?i)(?:^|[-_])(?:pass(?:word|wd)?|token|secret|api[-_]?key|'
    r'auth(?:orization)?|credential|cookie|session|bearer)')
URL_USERINFO = re.compile(r'(://[^/:@\s]+:)[^/@\s]+@')


def read_tail(path, limit=LOG_TAIL_LINES):
    """Return the last lines without holding the whole log in memory."""
    try:
        with open(path, 'r', errors='replace') as handle:
            lines = deque(handle, maxlen=limit)
    except OSError as error:
        return None, str(error)
    return ''.join(lines), None


def mask_argv(arguments):
    """Replace credentials in a command line, without hiding the command itself."""
    masked = []
    hide_next = False
    for argument in arguments:
        if hide_next:
            masked.append('<redacted>')
            hide_next = False
            continue
        name, separator, _value = argument.partition('=')
        if SENSITIVE.search(name):
            if separator:
                masked.append(name + '=<redacted>')
                continue
            masked.append(argument)
            hide_next = True
            continue
        masked.append(URL_USERINFO.sub(r'\1<redacted>@', argument))
    return ' '.join(masked)


def command_arguments(proc):
    try:
        raw = (proc / 'cmdline').read_bytes()
    except OSError:
        return []
    return [part.decode(errors='replace') for part in raw.split(b'\0') if part]


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
            argv = mask_argv(command_arguments(entry)).strip()
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


def format_processes(rows, relevant, all_processes=False):
    lines = []
    if all_processes:
        lines.append('# all processes owned by this user, plus zombies')
        lines.append('pid\tppid\tstate\tcomm\targv')
        for row in rows:
            lines.append('%s\t%s\t%s\t%s\t%s' % (row['pid'], row['ppid'], row['state'], row['comm'], row['argv']))
        lines.append('')
    lines.append('# runtime, client and zombie processes')
    lines.append('pid\tppid\tstate\tcomm\targv')
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


CLIENT_FIELDS = ('address', 'mapped', 'hidden', 'at', 'size', 'workspace',
                 'floating', 'fullscreen', 'class', 'initialClass', 'pinned',
                 'grouped', 'swallowing', 'monitor')


def window_listing(output):
    """Keep geometry and class from hyprctl; drop titles, which are private."""
    try:
        clients = json.loads(output)
    except ValueError:
        return '# hyprctl clients output could not be parsed\n'
    if not isinstance(clients, list):
        return '# unexpected hyprctl clients output\n'
    visible = [
        {key: client[key] for key in CLIENT_FIELDS if key in client}
        for client in clients if isinstance(client, dict)
    ]
    return json.dumps(visible, indent=2) + '\n'


def display_state(which, env):
    if env.get('WAYLAND_DISPLAY') and which('hyprctl'):
        output, error = run_command(['hyprctl', 'monitors', '-j'])
        clients, client_error = run_command(['hyprctl', 'clients', '-j'])
        listing = window_listing(clients) if clients else '# unavailable: %s\n' % client_error
        return '# hyprctl monitors\n%s\n# hyprctl clients (titles omitted)\n%s' % (
            output or error, listing)
    if which('xrandr') and env.get('DISPLAY'):
        output, error = run_command(['xrandr', '--query'])
        if output:
            return output
    return '# no display tool available; DISPLAY=%r WAYLAND_DISPLAY=%r\n' % (
        env.get('DISPLAY'), env.get('WAYLAND_DISPLAY'))


def replace_token(text, value, placeholder):
    """Replace a whole word, so a short user name cannot hit unrelated text."""
    return re.sub(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(value),
                  placeholder, text)


def scrub(text, home=None, username=None, hostname=None):
    if not text:
        return text
    text = text.replace(str(home or Path.home()), '~')
    username = username or os.environ.get('USER') or ''
    if username:
        text = replace_token(text, username, '<user>')
    hostname = hostname or socket.gethostname()
    if hostname:
        text = replace_token(text, hostname, '<host>')
    return text


LABEL = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def bundle_name(label=None):
    if label is not None and (not LABEL.match(label) or label in ('.', '..')):
        raise ValueError('label may contain only letters, digits, dot, underscore and dash')
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime())
    return 'lol-report-%s%s.tar.gz' % (stamp, '-' + label if label else '')


def default_launch_log(env):
    """Launcher log written by desktop-run.sh, preferring the active workspace."""
    candidates = []
    workspace = env.get('DARLING_WORKSPACE')
    if workspace:
        candidates.append(Path(workspace) / 'build/desktop-launch.log')
    root = Path(__file__).resolve().parents[1]
    candidates.append(root / 'build/desktop-launch.log')
    candidates.append(root / 'build/lol-launch.log')
    return next((path for path in candidates if path.is_file()), candidates[0])


def build(prefix, out_dir, launch_log=None, label=None, redact=True, all_processes=False,
          proc_root=Path('/proc'), which=None, env=None):
    """Write the bundle and return its path and manifest."""
    env = dict(os.environ if env is None else env)
    which = which or shutil.which
    prefix = Path(prefix or env.get('DPREFIX') or (Path.home() / '.darling')).expanduser()
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = out_dir / bundle_name(label)
    launch_log = Path(launch_log).expanduser() if launch_log else default_launch_log(env)

    facts = doctor.gather(prefix=prefix, env=env, proc_root=proc_root, which=which)
    checks = doctor.run_checks(facts)
    rows = process_snapshot(proc_root)
    relevant = relevant_processes(rows)

    dserver_log = Path(prefix) / 'private/var/log/dserver.log'
    log_text, log_error = read_tail(dserver_log)
    launch_text, launch_error = read_tail(launch_log)

    entries = {
        'processes.txt': format_processes(rows, relevant, all_processes),
        'logs/dserver.log.tail': (log_text if log_text is not None
                                  else '# unavailable: %s\n' % log_error),
        'logs/launch.log.tail': (launch_text if launch_text is not None
                                 else '# unavailable: %s\n' % launch_error),
        'logs/kernel.txt': kernel_messages(which),
        'display.txt': display_state(which, env),
        'coredumps.txt': coredumps(which),
    }
    if redact:
        workspace = facts.get('workspace', '')

        def private(text):
            return scrub(doctor.redact_text(text, prefix, workspace), username=env.get('USER'))

        entries = {name: private(text) for name, text in entries.items()}
        checks = [
            dict(check, title=private(check['title']), message=private(check['message']))
            for check in checks
        ]

    manifest = {
        'schema': doctor.SCHEMA,
        'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'host': {key: facts['host'][key] for key in ('distro', 'kernel', 'arch')},
        'python': platform.python_version(),
        'prefix': '<prefix>' if redact else facts['prefix'],
        'redacted': redact,
        'archive': archive.name,
        'doctor': {'summary': doctor.summarize(checks), 'checks': checks},
        'processes': {
            'total': len(rows),
            'relevant': len(relevant),
            'zombies': sum(1 for row in rows if row['state'] == 'Z'),
            'included': 'all' if all_processes else 'runtime-client-zombies',
        },
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
                    add_private(tar, path, str(Path('lol-report') / path.relative_to(root)))

    os.chmod(archive, 0o600)
    return archive, manifest


def add_private(tar, path, name):
    """Add a file without this machine's uid, user name or timestamps."""
    info = tarfile.TarInfo(name)
    info.size = path.stat().st_size
    info.mode = 0o600
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    with open(path, 'rb') as handle:
        tar.addfile(info, handle)


def readme(manifest):
    return '''darling-lol crash report
========================

Created: %(created)s
Host:    %(distro)s, kernel %(kernel)s (%(arch)s)
Prefix:  %(prefix)s
Redacted: %(redacted)s

What is inside:
- report.json: the doctor checks, host summary and process counts
- processes.txt: the Darling runtime, Riot/League client and zombie processes;
  pass --all-processes to include every process owned by this user
- logs/dserver.log.tail: the last %(lines)d lines of the runtime log
- logs/launch.log.tail: the last lines the launcher wrote
- logs/kernel.txt: graphics-related kernel messages
- display.txt: monitors and windows (or an explanation when unavailable)
- coredumps.txt: recent systemd-coredump entries, if any

Environment variables are not collected. Credentials in command-line arguments
(--token, --password, URL userinfo) are masked. Home directory, user name and
host name are replaced with placeholders unless the bundle was created with
--no-redact. The archive is mode 0600 and carries no owner metadata.
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
    parser.add_argument('--label', help='short label added to the file name ([A-Za-z0-9._-])')
    parser.add_argument('--all-processes', action='store_true',
                        help='include every process owned by this user, not only the runtime/client tree')
    parser.add_argument('--no-redact', action='store_true', help='keep the real home path, user and host name')
    parser.add_argument('--json', action='store_true', help='print the manifest')
    args = parser.parse_args(argv)

    try:
        archive, manifest = build(
            prefix=args.prefix,
            out_dir=args.out,
            launch_log=args.launch_log,
            label=args.label,
            redact=not args.no_redact,
            all_processes=args.all_processes,
        )
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(manifest, indent=2))
        print('Report: %s (%s)' % (archive, human_size(archive.stat().st_size)), file=sys.stderr)
        return 0
    print('Report: %s (%s)' % (archive, human_size(archive.stat().st_size)))
    summary = manifest['doctor']['summary']
    print('%d ok, %d warning(s), %d failure(s); %d zombie process(es)' % (
        summary['ok'], summary['warn'], summary['fail'], manifest['processes']['zombies']))
    print('Attach this file to the issue; environment variables and command-line credentials are omitted.')
    return 0


def human_size(size):
    for unit in ('B', 'KiB', 'MiB'):
        if size < 1024:
            return '%.0f %s' % (size, unit)
        size /= 1024
    return '%.1f GiB' % size


if __name__ == '__main__':
    sys.exit(main())
