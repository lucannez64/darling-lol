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
import errno
import importlib.util
import json
import os
import platform
import re
import shutil
import struct
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
MIN_KERNEL = (5, 14)
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
    'RCLOL_HIDE_LEAGUE_SPLASH',
    'RCLOL_WAYLAND',
    'DPREFIX',
    'DARLING_WORKSPACE',
    # PRIME render offload (docs/TROUBLESHOOTING.md, hybrid graphics laptops).
    '__NV_PRIME_RENDER_OFFLOAD',
    '__VK_LAYER_NV_optimus',
    '__GLX_VENDOR_LIBRARY_NAME',
    'DRI_PRIME',
)
# Variables that only add tracing. Left set, they slow the client or change its
# behaviour (the input trace once made the game see stray clicks), so the
# doctor names them; their values are never collected.
TRACE_ENV = re.compile(r'^(?:RCLOL_[A-Z0-9_]*(?:TRACE|DEBUG|DUMP)[A-Z0-9_]*|DARLING_METAL_DEVICE_LOG)$')
# Opt-in switch files. Guest files live in the prefix's /private/tmp, which
# Darling empties when it starts; host files are read by the game process at
# start (docs/SETUP.md, "Game-process switches").
GUEST_DIAGNOSTIC_SWITCHES = ('rclol-input-trace', 'metal-debug-waits', 'metal-gpu')
GUEST_TUNING_SWITCHES = (
    'metal-present-dispatch-wait', 'metal-deferred-blit', 'metal-worker-lease',
    'metal-skip-cpu-devices', 'metal-all-devices', 'metal-no-drawable-vram',
    'metal-no-lazy-wakeup', 'metal-submit-batch-us', 'metal-submit-batch-max',
    'metal-texture-timelines', 'metal-no-texture-timelines',
)
HOST_TUNING_SWITCHES = (
    'rclol-no-local-mutex', 'rclol-no-local-condvars', 'rclol-no-local-semaphores',
    'rclol-core-isolation', 'rclol-region-pace-us', 'rclol-rpc-spin-us',
)
DISCRETE_DRIVERS = ('nvidia', 'nouveau')
INTEGRATED_DRIVERS = ('i915', 'xe', 'amdgpu', 'radeon')


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


def read_gpus(sys_root=Path('/sys'), proc_root=Path('/proc')):
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
        if not name and vendor == '0x10de':
            uevent = read_text(device / 'uevent')
            slot_match = re.search(r'^PCI_SLOT_NAME=(\S+)', uevent, re.M)
            if slot_match:
                info = read_text(Path(proc_root) / 'driver/nvidia/gpus' / slot_match.group(1) / 'information')
                model_match = re.search(r'^Model:\s+(.+)', info, re.M)
                if model_match:
                    name = model_match.group(1).strip()
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
        *[os.path.join(value, 'vulkan/icd.d')
          for value in (env.get('XDG_DATA_DIRS') or '').split(':') if value],
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


def runtime_dir(env=None):
    """Installed Darling runtime: workspace/Nix first, then the native path."""
    env = os.environ if env is None else env
    candidates = []
    workspace = env.get('DARLING_WORKSPACE')
    if workspace:
        candidates.append(Path(workspace) / 'usr-local/libexec/darling')
        config = Path(workspace) / 'build/config.json'
        try:
            configured = json.loads(config.read_text()).get('sdk')
        except (OSError, ValueError, AttributeError):
            configured = None
        if configured:
            candidates.append(Path(configured))
    candidates.append(Path('/usr/local/libexec/darling'))
    for candidate in candidates:
        if (candidate / 'sbin/launchd').is_file():
            return candidate
    return next((candidate for candidate in candidates if candidate.is_dir()), candidates[0])


# Built by the runtime build when the host Wayland packages exist (docs/SETUP.md).
WAYLAND_LIBRARY = Path('build/libdarling-wayland.so')


def wayland_socket_path(env):
    """The socket rule of tools/run-riot-client.sh: an absolute WAYLAND_DISPLAY, else XDG_RUNTIME_DIR/name."""
    display = env.get('WAYLAND_DISPLAY', '')
    if display.startswith('/'):
        return display
    return '%s/%s' % (env.get('XDG_RUNTIME_DIR', ''), display or 'wayland-0')


