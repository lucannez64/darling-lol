"""Find LLVM command line tools, including versioned Debian/Ubuntu names.

Ubuntu's llvm-<N>-dev installs llvm-objdump and llvm-nm in /usr/lib/llvm-<N>/bin
without an unversioned name in PATH, so a bare ['llvm-objdump'] run fails on an
otherwise complete host (issue #138). Resolution order: an unversioned tool on
PATH, then the highest versioned name on PATH, then the directory llvm-config
reports. Nothing here runs a guest process.
"""
import os
import subprocess

TIMEOUT = 30  # A probe that hangs must not stall the preflight or the build.


def _collect(directory, name, found):
    """Add every `name` and `name-<digits>` executable in the directory to found."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return
    for entry in entries:
        suffix = entry[len(name) + 1:]
        if entry == name:
            version = 0  # An unversioned name always wins.
        elif entry.startswith(name + '-') and suffix.isdigit():
            version = int(suffix)
        else:
            continue
        path = os.path.join(directory or '.', entry)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            found.setdefault(version, path)


def _executables(name):
    """{version: path} for `name` and for every `name-<digits>` on PATH."""
    found = {}
    for folder in os.environ.get('PATH', '').split(os.pathsep):
        _collect(folder or '.', name, found)
    return found


def _preferred(candidates):
    return candidates[0 if 0 in candidates else max(candidates)] if candidates else None


def _run(command):
    """Run a short probe; return (completed process, problem), one of them None."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors='replace',
                                timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f'did not finish within {TIMEOUT} seconds'
    except OSError as error:
        return None, f'could not be started: {error}'
    return result, None


def _bindir():
    """`llvm-config --bindir` from the preferred llvm-config, or None."""
    configuration = _preferred(_executables('llvm-config'))
    if not configuration:
        return None
    result, _ = _run([configuration, '--bindir'])
    if result is None:
        return None
    directory = result.stdout.strip()
    if result.returncode or not os.path.isdir(directory):
        return None
    return directory


def find(name):
    """Path of an LLVM tool, or None when this host does not have it."""
    candidates = _executables(name)
    if candidates:
        return _preferred(candidates)
    directory = _bindir()
    if directory:
        in_bindir = {}
        _collect(directory, name, in_bindir)
        if in_bindir:
            return _preferred(in_bindir)
    return None


def require(name):
    """Path of an LLVM tool, or exit with what was tried and what to install."""
    path = find(name)
    if path:
        return path
    raise SystemExit(f'{name} not found: looked for an unversioned {name}, a versioned '
                     f'{name}-<N> in PATH and `llvm-config --bindir`. Install the LLVM tools '
                     f'package (for example llvm-dev, llvm15-tools or llvm23-tools).')


def config(*arguments):
    """Run llvm-config with the arguments, or exit with an actionable message."""
    path = require('llvm-config')
    result, problem = _run([path, *arguments])
    called = f'{path} {" ".join(arguments)}'
    if problem:
        raise SystemExit(f'{called} {problem}; reinstall the LLVM development package.')
    if result.returncode:
        raise SystemExit(f'{called} failed: {result.stderr.strip()}')
    return result.stdout.strip()
