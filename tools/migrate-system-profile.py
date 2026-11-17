#!/usr/bin/env python3
"""Move an installed darling-system.prop to the compat default, once.

Until issue #31, tools/system.prop shipped `profile=host`, and install-runtime.sh
never replaces an installed copy, so existing prefixes kept HOST even after the
template switched to COMPAT. COMPAT is required for the notarization fallback in
patches/security.patch: under HOST, MVG's `notarized` self-check fails.

A file without the `profile.default=compat` marker predates the switch: its
`profile=host` is the old default, not a choice, so it becomes `profile=compat`.
The marker is then added, so a later explicit `profile=host` is kept. A
`compat.cpu.brand` still on the pre-2026-11-11 template value is updated too.
"""
import argparse
from pathlib import Path
import sys

MARKER = 'profile.default=compat'
# Template brand before b84eaa7 (2026-11-11). The kernel lays the brand over the
# i7-9750H identity table (family, model, 6 cores), so the old i9 name no longer
# matches; installed copies kept it because install-runtime.sh never replaces them.
OLD_COMPAT_BRAND = 'compat.cpu.brand=Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz'
COMPAT_BRAND = 'compat.cpu.brand=Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz'


def effective_profile(text):
    """The profile the readers select: run-riot-client.sh and
    darling-system_profiler.sh take the last line starting with `profile=` at
    column 0, untrimmed."""
    values = [line[len('profile='):] for line in text.splitlines() if line.startswith('profile=')]
    return values[-1] if values else None


def migrate(text):
    """Return (new_text, changed)."""
    lines = text.splitlines()
    if any(line.strip() == MARKER for line in lines):
        return text, False
    profile_lines = [i for i, line in enumerate(lines) if line.startswith('profile=')]
    if profile_lines and lines[profile_lines[-1]] == 'profile=host':
        lines[profile_lines[-1]] = 'profile=compat'
    lines = [COMPAT_BRAND if line == OLD_COMPAT_BRAND else line for line in lines]
    lines.append('# Set by tools/migrate-system-profile.py; keep it so profile=host stays a choice.')
    lines.append(MARKER)
    return '\n'.join(lines) + '\n', True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('path', type=Path)
    # Write through a symlink to the file it names, not over the link.
    path = parser.parse_args().path.resolve()
    if not path.is_file():
        return 0
    old = path.read_text(encoding='utf-8', errors='surrogateescape')
    new, changed = migrate(old)
    if changed:
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(new, encoding='utf-8', errors='surrogateescape')
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
        if effective_profile(old) == 'host':
            print(f'darling-lol: hardware profile switched from host to compat, the new default '
                  f'(issue #31). To keep host, set profile=host in {path}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