def wayland_library(env, here=None):
    """Where the runtime build puts libdarling-wayland.so: the workspace, or the checkout around this file."""
    here = Path(here or __file__).resolve()
    roots = [Path(env['DARLING_WORKSPACE'])] if env.get('DARLING_WORKSPACE') else []
    # tools/doctor.py -> checkout (parents[1]); build/runtime/tools/doctor.py -> checkout (parents[3]).
    roots += [parent for index, parent in enumerate(here.parents) if index in (1, 3)]
    for root in roots:
        if (root / WAYLAND_LIBRARY).is_file():
            return root / WAYLAND_LIBRARY
    return (roots[0] if roots else here.parent) / WAYLAND_LIBRARY


def wayland_facts(env):
    socket = wayland_socket_path(env)
    library = wayland_library(env)
    return {
        'requested': env.get('RCLOL_WAYLAND', ''),
        'session_type': env.get('XDG_SESSION_TYPE', ''),
        'socket': socket,
        'socket_found': bool(socket) and Path(socket).is_socket(),
        'library': str(library),
        'library_found': library.is_file(),
    }


def active_switches(prefix, host_tmp=Path('/tmp')):
    """Names of the opt-in switch files that exist now, by kind."""
    guest_tmp = Path(prefix) / 'private/tmp'

    def present(folder, names):
        return [name for name in names if (folder / name).exists()]

    return {
        'diagnostic': present(guest_tmp, GUEST_DIAGNOSTIC_SWITCHES),
        'tuning': present(guest_tmp, GUEST_TUNING_SWITCHES) + present(host_tmp, HOST_TUNING_SWITCHES),
    }


PERF_EVENT_OPEN_X86_64 = 298


def probe_perf_event_open():
    """Open and close one disabled, user-only software event on this thread.

    The cpuid alignment of profile=compat needs perf_event_open. Besides
    kernel.perf_event_paranoid, a seccomp filter (Docker, Podman and Flatpak
    defaults) or an LSM can refuse it, which only a real call shows. Returns
    'ok', the errno name, or 'unavailable' where the probe cannot run.
    """
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        return 'unavailable'
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
    except (ImportError, OSError):
        return 'unavailable'
    # struct perf_event_attr, PERF_ATTR_SIZE_VER0: PERF_TYPE_SOFTWARE,
    # PERF_COUNT_SW_DUMMY; flags disabled | exclude_kernel | exclude_hv.
    attr = bytearray(64)
    struct.pack_into('<IIQ', attr, 0, 1, 64, 9)
    struct.pack_into('<Q', attr, 40, 1 | 1 << 5 | 1 << 6)
    buffer = (ctypes.c_char * len(attr)).from_buffer(attr)
    libc.syscall.restype = ctypes.c_long
    fd = libc.syscall(ctypes.c_long(PERF_EVENT_OPEN_X86_64), buffer, ctypes.c_int(0), ctypes.c_int(-1),
                      ctypes.c_int(-1), ctypes.c_ulong(8))  # PERF_FLAG_FD_CLOEXEC
    if fd >= 0:
        os.close(fd)
        return 'ok'
    code = ctypes.get_errno()
    return errno.errorcode.get(code, str(code))


def _platform_tool():
    """configure-darling-platform.py next to this file (source or rendered copy), or None."""
    path = Path(__file__).resolve().parent / 'configure-darling-platform.py'
    try:
        spec = importlib.util.spec_from_file_location('darling_lol_platform', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError, SyntaxError, AttributeError):
        return None


def platform_identity_state(prefix):
    """'missing', 'shared', 'saved' or 'unreadable' for the prefix's saved platform identity."""
    etc = Path(prefix) / 'private/etc'
    files = [etc / 'darling-platform.plist', etc / 'darling-smbios.conf']
    present = [path for path in files if path.is_file()]
    if not present:
        return 'missing'
    tool = _platform_tool()
    if tool is None:
        return 'unreadable'
    try:
        if any(tool.has_known_defaults(tool.load(path)) for path in present):
            return 'shared'
    except Exception:  # a malformed file is reported, not raised
        return 'unreadable'
    return 'saved'


