#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Provision macOS 15.5 framework metadata in a stopped Darling prefix overlay.

Only factual bundle/build metadata is installed, never framework code. The
runtime root is read-only. Without --write, list changes without creating files;
--check reports missing or different overlay metadata with a nonzero exit status.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import plistlib

# Reuse the platform provisioner's prefix containment, stopped-prefix check and
# atomic publication without creating identity backups for reproducible metadata.
_spec = importlib.util.spec_from_file_location(
    'darling_platform', Path(__file__).resolve().with_name('configure-darling-platform.py'))
_platform = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_platform)
checked_path = _platform.checked_path
ensure_stopped = _platform.ensure_stopped
atomic_write = _platform.atomic_write
MAX_SIZE = _platform.MAX_SIZE
MAX_DATA_SIZE = 1024 * 1024
DATA_NAME = 'macos-15.5-framework-info.json'
DATA_FILE = Path(__file__).resolve().parent / 'data' / DATA_NAME
if not DATA_FILE.is_file():
    # configure.py renders tools, not subdirectories: retain the tracked data
    # source when this script is run from build/runtime/tools.
    DATA_FILE = Path('@PROJECT_ROOT@') / 'tools/data' / DATA_NAME
METADATA_KEYS = {
    'CFBundleIdentifier', 'CFBundleName', 'CFBundleExecutable', 'CFBundlePackageType',
    'CFBundleShortVersionString', 'CFBundleVersion', 'CFBundleSignature',
    'CFBundleDevelopmentRegion', 'CFBundleInfoDictionaryVersion',
    'CFBundleSupportedPlatforms', 'DTPlatformName', 'DTPlatformVersion',
    'DTPlatformBuild', 'DTSDKName', 'DTSDKBuild', 'DTXcode', 'DTXcodeBuild',
    'DTCompiler', 'BuildMachineOSBuild', 'LSMinimumSystemVersion',
}


def load_metadata(source=DATA_FILE):
    with Path(source).open('rb') as stream:
        raw = stream.read(MAX_DATA_SIZE + 1)
    if len(raw) > MAX_DATA_SIZE:
        raise ValueError('Framework metadata input exceeds 1 MiB')
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or type(payload.get('schema')) is not int
            or payload['schema'] != 1 or payload.get('macos') != '15.5'
            or payload.get('build') != '24F74' or not isinstance(payload.get('frameworks'), dict)):
        raise ValueError('Unsupported framework metadata schema or macOS release')
    for relative, metadata in payload['frameworks'].items():
        parts = PurePosixPath(relative).parts
        if (not relative.startswith(('System/Library/Frameworks/', 'System/Library/PrivateFrameworks/'))
                or not relative.endswith('.framework') or '..' in parts
                or str(PurePosixPath(relative)) != relative):
            raise ValueError('Invalid framework metadata path: ' + relative)
        if (not isinstance(metadata, dict) or set(metadata) - METADATA_KEYS
                or not metadata.get('CFBundleIdentifier')
                or not (metadata.get('CFBundleVersion') or metadata.get('CFBundleShortVersionString'))
                or metadata.get('DTPlatformVersion', '15.5') != '15.5'):
            raise ValueError('Invalid framework metadata: ' + relative)
        for key, value in metadata.items():
            if key == 'CFBundleSupportedPlatforms':
                valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise ValueError('Invalid framework metadata value: ' + key)
    return payload['frameworks']


def current_version(bundle):
    versions = bundle / 'Versions'
    try:
        current = (versions / 'Current').resolve(strict=True)
    except FileNotFoundError:
        return 'A'
    if current.parent != versions.resolve() or current.name in ('.', '..', 'Current') or not current.is_dir():
        raise ValueError('Runtime Versions/Current must name a framework version directory')
    return current.name


def provisioning_changes(prefix, runtime_root, frameworks):
    """Preflight every destination before allowing any overlay writes."""
    prefix = Path(prefix).expanduser()
    runtime_root = Path(runtime_root).expanduser().resolve()
    checked_path(prefix, 'System/Library')
    if prefix.resolve().is_relative_to(runtime_root):
        raise ValueError('Prefix must be outside the read-only runtime root')
    if not runtime_root.is_dir():
        raise ValueError('Runtime root does not exist: ' + str(runtime_root))
    files, links = {}, {}
    for relative, metadata in sorted(frameworks.items()):
        bundle = runtime_root / relative
        if not bundle.is_dir():
            continue
        if (bundle / 'Versions').is_dir():
            version = current_version(bundle)
            target = Path(relative) / 'Versions' / version / 'Resources/Info.plist'
            resources = prefix / relative / 'Resources'
            runtime_resources = bundle / 'Resources'
            # Reject an unsafe prefix alias even though writes use the canonical
            # versioned path; the guest also resolves metadata through Resources.
            if os.path.lexists(resources):
                checked_path(prefix, Path(relative) / 'Resources/Info.plist')
            # lexists preserves even dangling links used by the merged guest root.
            if not os.path.lexists(resources) and not os.path.lexists(runtime_resources):
                link = checked_path(prefix, Path(relative) / 'Resources')
                link_target = 'Versions/Current/Resources'
                resolved_target = (link.parent / link_target).resolve()
                if (not resolved_target.is_relative_to(prefix.resolve())
                        or resolved_target.is_relative_to(runtime_root)):
                    raise ValueError('Framework Resources link must remain inside the selected prefix')
                links[link] = link_target
        else:
            target = Path(relative) / 'Resources/Info.plist'
        path = checked_path(prefix, target)
        if path.resolve().is_relative_to(runtime_root):
            raise ValueError('Framework destination must be outside the read-only runtime root')
        encoded = plistlib.dumps(metadata, fmt=plistlib.FMT_XML, sort_keys=True)
        if len(encoded) > MAX_SIZE:
            raise ValueError('Framework Info.plist exceeds 64 KiB: ' + relative)
        previous = None
        if path.exists():
            with path.open('rb') as stream:
                previous = stream.read(MAX_SIZE + 1)
            if len(previous) > MAX_SIZE:
                raise ValueError('Existing framework Info.plist exceeds 64 KiB: ' + relative)
        if previous != encoded:
            files[path] = encoded
    return files, links


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True, help='Absolute path to the Darling prefix')
    parser.add_argument('--runtime-root', type=Path, default=Path('/usr/local/libexec/darling'),
                        help='Read-only Darling system root (default: /usr/local/libexec/darling)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--write', action='store_true', help='Write metadata to a stopped prefix')
    mode.add_argument('--check', action='store_true', help='Fail if any expected overlay plist or link differs')
    args = parser.parse_args(argv)
    try:
        prefix = args.prefix.expanduser()
        if args.write:
            ensure_stopped(prefix)
        files, links = provisioning_changes(prefix, args.runtime_root, load_metadata())
        for path in files:
            print(('Write ' if args.write else 'Missing or different ' if args.check else 'Would write ') + str(path))
        for path, target in links.items():
            print(('Link ' if args.write else 'Missing link ' if args.check else 'Would link ') + str(path) + ' -> ' + target)
        if args.write:
            for path, encoded in files.items():
                atomic_write(checked_path(prefix, path.relative_to(prefix)), encoded,
                             create_backup=False, mode=_platform.SYSTEM_METADATA_MODE)
            for path, target in links.items():
                checked_path(prefix, path.relative_to(prefix))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target)
        changed = len(files) + len(links)
        print(f'Framework metadata: {len(files)} plist changes, {len(links)} resource links'
              + ('; overlay saved.' if args.write else '.' if args.check else '; dry run.'))
        return int(args.check and changed != 0)
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
