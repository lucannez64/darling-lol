#!/usr/bin/env python3
"""Check whether this host can run darling-lol, and write a report to attach to an issue.

The checks never start the game, the client or the runtime. They read host
information, the prefix and the environment, so the command is safe to run at
any time, including while a client is open.

Usage:
    python3 tools/doctor.py                 # human-readable summary
    python3 tools/doctor.py --json          # machine-readable report
    python3 tools/doctor.py --report FILE   # write the JSON report to FILE

Exit status is 1 when a required check fails; warnings do not fail.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 1
# League of Legends, the Riot Client and shader caches need room to grow. A
# checkout/build needs much more, but that is reported by the installer.
MIN_FREE_DISK_GIB = 5
WARN_FREE_DISK_GIB = 15
MIN_RAM_GIB = 4
WARN_RAM_GIB = 8
MIN_KERNEL = (5, 10)
# A report is public when it is attached to an issue, so only the variables
# that describe the runtime environment are collected.
ENV_ALLOWLIST = (
    'XDG_SESSION_TYPE',
    'XDG_CURRENT_DESKTOP',
    'WAYLAND_DISPLAY',
    'DISPLAY',
    'MANGOHUD',
    'DISABLE_MANGOHUD',
    'RCLOL_REGION',
    'RCLOL_LOCALE',
    'RCLOL_CDN_IPV4_ONLY',
    'RCLOL_RECOVER_STALE_CLIENT',
    'RCLOL_CLIENT_IN_GAME_TRICKS',
    'DPREFIX',
    'DARLING_WORKSPACE',
)


def read_text(path, default=''):
    try:
        return Path(path).read_text()
    except OSError:
        return default


def os_release(path='/etc/os-release'):
    values = {}
    for line in read_text(path).splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key] = value.strip().strip('"')
    return values


def meminfo(path='/proc/meminfo'):
    values = {}
    for line in read_text(path).splitlines():
        match = re.match(r'(\w+):\s+(\d+)', line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    return values


def gib(value):
    return round(value / (1024 ** 3), 1)


def kernel_tuple(release):
    numbers = re.findall(r'^\d+(?:\.\d+)*', release)
    if not numbers:
        return (0,)
    return tuple(int(part) for part in numbers[0].split('.'))


def read_gpus(sys_root=Path('/sys')):
    """Return one entry per DRM card with its driver and PCI identity."""
    cards = []
    drm = Path(sys_root) / 'class/drm'
    try:
        entries = sorted(drm.iterdir())
    except OSError:
        return cards
    for entry in entries:
        if not re.fullmatch(r'card\d+', entry.name):
            continue
        device = entry / 'device'
        driver = ''
        try:
            driver = (device / 'driver').resolve().name
        except OSError:
            pass
        vendor = read_text(device / 'vendor').strip()
        product = read_text(device / 'device').strip()
        name = ''
        for candidate in (device / 'product_name', entry / 'product_name'):
            name = read_text(candidate).strip()
            if name:
                break
        cards.append({
            'card': entry.name,
            'driver': driver,
            'vendor_id': vendor,
            'device_id': product,
            'name': name,
        })
    return cards


def vulkan_icds(env=None):
    """Vulkan ICD manifests the loader can see, including Nix's driver store."""
    env = env or os.environ
    search = [
        '/usr/share/vulkan/icd.d',
        '/usr/local/share/vulkan/icd.d',
        '/run/opengl-driver/share/vulkan/icd.d',
        '/etc/vulkan/icd.d',
        os.path.join(env.get('XDG_DATA_DIRS', ''), 'vulkan/icd.d') if env.get('XDG_DATA_DIRS') else None,
    ]
    found = []
    for folder in filter(None, search):
        try:
            names = sorted(p.name for p in Path(folder).glob('*.json'))
        except OSError:
            names = []
        for name in names:
            item = os.path.join(folder, name)
            if item not in found:
                found.append(item)
    return found