def trust_store(env):
    """The runtime signing trust store sign-darling-runtime.sh writes, or ''."""
    candidates = []
    if env.get('DARLING_TRUST_DIR'):
        candidates.append(Path(env['DARLING_TRUST_DIR']) / 'trustcache')
    config = Path(env['XDG_CONFIG_HOME']) if env.get('XDG_CONFIG_HOME') else (
        Path(env['HOME']) / '.config' if env.get('HOME') else None)
    if config is not None:
        candidates.append(config / 'darling/trustcache')
    candidates.append(Path('/etc/darling/trustcache'))
    return next((str(path) for path in candidates if path.exists()), '')


NVIDIA_EGL_LIBDIRS = ('usr/lib64', 'usr/lib/x86_64-linux-gnu', 'usr/lib')


def nvidia_egl_location(marker=Path('/run/.toolboxenv'), container_root=Path('/'),
                        host_root=Path('/run/host')):
    """Where libEGL_nvidia.so.0 lives inside Toolbox: 'container', 'host' or 'missing'.

    Outside Toolbox this is 'not-toolbox'. The launcher writes absolute-path EGL manifests
    when the library is only on the host side (issue #361).
    """
    if not Path(marker).exists():
        return 'not-toolbox'
    for place, root in (('container', Path(container_root)), ('host', Path(host_root))):
        if any((root / libdir / 'libEGL_nvidia.so.0').exists() for libdir in NVIDIA_EGL_LIBDIRS):
            return place
    return 'missing'


def rootless_namespace(proc_root=Path('/proc')):
    """True in a rootless user namespace (Toolbox, the Nix runtime).

    darlingserver reads the same first uid_map line and ignores every trust
    store there, because the host user could rewrite it.
    """
    fields = read_text(Path(proc_root) / 'self/uid_map').split()
    try:
        inside, outside, count = (int(field) for field in fields[:3])
    except ValueError:
        return False
    return inside == 0 and outside != 0 and count != 0


def configured_prefix(env, roots=None):
    """The prefix the launcher uses: $DPREFIX, then build/config.json, then ~/.darling.

    `darling-lol --doctor` passes no prefix, and a prefix moved with --set-prefix
    is only recorded in build/config.json, so reading ~/.darling alone would
    inspect a stale or empty prefix.
    """
    if env.get('DPREFIX'):
        return Path(env['DPREFIX'])
    if roots is None:
        roots = []
        if env.get('DARLING_WORKSPACE'):
            roots.append(Path(env['DARLING_WORKSPACE']))
        # tools/doctor.py or the rendered build/runtime/tools/doctor.py
        roots.extend(Path(__file__).resolve().parents)
    for root in roots:
        config = Path(root) / 'build/config.json'
        try:
            prefix = json.loads(config.read_text()).get('prefix')
        except (OSError, ValueError, AttributeError):
            continue
        if prefix:
            return Path(prefix)
    return Path.home() / '.darling'


def gather(prefix=None, env=None, proc_root=Path('/proc'), sys_root=Path('/sys'), which=None,
           host_tmp=Path('/tmp'), perf_probe=probe_perf_event_open):
    """Collect facts once; checks below only interpret them and stay testable."""
    env = dict(os.environ if env is None else env)
    which = which or shutil.which
    prefix = Path(prefix or configured_prefix(env)).expanduser()

    release = os_release()
    memory = meminfo()
    total = memory.get('MemTotal', 0)
    swap = memory.get('SwapTotal', 0)
    try:
        free_disk = shutil.disk_usage(prefix if prefix.exists() else prefix.parent).free
    except OSError:
        free_disk = 0

    socket = prefix / '.darlingserver.sock'
    runtime_bin = runtime_dir(env)
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
        'gpus': read_gpus(sys_root, proc_root),
        'vulkan_icds': vulkan_icds(env),
        'nvidia_egl': nvidia_egl_location(),
        'prefix': str(prefix),
        'workspace': env.get('DARLING_WORKSPACE', ''),
        'runtime_dir': str(runtime_bin),
        'prefix_exists': prefix.is_dir(),
        'prefix_launchd': (runtime_bin / 'sbin/launchd').is_file(),
        'prefix_socket': socket.exists(),
        'prefix_state': prefix_state(prefix, proc_root),
        'psr': read_text(sys_root / 'module/i915/parameters/enable_psr').strip(),
        'perf_event_paranoid': read_text(proc_root / 'sys/kernel/perf_event_paranoid').strip(),
        'perf_event_open': perf_probe(),
        'platform_identity': platform_identity_state(prefix) if prefix.is_dir() else 'missing',
        'trust_store': trust_store(env),
        'rootless_namespace': rootless_namespace(proc_root),
        'core_limit': _core_limit(),
        'environment': {key: env[key] for key in ENV_ALLOWLIST if key in env},
        'trace_env': sorted(key for key, value in env.items() if value and TRACE_ENV.match(key)),
        'switches': active_switches(prefix, host_tmp),
        'wayland': wayland_facts(env),
        'tools':{name: which(name) for name in ('xprop', 'xdpyinfo', 'vulkaninfo', 'dmesg', 'coredumpctl')},
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


