#!/usr/bin/env python3
"""Opt-in recovery of a windowless Riot session on Hyprland; fail closed elsewhere."""
import json
import os
from pathlib import Path
import subprocess


def recover(proc_root=Path('/proc')):
    home = Path.home()
    prefix = Path(os.environ.get('DPREFIX', str(home / '.darling'))).expanduser()
    if prefix != home / '.darling':
        return False  # Custom prefixes are not validated by this desktop helper.
    riot_running = False
    try:
        for proc in proc_root.iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if proc.stat().st_uid != os.getuid():
                    continue
                argv = (proc / 'cmdline').read_bytes().split(b'\0')
                executable = argv[0].decode(errors='replace')
                if executable.endswith('/LeagueofLegends'):
                    return False  # Never restart a running game, even without a window.
                if '/Riot Client.app/' not in executable:
                    continue
                environment = dict(item.split(b'=', 1) for item in
                                   (proc / 'environ').read_bytes().split(b'\0') if b'=' in item)
                process_prefix = Path(os.fsdecode(environment.get(b'DPREFIX', os.fsencode(home / '.darling'))))
                if process_prefix == prefix:
                    riot_running = True
            except FileNotFoundError:
                continue
    except (OSError, ValueError):
        return False  # Unknown ownership/visibility must not trigger shutdown.
    if not riot_running:
        return False
    try:
        result = subprocess.run(['hyprctl', 'clients', '-j'], check=True,
                                capture_output=True, text=True, timeout=5)
        windows = json.loads(result.stdout)
        if not isinstance(windows, list):
            return False
        for window in windows:
            identity = (window.get('class', '') + ' ' + window.get('title', '')).lower()
            if any(name in identity for name in ('riot', 'league')) and window.get('mapped') and not window.get('hidden'):
                return False
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return False
    print('Recovering a Riot session with no visible client window.', flush=True)
    subprocess.run(['darling', 'shutdown'], check=True, timeout=25)
    return True


if __name__ == '__main__':
    recover()
