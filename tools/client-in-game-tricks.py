#!/usr/bin/env python3
"""Resolve the client-in-game mode from League's local preferences.

The public launcher switch is RCLOL_CLIENT_IN_GAME_TRICKS=1. It reads
`lol-user-experience` from LCULocalPreferences.yaml:

- closeLeagueClientDuringGame true -> close the client UI while a match runs
  (independent of Low Spec Mode, because Riot's backend launches a second CEF
  instance after a match whenever closeLeagueClientDuringGame is true);
- closeLeagueClientDuringGame false with potatoModeEnabled true -> hide the
  client windows instead;
- missing or invalid closeLeagueClientDuringGame, or closeLeagueClientDuringGame
  false with potatoModeEnabled false/missing/invalid -> no action (none).

Only the named `lol-user-experience` section is considered, so identical keys
in another section cannot select a helper. This helper only selects a mode; it
does not change preferences or files.
"""

import sys
from pathlib import Path

SECTION = 'lol-user-experience'
CLOSE_KEY = 'closeLeagueClientDuringGame'
OPTIMIZATION_KEY = 'potatoModeEnabled'
TRUE_VALUES = {'true', 'yes', '1'}
FALSE_VALUES = {'false', 'no', '0'}


def parse_bool(value):
    """Return True/False/None for one YAML scalar, ignoring a trailing comment."""
    value = value.strip()
    if not value:
        return None
    if value[0] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            return None
        scalar = value[1:end]
        remainder = value[end + 1:]
        if remainder and not remainder[0].isspace():
            return None
        remainder = remainder.strip()
        if remainder and not remainder.startswith('#'):
            return None
    else:
        scalar = value
        for index, char in enumerate(scalar):
            if char == '#' and (index == 0 or scalar[index - 1].isspace()):
                scalar = scalar[:index].rstrip()
                break
    scalar = scalar.lower()
    if scalar in TRUE_VALUES:
        return True
    if scalar in FALSE_VALUES:
        return False
    return None


def read_bool(path, key, section=SECTION):
    """Return True/False for the last key in section, or None when invalid.

    OSError/UnicodeError is raised for an unreadable file so callers can fail
    closed instead of treating a read race as a missing preference.
    """
    lines = Path(path).read_text().splitlines()
    in_section = False
    value = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if not line.startswith((' ', '\t')):
            if stripped == section + ':':
                in_section = True
            elif in_section:
                break
            continue
        if not in_section or not stripped.startswith(key + ':'):
            continue
        value = parse_bool(stripped.split(':', 1)[1])
    return value


def mode(path):
    try:
        close = read_bool(path, CLOSE_KEY)
    except (OSError, UnicodeError):
        return 'none'
    if close is True:
        return 'close'
    if close is not False:
        return 'none'
    try:
        optimization = read_bool(path, OPTIMIZATION_KEY)
    except (OSError, UnicodeError):
        return 'none'
    if optimization is True:
        return 'hide'
    return 'none'


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else ''
    print(mode(path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