def check_hybrid(facts):
    drivers = {gpu['driver'] for gpu in facts['gpus']}
    if not (drivers & set(DISCRETE_DRIVERS) and drivers & set(INTEGRATED_DRIVERS)):
        return 'ok', 'Single GPU vendor'
    environment = facts['environment']
    offload = environment.get('__NV_PRIME_RENDER_OFFLOAD') == '1' or environment.get('DRI_PRIME')
    guide = 'see docs/TROUBLESHOOTING.md, "Hybrid graphics laptops"'
    if 'metal-gpu' in facts.get('switches', {}).get('diagnostic', ()):
        return 'warn', ('/tmp/metal-gpu moves only Vulkan; with two GPUs, OpenGL stays on the other one and '
                        'frames cannot be presented. Remove it and use PRIME offload; ' + guide)
    if not offload:
        return 'warn', ('Hybrid graphics without PRIME offload in this environment: Vulkan and OpenGL can '
                        'land on different GPUs (black window or stuck at 93%%). If you start the launcher '
                        'with prime-run, run the doctor the same way; %s' % guide)
    if environment.get('__NV_PRIME_RENDER_OFFLOAD') == '1' and (
            environment.get('__VK_LAYER_NV_optimus') != 'NVIDIA_only'
            or environment.get('__GLX_VENDOR_LIBRARY_NAME') != 'nvidia'):
        return 'warn', ('PRIME offload is only partly set: Vulkan and OpenGL need __VK_LAYER_NV_optimus='
                        'NVIDIA_only and __GLX_VENDOR_LIBRARY_NAME=nvidia too (prime-run sets all three); '
                        + guide)
    return 'ok', 'Hybrid graphics with PRIME offload'


def check_switches(facts):
    switches = facts.get('switches', {})
    diagnostic = [('/tmp/' + name) for name in switches.get('diagnostic', ())]
    diagnostic += facts.get('trace_env', [])
    tuning = [('/tmp/' + name) for name in switches.get('tuning', ())]
    if diagnostic:
        return 'warn', ('Diagnostic switches are on: %s. They add tracing and can slow or change input; '
                        'remove them unless you are collecting a trace' % ', '.join(diagnostic))
    if tuning:
        return 'warn', ('Non-default tuning files are present: %s. Remove them to compare against the '
                        'defaults (docs/SETUP.md)' % ', '.join(tuning))
    return 'ok', 'No diagnostic or tuning switches'


def check_vulkan(facts):
    icds = facts['vulkan_icds']
    if not icds:
        return 'fail', 'No Vulkan ICD found; install the Mesa Vulkan driver (vulkan-radeon or vulkan-intel)'
    names = ', '.join(Path(item).name for item in icds)
    return 'ok', names


def check_toolbox_nvidia_egl(facts):
    location = facts.get('nvidia_egl', 'not-toolbox')
    if location == 'not-toolbox':
        return 'ok', 'Not inside Toolbox'
    if not any(gpu.get('vendor_id') == '0x10de' for gpu in facts['gpus']):
        return 'ok', 'No NVIDIA GPU'
    guide = 'see docs/TROUBLESHOOTING.md, "NVIDIA in Toolbox"'
    if location == 'host':
        return 'warn', ('The NVIDIA EGL vendor library is only on the host side (/run/host); the launcher '
                        'writes EGL manifests with its absolute path. If OpenGL still lands on llvmpipe, '
                        + guide)
    if location == 'missing':
        return 'warn', ('No NVIDIA EGL vendor library (libEGL_nvidia.so.0) in the container or under '
                        '/run/host: OpenGL falls back to llvmpipe and the game cannot present; ' + guide)
    return 'ok', 'NVIDIA EGL vendor library inside the container'


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


