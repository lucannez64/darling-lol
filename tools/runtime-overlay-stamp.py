#!/usr/bin/env python3
"""Fingerprint the runtime overlay install so an unchanged tree skips the rebuild.

`install-runtime.sh` rebuilds and installs the per-user overlays on every
update: seven CMake targets through `install-riot-presentation-runtime.sh`, the
overlay builders of `install-riot-gpu-runtime.sh`, the optimized library batch,
the Electron 43 stubs and their signing pass. The CMake graph scan alone costs
about 25 seconds even when every target is already up to date.

`record` stores a digest of everything those steps consume, taken after the
install and after signing (which rewrites the installed Mach-Os). `check`
recomputes it; exit 0 means the previous install is still current and the
caller may skip the whole batch. Anything unknown, unreadable or missing is a
change, never a skip, so a failed probe only costs the rebuild it replaces.

Sections:
  tree       pinned and patched Darling checkout (HEAD, working-tree diff and patch-added files)
  tools      every source under tools/ plus the installer that drives the batch
  flags      cmake flags.make/link.txt of every directory the batch builds
  artifacts  cmake artifacts the batch installs
  capabilities  clang/llvm-config versions and the optional Wayland headers
  config     build/config.json (paths, guest home, region, locale, SDK)
  overlays   every installed overlay file, after signing
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checkout_digest import digest as checkout_digest  # noqa: E402
from checkout_digest import entry  # noqa: E402
from console import fail, ok  # noqa: E402
from file_digest import sha256  # noqa: E402

VERSION = 1

# Directories of the prepared CMake tree whose targets the overlay scripts build.
TARGET_DIRS = (
    'src/external/xnu/darling/src/libsystem_kernel',
    'src/external/libplatform',
    'src/external/corefoundation',
    'src/external/cfnetwork/src',
    'src/external/configd/SystemConfiguration.fproj',
    'src/external/cocotron/QuartzCore',
    'src/external/cocotron/AppKit',
    'src/native',
    'src/frameworks/CoreServices/src/LaunchServices',
    'src/frameworks/Accelerate/vecLib/vDSP',
)

# Artifacts those scripts install; relative to build/darling-src/build.
ARTIFACTS = (
    'src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib',
    'src/external/libplatform/libsystem_platform.dylib',
    'src/external/corefoundation/CoreFoundation',
    'src/external/cfnetwork/src/CFNetwork',
    'src/external/configd/SystemConfiguration.fproj/SystemConfiguration',
    'src/external/cocotron/QuartzCore/QuartzCore',
    'src/external/cocotron/AppKit/AppKit',
    'src/external/cocotron/AppKit/X11',
    'src/native/libGL.dylib',
    'src/frameworks/CoreServices/src/LaunchServices/LaunchServices',
    'src/frameworks/Accelerate/vecLib/vDSP/libvDSP.dylib',
)

# Sources of the overlay builders and installers. Every regular file under
# tools/ counts, so a new builder cannot be forgotten here; only the caches and
# compiled binaries the tools themselves drop into tools/ are skipped, along
# with the stamp tooling, which decides whether to run the batch but never
# changes what it produces.
TOOL_SKIP_DIRS = ('__pycache__',)
TOOL_OUTPUTS = ('tools/dc-codesign/dc-codesign', 'tools/dc-codesign/dc-parse-fuzz')
TOOL_SELF = (
    'tools/runtime-overlay-stamp.py',
    'tools/build-configure-stamp.py',
    'tools/darling-install-stamp.py',
    'tools/sdk-sign-stamp.py',
    'tools/checkout_digest.py',
    'tools/console.py',
)

EXTRA_INPUTS = ('scripts/install-runtime.sh',)

# Optional build capabilities and the compiler version: installing the Wayland
# headers or upgrading clang changes what the overlay batch produces without
# touching any hashed file, so probe them too.
CAPABILITIES = (
    ('clang', ('clang', '--version')),
    ('llvm-config', ('llvm-config', '--version')),
    ('wayland', ('pkg-config', '--exists', 'wayland-client', 'wayland-egl',
                 'xkbcommon', 'wayland-protocols', 'wayland-scanner')),
)


def _reraise(error):
    raise error


def _entry(relative, path):
    return entry(relative, path)


def _tool_output(path, root):
    """True for the compiled binaries and stamp tooling that are not inputs."""
    relative = path.relative_to(root).as_posix()
    return relative in TOOL_OUTPUTS or relative in TOOL_SELF


def tree_digest(root):
    """Digest of one file, one symlink or every entry of a directory tree.

    Returns None when anything under the tree cannot be read, which the caller
    treats as a change.
    """
    root = Path(root)
    hasher = hashlib.sha256()
    try:
        if root.is_symlink() or root.is_file():
            hasher.update(_entry('.', root).encode())
        elif root.is_dir():
            for top, dirs, files in os.walk(root, followlinks=False, onerror=_reraise):
                dirs.sort()
                folder = Path(top)
                for name in dirs:
                    item = folder / name
                    if item.is_symlink():
                        hasher.update(_entry(item.relative_to(root).as_posix(), item).encode())
                for name in sorted(files):
                    item = folder / name
                    hasher.update(_entry(item.relative_to(root).as_posix(), item).encode())
    except OSError:
        return None
    return hasher.hexdigest()


def files_digest(paths, base):
    """Digest of relative paths and contents; None when any file is missing."""
    hasher = hashlib.sha256()
    try:
        for path in sorted(set(paths)):
            hasher.update(path.relative_to(base).as_posix().encode())
            hasher.update(b'\0')
            hasher.update(sha256(path).encode())
            hasher.update(b'\n')
    except OSError:
        return None
    return hasher.hexdigest()


# Sources of the overlay builders and installers. Every regular file under
# tools/ counts, so a new builder cannot be forgotten here; only the build
# outputs the scripts drop into tools/ are skipped.
TOOL_SKIP_DIRS = ('__pycache__',)


def tools_digest(root):
    root = Path(root)
    found = []
    try:
        for top, dirs, files in os.walk(root / 'tools', onerror=_reraise):
            dirs[:] = sorted(name for name in dirs if name not in TOOL_SKIP_DIRS)
            for name in files:
                path = Path(top) / name
                if path.is_file() and not _tool_output(path, root):
                    found.append(path)
    except OSError:
        return None
    found.extend(path for path in (root / name for name in EXTRA_INPUTS) if path.is_file())
    if not found:
        return None
    return files_digest(found, root)


def flags_digest(root):
    base = Path(root) / 'build/darling-src/build'
    found = []
    for directory in TARGET_DIRS:
        folder = base / directory / 'CMakeFiles'
        if not folder.is_dir():
            return None
        found.extend(folder.rglob('flags.make'))
        found.extend(folder.rglob('link.txt'))
    if not found:
        return None
    return files_digest(found, base)


def artifacts_digest(root):
    base = Path(root) / 'build/darling-src/build'
    return files_digest([base / artifact for artifact in ARTIFACTS], base)


def capabilities_digest():
    """Toolchain versions and optional build capabilities the batch probes."""
    values = []
    for name, command in CAPABILITIES:
        try:
            result = subprocess.run(command, capture_output=True, text=True)
            first = (result.stdout or '').strip().splitlines()
            value = '%d:%s' % (result.returncode, first[0] if first else '')
        except OSError as error:
            value = 'unavailable:%s' % error.errno
        values.append('%s\0%s' % (name, value))
    return hashlib.sha256('\0'.join(values).encode()).hexdigest()


def load_config(root):
    try:
        config = json.loads((Path(root) / 'build/config.json').read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict) or not config.get('prefix') or not config.get('guest_home'):
        return None
    return config


def config_digest(root):
    return files_digest([Path(root) / 'build/config.json'], Path(root))


def effective_paths(config):
    """The prefix and guest home the installers resolve, including env overrides."""
    prefix = Path(os.environ.get('DPREFIX') or config['prefix'])
    guest_home = Path(os.environ.get('RIOT_GUEST_HOME')
                      or str(prefix / str(config['guest_home']).lstrip('/')))
    return prefix, guest_home


def overlay_roots(prefix, guest_home):
    """Every tree the overlay batch writes, as sign-darling-runtime.sh sees them."""
    return (
        guest_home / 'RuntimeLibraries',
        guest_home / 'RuntimeFrameworks',
        guest_home / 'Library/Audio/Plug-Ins/Components',
        guest_home / 'libDarlingXShm.dylib',
        guest_home / 'libDarlingWayland.dylib',
        prefix / 'usr/lib',
        prefix / 'System/Library/Frameworks',
    )


def overlays_digest(root, config):
    prefix, guest_home = effective_paths(config)
    hasher = hashlib.sha256(('%s\0%s\0' % (prefix, guest_home)).encode())
    for target in overlay_roots(prefix, guest_home):
        value = tree_digest(target)
        if value is None:
            return None
        hasher.update(('%s\0%s\n' % (target, value)).encode())
    return hasher.hexdigest()


def collect(root):
    root = Path(root)
    config = load_config(root)
    return {
        'tree': checkout_digest(root),
        'tools': tools_digest(root),
        'flags': flags_digest(root),
        'artifacts': artifacts_digest(root),
        'capabilities': capabilities_digest(),
        'config': config_digest(root),
        'overlays': overlays_digest(root, config) if config else None,
    }


def default_root():
    here = Path(__file__).resolve().parent
    for candidate in (here,) + here.parents:
        if ((candidate / 'scripts/install-runtime.sh').is_file()
                and (candidate / 'tools/riot_build_cache.py').is_file()):
            return candidate
    return here.parent


def record(state, sections):
    unknown = sorted(name for name, value in sections.items() if value is None)
    if unknown:
        fail('runtime overlay stamp: cannot record %s; the next update rebuilds'
             % ', '.join(unknown))
        return 1
    state.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'version': VERSION,
        'recorded': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'sections': sections,
    }
    part = state.with_name(state.name + '.part')
    part.write_text(json.dumps(payload, indent=1, sort_keys=True) + '\n')
    os.replace(part, state)
    return 0


def check(state, sections):
    try:
        recorded = json.loads(Path(state).read_text())
    except (OSError, ValueError):
        print('runtime overlay stamp: no usable state at %s' % state)
        return 1
    if not isinstance(recorded, dict) or recorded.get('version') != VERSION:
        print('runtime overlay stamp: stale format at %s' % state)
        return 1
    previous = recorded.get('sections')
    if not isinstance(previous, dict):
        print('runtime overlay stamp: missing sections at %s' % state)
        return 1
    changed = []
    for name in sorted(sections):
        if sections[name] is None:
            changed.append(name + ' (unreadable)')
        elif previous.get(name) != sections[name]:
            changed.append(name)
    if changed:
        print('runtime overlay stamp: changed %s' % ', '.join(changed))
        return 1
    ok('runtime overlay stamp: up to date')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('check', 'record'))
    parser.add_argument('--root', default=None,
                        help='repository root (default: derived from this file)')
    parser.add_argument('--state', default=None,
                        help='stamp file (default: build/runtime/overlay-stamp.json)')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    state = Path(args.state) if args.state else root / 'build/runtime/overlay-stamp.json'
    sections = collect(root)
    if args.command == 'record':
        return record(state, sections)
    return check(state, sections)


if __name__ == '__main__':
    sys.exit(main())
