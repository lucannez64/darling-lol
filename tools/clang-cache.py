#!/usr/bin/env python3
"""Skip clang compiles whose inputs did not change since the last run.

The runtime overlay builders (tools/build-*.py, run by
install-riot-gpu-runtime.sh) call clang/clang++ for every source on every
update. install-riot-gpu-runtime.sh puts shims named clang and clang++ first on
PATH that run this script, so a single-source `-c ... -o OBJ` compile is skipped
when OBJ exists and nothing that could change it has changed:

- the command line, working directory and compiler binary (path, mtime, size);
- the environment variables clang reads for include paths and the SDK;
- the CPU model and flags when the command uses -march/-mtune=native;
- the content of every file the previous compile read, from its -MD dependency
  file (riot_build_cache.dependency_chunk), which covers the source and every
  header, system headers included.

Anything else (links, preprocessing, multiple sources, stdin) runs the real
compiler untouched. A compile whose inputs changed while it ran records nothing,
so the next run compiles again.

Usage: clang-cache.py clang|clang++ ARGS...
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import riot_build_cache  # noqa: E402

SOURCE_SUFFIXES = ('.c', '.cc', '.cpp', '.cxx', '.m', '.mm', '.s', '.S')
ENVIRONMENT = ('CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'OBJC_INCLUDE_PATH',
               'OBJCPLUS_INCLUDE_PATH', 'SDKROOT', 'MACOSX_DEPLOYMENT_TARGET')


def real_compiler(name):
    """The first `name` on PATH that is not one of the shims."""
    shims = os.environ.get('RCLOL_CLANG_CACHE_SHIMS', '')
    path = os.pathsep.join(entry for entry in os.environ.get('PATH', '').split(os.pathsep)
                           if entry and os.path.realpath(entry) != os.path.realpath(shims or '/nonexistent'))
    found = shutil.which(name, path=path)
    if not found:
        raise SystemExit('clang-cache: %s not found on PATH' % name)
    return found


def cacheable(args):
    """(object path, dependency file, extra args) for a plain compile, else None."""
    if '-c' not in args or args.count('-o') != 1 or '-' in args:
        return None
    if any(flag in args for flag in ('-E', '-M', '-MM', '-S', '-fsyntax-only')):
        return None
    if sum(1 for arg in args if arg.endswith(SOURCE_SUFFIXES) and not arg.startswith('-')) != 1:
        return None
    output = args[args.index('-o') + 1]
    if '-MF' in args:
        index = args.index('-MF')
        if index + 1 >= len(args):
            return None
        return output, args[index + 1], []
    return output, output + '.d', ['-MD', '-MF', output + '.d']


def cpu_identity(args):
    if not any(arg.endswith('=native') for arg in args):
        return ''
    try:
        text = Path('/proc/cpuinfo').read_text()
    except OSError:
        return 'unknown-cpu'
    lines = [line for line in text.splitlines() if line.startswith(('model name', 'flags'))]
    return hashlib.sha256('\n'.join(sorted(set(lines))).encode()).hexdigest()


def key(compiler, args, depfile):
    """The digest of everything that decides the object, or None when unknown."""
    dependencies = riot_build_cache.dependency_chunk([Path(depfile)])
    if not dependencies.startswith('dependencies:'):
        return None
    chunks = [compiler, riot_build_cache.stat_entry(compiler), os.getcwd(), '\0'.join(args),
              '\0'.join('%s=%s' % (name, os.environ.get(name, '')) for name in ENVIRONMENT),
              cpu_identity(args), dependencies]
    return riot_build_cache.fingerprint(chunks)


def stamp_of(output):
    return output + '.clang-cache'


def recorded(output):
    try:
        return json.loads(Path(stamp_of(output)).read_text()).get('key')
    except (OSError, ValueError, AttributeError):
        return None


def main(argv):
    if len(argv) < 2:
        raise SystemExit('usage: clang-cache.py clang|clang++ ARGS...')
    compiler = real_compiler(argv[1])
    args = argv[2:]
    plan = None if os.environ.get('RCLOL_CLANG_CACHE') == '0' else cacheable(args)
    if plan is None:
        os.execv(compiler, [compiler] + args)
    output, depfile, extra = plan
    command = [compiler] + args + extra

    if os.path.isfile(output) and os.path.isfile(depfile):
        current = key(compiler, args, depfile)
        if current is not None and current == recorded(output):
            return 0

    Path(stamp_of(output)).unlink(missing_ok=True)
    started = time.time()
    result = subprocess.run(command)
    if result.returncode != 0 or not os.path.isfile(output):
        return result.returncode or 1
    # Record only when no input changed while compiling, so the stamp never
    # vouches for an object built from mixed inputs.
    paths = riot_build_cache.dependency_paths(depfile) or []
    try:
        if any(os.stat(path).st_mtime > started for path in paths):
            return 0
    except OSError:
        return 0
    current = key(compiler, args, depfile)
    if current is not None:
        Path(stamp_of(output)).write_text(json.dumps({'key': current}) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