def check_wayland(facts):
    """Experimental native Wayland game window. Informational: it never fails the doctor."""
    wayland = facts.get('wayland') or {}
    requested = wayland.get('requested', '')
    session = (wayland.get('session_type') or '').lower() or 'unknown'
    if requested not in ('', '0', '1', 'game'):
        return 'warn', 'RCLOL_WAYLAND=%s is not 0, 1 or game; the launcher treats it as 0' % requested
    if requested not in ('1', 'game') and session != 'wayland':
        return 'ok', 'Experimental Wayland game window off (RCLOL_WAYLAND unset, %s session)' % session
    socket_state = 'found' if wayland.get('socket_found') else 'missing'
    library_state = 'found' if wayland.get('library_found') else 'missing'
    details = ('RCLOL_WAYLAND=%s; %s session; socket %s (%s); library %s (%s)'
               % (requested or 'unset', session, wayland.get('socket') or 'none', socket_state,
                  wayland.get('library', ''), library_state))
    if requested in ('1', 'game') and not (wayland.get('socket_found') and wayland.get('library_found')):
        return 'warn', details + '; the game stays on X11 until both are present (docs/TROUBLESHOOTING.md)'
    return 'ok', details


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


def check_perf_event_paranoid(facts):
    raw = str(facts.get('perf_event_paranoid', '')).strip()
    if not raw:
        return 'ok', 'kernel.perf_event_paranoid unavailable'
    try:
        value = int(raw)
    except ValueError:
        return 'ok', 'kernel.perf_event_paranoid=%s' % raw
    if value > 2:
        return 'warn', (
            'kernel.perf_event_paranoid=%d blocks unprivileged perf_event_open (EACCES), '
            'so profile=compat cannot align guest cpuid (common on Debian and hardened kernels). '
            'Set it to 2 or lower with `sudo sysctl kernel.perf_event_paranoid=2` '
            '(or persist in /etc/sysctl.d/99-darling-lol.conf); see docs/TROUBLESHOOTING.md' % value
        )
    return 'ok', 'kernel.perf_event_paranoid=%d' % value


def check_perf_event_open(facts):
    result = facts.get('perf_event_open', 'unavailable')
    if result in ('ok', 'unavailable', ''):
        return 'ok', ('perf_event_open works' if result == 'ok'
                      else 'perf_event_open probe not available on this platform')
    try:
        paranoid = int(str(facts.get('perf_event_paranoid', '')).strip())
    except ValueError:
        paranoid = None
    if result in ('EACCES', 'EPERM') and paranoid is not None and paranoid > 2:
        # check_perf_event_paranoid already names the fix.
        return 'warn', 'perf_event_open refused (%s) by kernel.perf_event_paranoid=%d' % (result, paranoid)
    return 'warn', (
        'perf_event_open is refused here (%s) although kernel.perf_event_paranoid allows it: a seccomp '
        'filter (Docker, Podman or Flatpak sandbox) or a security module blocks it, so profile=compat '
        'cannot align guest cpuid. Run the launcher outside that sandbox, or allow perf_event_open in '
        'its seccomp profile; see docs/TROUBLESHOOTING.md' % result)


def check_platform_identity(facts):
    if not facts.get('prefix_exists'):
        return 'ok', 'No prefix yet; setup provisions its platform identity'
    state = facts.get('platform_identity', 'missing')
    fix = 'run `python3 tools/configure-darling-platform.py --provision --write` with Darling stopped'
    if state == 'missing':
        return 'warn', ('No saved platform identity in this prefix, so iokitd reports a DARLING- serial no '
                        'Mac has; ' + fix + ' (scripts/update.sh does this too)')
    if state == 'shared':
        return 'warn', 'The prefix still has the old shared serial or UUID; ' + fix
    if state == 'unreadable':
        return 'warn', 'The saved platform identity could not be read; ' + fix
    return 'ok', 'Per-prefix platform identity saved'


