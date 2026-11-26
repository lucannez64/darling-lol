#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Give the guest user the language preferences a macOS 15.5 user has.

macOS keeps AppleLanguages and AppleLocale in the global domain
(~/Library/Preferences/.GlobalPreferences.plist) and applications read them
from there. Darling's Foundation used to copy an empty AppleLanguages array
into every application's own domain instead. This tool, run on a stopped
prefix:

- adds the global values from the configured locale when the user has none
  (existing user choices are kept), and
- removes the empty AppleLanguages arrays Darling wrote into application
  domains, deleting a domain file that held nothing else (macOS has no such
  file).

Without --write it lists the changes; --check exits nonzero if any are needed.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import plistlib
import re
import sys

_spec = importlib.util.spec_from_file_location(
    'darling_platform', Path(__file__).resolve().with_name('configure-darling-platform.py'))
_platform = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_platform)
checked_path = _platform.checked_path
ensure_stopped = _platform.ensure_stopped
atomic_write = _platform.atomic_write
MAX_SIZE = _platform.MAX_SIZE

LOCALE_RE = re.compile(r'^[a-z]{2,3}(_[A-Z]{2})?$')
# Values a macOS 15 language migration leaves in the global domain (measured on 15.5).
LANGUAGES_SCHEMA_VERSION = 5400
USER_PREFERENCES_MODE = 0o600


def global_values(locale):
    if not LOCALE_RE.match(locale or ''):
        raise ValueError('Locale must look like en_GB, got %r' % (locale,))
    return {
        'AppleLanguages': [locale.replace('_', '-')],
        'AppleLocale': locale,
        'AppleLanguagesSchemaVersion': LANGUAGES_SCHEMA_VERSION,
        'AppleLanguagesDidMigrate': _platform.EMULATED_PRODUCT_VERSION,
    }


def read_plist(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_SIZE + 1)
    if len(raw) > MAX_SIZE:
        raise ValueError('%s exceeds %d bytes' % (path, MAX_SIZE))
    data = plistlib.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('%s is not a dictionary plist' % path)
    return data


def plan(prefix, guest_user, locale):
    """Return [(action, path, data)] with action 'write' or 'delete'."""
    preferences = checked_path(prefix, 'Users/%s/Library/Preferences' % guest_user)
    changes = []
    global_path = preferences / '.GlobalPreferences.plist'
    current = read_plist(global_path) if global_path.is_file() else {}
    wanted = dict(current)
    # Keep the user's own language choices; only fill what is missing as a unit.
    if not current.get('AppleLanguages'):
        wanted.update(global_values(locale))
    if wanted != current:
        changes.append(('write', global_path, wanted))
    if preferences.is_dir():
        for path in sorted(preferences.glob('*.plist')):
            if path.name.startswith('.') or path.is_symlink() or not path.is_file():
                continue
            try:
                data = read_plist(path)
            except (ValueError, plistlib.InvalidFileException):
                continue
            if data.get('AppleLanguages') != []:
                continue
            rest = {key: value for key, value in data.items() if key != 'AppleLanguages'}
            changes.append(('delete', path, None) if not rest else ('write', path, rest))
    return changes


def apply(changes):
    for action, path, data in changes:
        if action == 'delete':
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, plistlib.dumps(data, fmt=plistlib.FMT_BINARY),
                         create_backup=False, mode=USER_PREFERENCES_MODE)


def load_config():
    config = Path(__file__).resolve().parents[1] / 'build/config.json'
    if not config.is_file():
        config = Path('@PROJECT_ROOT@') / 'build/config.json'
    return json.loads(config.read_text()) if config.is_file() else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--prefix', type=Path)
    parser.add_argument('--guest-user')
    parser.add_argument('--locale')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    config = load_config()
    prefix = args.prefix or Path(config.get('prefix') or Path.home() / '.darling')
    guest_user = args.guest_user or config.get('guest_user')
    locale = args.locale or config.get('locale') or 'en_US'
    if not guest_user or '/' in guest_user or guest_user in ('.', '..'):
        parser.error('a guest user name is required (--guest-user or build/config.json)')
    changes = plan(prefix.resolve(), guest_user, locale)
    for action, path, data in changes:
        print('%s %s' % (action, path))
    if args.check:
        return 1 if changes else 0
    if args.write and changes:
        ensure_stopped(prefix.resolve())
        apply(changes)
    return 0


if __name__ == '__main__':
    sys.exit(main())