def prefix_state(prefix, proc_root=Path('/proc')):
    marker = Path(prefix) / '.init.pid'
    state = 'absent'
    pid = 0
    try:
        pid = int(marker.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid > 0:
        status = Path(proc_root) / str(pid) / 'status'
        text = read_text(status)
        current = re.search(r'^State:\s+(\S)', text, re.M)
        if not text:
            state = 'stale'
        elif current and current.group(1) == 'Z':
            state = 'stale'
        else:
            state = 'running'
    stale_markers = len(list(Path(prefix).glob('.init.pid.stale-*')))
    return {'pid': pid, 'state': state, 'stale_markers': stale_markers}


def gather(prefix=None, env=None, proc_root=Path('/proc'), sys_root=Path('/sys'), which=None):
    """Collect facts once; checks below only interpret them and stay testable."""
    env = dict(os.environ if env is None else env)
    which = which or shutil.which
    prefix = Path(prefix or env.get('DPREFIX') or (Path.home() / '.darling')).expanduser()

    release = os_release()
    memory = meminfo()
    total = memory.get('MemTotal', 0)
    swap = memory.get('SwapTotal', 0)
    try:
        free_disk = shutil.disk_usage(prefix if prefix.exists() else prefix.parent).free
    except OSError:
        free_disk = 0

    socket = prefix / '.darlingserver.sock'
    runtime_bin = Path('/usr/local/libexec/darling')
    return {
        'host': {
            'distro': release.get('PRETTY_NAME') or platform.system(),
            'kernel': platform.release(),
            'arch': platform.machine(),
            'python': platform.python_version(),
        },
        'session': {
            'type': env.get('XDG_SESSION_TYPE', ''),
            'desktop': env.get('XDG_CURRENT_DESKTOP', ''),
            'display': env.get('DISPLAY', ''),
            'wayland_display': env.get('WAYLAND_DISPLAY', ''),
        },
        'memory': {
            'total': total,
            'swap': swap,
            'total_gib': gib(total),
            'swap_gib': gib(swap),
        },
        'disk': {'free': free_disk, 'free_gib': gib(free_disk)},
        'gpus': read_gpus(sys_root),
        'vulkan_icds': vulkan_icds(env),
        'prefix': str(prefix),
        'prefix_exists': prefix.is_dir(),
        'prefix_launchd': (runtime_bin / 'sbin/launchd').is_file(),
        'prefix_socket': socket.exists(),
        'prefix_state': prefix_state(prefix, proc_root),
        'psr': read_text(sys_root / 'module/i915/parameters/enable_psr').strip(),
        'core_limit': _core_limit(),
        'environment': {key: env[key] for key in ENV_ALLOWLIST if key in env},
        'tools': {name: which(name) for name in ('xprop', 'xdpyinfo', 'vulkaninfo', 'dmesg', 'coredumpctl')},
    }


def _core_limit():
    try:
        import resource
        soft, _ = resource.getrlimit(resource.RLIMIT_CORE)
        return 'unlimited' if soft == resource.RLIM_INFINITY else str(soft)
    except (ImportError, OSError, ValueError):
        return 'unknown'


def check_kernel(facts):
    if kernel_tuple(facts['host']['kernel']) < MIN_KERNEL:
        return 'warn', 'Kernel %s is older than 5.10; Vulkan and DRM fixes may be missing' % facts['host']['kernel']
    return 'ok', 'Kernel %s' % facts['host']['kernel']


def check_memory(facts):
    total = facts['memory']['total']
    if total and total < MIN_RAM_GIB * 1024 ** 3:
        return 'fail', 'Only %s GiB RAM; the client and game need more' % facts['memory']['total_gib']
    if total and total < WARN_RAM_GIB * 1024 ** 3:
        return 'warn', '%s GiB RAM is tight; close other applications' % facts['memory']['total_gib']
    return 'ok', '%s GiB RAM, %s GiB swap' % (facts['memory']['total_gib'], facts['memory']['swap_gib'])


def check_disk(facts):
    free = facts['disk']['free']
    if free < MIN_FREE_DISK_GIB * 1024 ** 3:
        return 'fail', 'Only %s GiB free; the client needs a few GiB plus patches' % facts['disk']['free_gib']
    if free < WARN_FREE_DISK_GIB * 1024 ** 3:
        return 'warn', '%s GiB free; large patches and shader caches need room' % facts['disk']['free_gib']
    return 'ok', '%s GiB free' % facts['disk']['free_gib']


def check_gpu(facts):
    gpus = facts['gpus']
    if not gpus:
        return 'fail', 'No DRM card found; Darling needs a GPU with Vulkan'
    described = []
    for gpu in gpus:
        driver = gpu['driver'] or 'unknown driver'
        name = gpu['name'] or 'GPU %s:%s' % (gpu['vendor_id'], gpu['device_id'])
        described.append('%s (%s)' % (name, driver))
    if any(gpu['driver'] == 'nouveau' for gpu in gpus):
        return 'warn', ', '.join(described) + '; nouveau has limited Vulkan support'
    return 'ok', ', '.join(described)


def check_vulkan(facts):
    icds = facts['vulkan_icds']
    if not icds:
        return 'fail', 'No Vulkan ICD found; install the Mesa Vulkan driver (vulkan-radeon or vulkan-intel)'
    names = ', '.join(Path(item).name for item in icds)
    return 'ok', names


def check_session(facts):
    session = facts['session']
    kind = (session['type'] or '').lower()
    if kind == 'wayland' and not session['display']:
        return 'fail', 'Wayland session without DISPLAY; the client needs Xwayland'
    if kind == 'wayland':
        return 'ok', 'Wayland through Xwayland (%s)' % session['display']
    if session['display']:
        return 'ok', 'X11 (%s)' % session['display']
    return 'warn', 'No X11 DISPLAY; start the launcher from a desktop session'


def check_hud(facts):
    environment = facts['environment']
    if environment.get('DISABLE_MANGOHUD') == '1':
        return 'ok', 'MangoHud layer disabled for this launcher'
    if environment.get('MANGOHUD'):
        return 'fail', ('MANGOHUD is set; the overlay reads host /proc entries with guest PIDs and can abort '
                        'the client (issue #38). Unset it and set DISABLE_MANGOHUD=1')
    return 'ok', 'No inherited MangoHud layer'


def check_core_dumps(facts):
    if facts['core_limit'] == 'unlimited':
        return 'warn', 'Core dumps are unlimited; a crashing guest can fill the disk'
    return 'ok', 'Core dumps are bounded'


def check_psr(facts):
    if facts['psr'] == '1':
        return 'warn', ('i915 panel self-refresh is on; this can flicker or hitch the client. '
                        'Set options i915 enable_psr=0 in /etc/modprobe.d and reboot')
    return 'ok', 'Panel self-refresh is off or not applicable'


def check_prefix(facts):
    if not facts['prefix_exists']:
        return 'fail', 'Prefix %s does not exist; run the installer first' % facts['prefix']
    if not facts['prefix_launchd']:
        return 'fail', 'Installed launchd is missing; the runtime install did not complete'
    return 'ok', facts['prefix']


def check_runtime(facts):
    state = facts['prefix_state']
    if state['state'] == 'running':
        return 'ok', 'darlingserver is running (PID %s)' % state['pid']
    if state['state'] == 'stale':
        return 'warn', 'Stale darlingserver PID marker; the next launch recovers it'
    return 'ok', 'Runtime is stopped'


def check_stale_markers(facts):
    count = facts['prefix_state']['stale_markers']
    if count > 3:
        return 'warn', '%d stale runtime markers in the prefix; safe to delete' % count
    return 'ok', 'No stale runtime markers'


CHECKS = (
    ('kernel', 'Kernel', check_kernel),
    ('memory', 'Memory', check_memory),
    ('disk', 'Disk space', check_disk),
    ('gpu', 'GPU driver', check_gpu),
    ('vulkan', 'Vulkan driver', check_vulkan),
    ('session', 'Display session', check_session),
    ('hud', 'Overlay layer', check_hud),
    ('core', 'Core dumps', check_core_dumps),
    ('psr', 'Panel self-refresh', check_psr),
    ('prefix', 'Darling prefix', check_prefix),
    ('runtime', 'Darling runtime', check_runtime),
    ('markers', 'Runtime markers', check_stale_markers),
)


def run_checks(facts):
    results = []
    for key, title, check in CHECKS:
        try:
            status, message = check(facts)
        except Exception as error:  # one broken probe must not hide the rest
            status, message = 'warn', 'Check could not run: %s' % error
        results.append({'id': key, 'title': title, 'status': status, 'message': message})
    return results


def summarize(results):
    counts = {'ok': 0, 'warn': 0, 'fail': 0}
    for result in results:
        counts[result['status']] = counts.get(result['status'], 0) + 1
    return counts


def report(facts, results):
    return {
        'schema': SCHEMA,
        'tool': 'darling-lol doctor',
        'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'summary': summarize(results),
        'host': facts['host'],
        'session': facts['session'],
        'memory': facts['memory'],
        'disk': facts['disk'],
        'gpus': facts['gpus'],
        'vulkan_icds': facts['vulkan_icds'],
        'prefix': facts['prefix'],
        'environment': facts['environment'],
        'checks': results,
    }


def render(facts, results):
    lines = []
    host = facts['host']
    lines.append('darling-lol doctor - %s' % host['distro'])
    lines.append('  kernel %s  python %s  %s' % (host['kernel'], host['python'], host['arch']))
    results_by_id = {result['id']: result for result in results}
    for key in ('gpu', 'vulkan', 'session', 'memory', 'disk'):
        lines.append('  %-8s %s' % (results_by_id[key]['title'] + ':', results_by_id[key]['message']))
    lines.append('')
    for result in results:
        if result['status'] == 'ok':
            continue
        lines.append('%-4s %s: %s' % (result['status'].upper(), result['title'], result['message']))
    counts = summarize(results)
    if counts['fail'] or counts['warn']:
        lines.append('')
    lines.append('%d ok, %d warning(s), %d failure(s)' % (counts['ok'], counts['warn'], counts['fail']))
    if counts['fail']:
        lines.append('Fix the failures above before reporting other problems.')
    lines.append('Attach a full report with: darling-lol --doctor --report doctor.json')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check this host for darling-lol and write an attachable report.')
    parser.add_argument('--json', action='store_true', help='print the report as JSON')
    parser.add_argument('--report', type=Path, help='write the JSON report to this file')
    parser.add_argument('--prefix', type=Path, help='Darling prefix (default: DPREFIX or ~/.darling)')
    args = parser.parse_args(argv)

    facts = gather(prefix=args.prefix)
    results = run_checks(facts)
    document = report(facts, results)

    if args.report:
        args.report.write_text(json.dumps(document, indent=2) + '\n')
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        print(render(facts, results))
        if args.report:
            print('Report written to %s' % args.report)
    return 1 if document['summary']['fail'] else 0


if __name__ == '__main__':
    sys.exit(main())