def check_runtime_signing(facts):
    if not facts.get('prefix_launchd'):
        return 'ok', 'Runtime not installed yet'
    if facts.get('trust_store'):
        return 'ok', 'Runtime trust store present'
    if facts.get('rootless_namespace'):
        return 'ok', ('Rootless namespace (Toolbox or Nix): darlingserver does not use a trust store here, '
                      'so none is expected; the SDK signatures from scripts/update.sh still apply')
    return 'warn', (
        'No runtime signing trust store found, so the base Darling SDK is probably unsigned; '
        'run `sudo env RCODESIGN="$(sh tools/install-rcodesign.sh)" sh tools/sign-darling-runtime.sh --sdk` '
        '(inside the Toolbox container for Toolbox installs) or `sh scripts/update.sh`'
    )


def check_prefix(facts):
    if not facts['prefix_exists']:
        return 'fail', 'Prefix %s does not exist; run the installer first' % facts['prefix']
    if not facts['prefix_launchd']:
        return 'fail', ('Installed launchd is missing from %s; the runtime install did not complete'
                        % facts.get('runtime_dir', '/usr/local/libexec/darling'))
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
    ('hybrid', 'Hybrid graphics', check_hybrid),
    ('toolbox_nvidia_egl', 'Toolbox NVIDIA EGL', check_toolbox_nvidia_egl),
    ('session', 'Display session', check_session),
    ('wayland', 'Experimental Wayland game window', check_wayland),
    ('hud', 'Overlay layer', check_hud),
    ('switches', 'Diagnostic switches', check_switches),
    ('core', 'Core dumps', check_core_dumps),
    ('psr', 'Panel self-refresh', check_psr),
    ('perf_event_paranoid', 'Perf event access', check_perf_event_paranoid),
    ('perf_event_open', 'Perf event syscall', check_perf_event_open),
    ('platform_identity', 'Platform identity', check_platform_identity),
    ('runtime_signing', 'Runtime signing', check_runtime_signing),
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


def redact_text(text, prefix='', workspace=''):
    """Replace the home, prefix and workspace paths before a report is shared."""
    replacements = [
        (str(prefix), '<prefix>'),
        (str(workspace), '<workspace>'),
        (str(Path.home()), '~'),
    ]
    for value, placeholder in replacements:
        if value and value not in ('', '.'):
            text = text.replace(value, placeholder)
    return text


def report(facts, results, redact=True):
    prefix = facts['prefix']
    workspace = facts.get('workspace', '')
    checks = results
    environment = facts['environment']
    icds = facts['vulkan_icds']
    if redact:
        checks = [
            dict(check, title=redact_text(check['title'], prefix, workspace),
                 message=redact_text(check['message'], prefix, workspace))
            for check in results
        ]
        environment = {
            key: redact_text(value, prefix, workspace)
            for key, value in environment.items()
        }
        icds = [redact_text(item, prefix, workspace) for item in icds]
    return {
        'schema': SCHEMA,
        'tool': 'darling-lol doctor',
        'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'summary': summarize(checks),
        'host': facts['host'],
        'session': facts['session'],
        'memory': facts['memory'],
        'disk': facts['disk'],
        'gpus': facts['gpus'],
        'vulkan_icds': icds,
        'prefix': '<prefix>' if redact else prefix,
        'redacted': redact,
        'environment': environment,
        'trace_env': facts.get('trace_env', []),
        'switches': facts.get('switches', {}),
        'checks': checks,
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
    parser.add_argument('--no-redact', action='store_true',
                        help='keep the real prefix and workspace paths in the report')
    args = parser.parse_args(argv)

    facts = gather(prefix=args.prefix)
    results = run_checks(facts)
    document = report(facts, results, redact=not args.no_redact)

    if args.report:
        args.report.write_text(json.dumps(document, indent=2) + '\n')
        os.chmod(args.report, 0o600)
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        print(render(facts, results))
        if args.report:
            print('Report written to %s' % args.report)
    return 1 if document['summary']['fail'] else 0


if __name__ == '__main__':
    sys.exit(main())
