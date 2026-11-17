#!/usr/bin/env python3
"""Move an installed darling-system.prop to the compat default, once.

Until issue #31, tools/system.prop shipped `profile=host`, and install-runtime.sh
never replaces an installed copy, so existing prefixes kept HOST even after the
template switched to COMPAT. COMPAT is required for the notarization fallback in
patches/security.patch: under HOST, MVG's `notarized` self-check fails.

A file without the `profile.default=compat` marker predates the switch: its
`profile=host` is the old default, not a choice, so it becomes `profile=compat`.
The marker is then added, so a later explicit `profile=host` is kept.
"""
import argparse
from pathlib import Path
import sys

MARKER = 'profile.default=compat'


def migrate(text):
    """Return (new_text, changed)."""
    lines = text.splitlines()
    if any(line.strip() == MARKER for line in lines):
        return text, False
    profile_lines = [i for i, line in enumerate(lines) if line.strip().startswith('profile=')]
    if profile_lines and lines[profile_lines[-1]].strip() == 'profile=host':
        lines[profile_lines[-1]] = 'profile=compat'
    lines.append('# Set by tools/migrate-system-profile.py; keep it so profile=host stays a choice.')
    lines.append(MARKER)
    return '\n'.join(lines) + '\n', True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('path', type=Path)
    path = parser.parse_args().path
    if not path.is_file():
        return 0
    old = path.read_text()
    new, changed = migrate(old)
    if changed:
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(new)
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
        profile = [line for line in new.splitlines() if line.startswith('profile=')][-1:]
        print(f'{path}: {profile[0] if profile else "no profile line"} (compat is now the default)',
              file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
