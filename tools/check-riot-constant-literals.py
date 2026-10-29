#!/usr/bin/env python3
"""Build and run the constant-literal check inside the guest.

Clang turns `@42` and `@[@"a"]` into statically allocated objects when the
translation unit targets a macOS 11 or newer runtime, and binds them to
NSConstantIntegerNumber / NSConstantArray and friends. Before this support,
Darling had none of those classes or the empty-collection objects, so
a client built that way failed to bind at launch with

    dyld: Symbol not found: _OBJC_CLASS_$_NSConstantIntegerNumber

The check builds tools/riot-electron43-frameworks/constant-literal-check.m and
runs it against this prefix's RuntimeFrameworks overlay, which is where the
rebuilt CoreFoundation and Foundation live. A pass means the emitted layouts are
understood: values, descriptions, equality in both directions, copies that return
the same object, and retain/release that do not touch the read-only instance.

    python3 tools/check-riot-constant-literals.py

Build and install the overlays first; the check reports which class is missing
when the overlay is older than the sources.
"""
import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from runtime_paths import runtime_paths  # noqa: E402

SOURCE = ROOT / 'tools/riot-electron43-frameworks/constant-literal-check.m'
REQUIRED = {
    'CoreFoundation': ['_OBJC_CLASS_$_NSConstantArray', '_OBJC_CLASS_$_NSConstantDictionary',
                       '___NSArray0__struct', '___NSDictionary0__struct'],
    'Foundation': ['_OBJC_CLASS_$_NSConstantIntegerNumber',
                   '_OBJC_CLASS_$_NSConstantDoubleNumber',
                   '_OBJC_CLASS_$_NSConstantFloatNumber'],
}


def frameworks():
    """The framework builder's configuration and compile flags."""
    spec = importlib.util.spec_from_file_location(
        'riot_electron43_frameworks', ROOT / 'tools/build-riot-electron43-frameworks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def missing_symbols(binary, required):
    out = subprocess.run(['llvm-nm', '-gU', '--arch=x86_64', str(binary)],
                         capture_output=True, text=True, check=True).stdout
    exported = {line.split()[-1] for line in out.splitlines() if line.strip()}
    return [symbol for symbol in required if symbol not in exported]


def overlay(config):
    prefix, home = runtime_paths(config)
    guest_root = '/' + str(home.relative_to(prefix))
    return home / 'RuntimeFrameworks', guest_root


def build(frameworks, config, folder):
    flags = frameworks.compiled_flags()
    folder.mkdir(parents=True, exist_ok=True)
    obj = folder / 'constant-literal-check.o'
    binary = folder / 'constant-literal-check'
    subprocess.run([
        'clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
        '-O1', '-c', str(SOURCE), '-o', str(obj),
    ], check=True)
    subprocess.run([
        'clang', '-target', 'x86_64-apple-darwin20', '-fuse-ld=lld', '-nostdlib',
        '-isysroot', config['sdk'], '-L', str(Path(config['sdk']) / 'usr/lib'),
        str(obj), '-lSystem', '-framework', 'Foundation', '-o', str(binary),
    ], check=True)
    return binary


def run(frameworks, config, binary):
    overlay_dir, guest_root = overlay(config)
    libraries = {
        'CoreFoundation': overlay_dir / 'CoreFoundation.framework/Versions/A/CoreFoundation',
        'Foundation': overlay_dir / 'Foundation.framework/Versions/C/Foundation',
    }
    problems = []
    for name, path in libraries.items():
        if not path.is_file():
            problems.append(f'{path} is missing')
            continue
        problems += [f'{path} does not export {symbol}'
                     for symbol in missing_symbols(path, REQUIRED[name])]
    if problems:
        raise SystemExit('\n'.join(problems) + '\n'
                         'Build and install the overlays from the prepared tree first; '
                         'see docs/TROUBLESHOOTING.md ("constant literals").')
    return subprocess.run([
        sys.executable, str(ROOT / 'build/runtime/tools/exec-guest.py'), 'env',
        f'DYLD_FRAMEWORK_PATH={guest_root}/RuntimeFrameworks',
        'darling', str(binary),
    ], text=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--keep', action='store_true',
                        help='keep the built check under build/logs/constant-literals')
    args = parser.parse_args()
    module = frameworks()
    config = module.configuration()
    folder = ROOT / 'build/logs/constant-literals'
    binary = build(module, config, folder)
    result = run(module, config, binary)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if not args.keep:
        for path in (binary, folder / 'constant-literal-check.o'):
            path.unlink(missing_ok=True)
    if result.returncode != 0 or 'PASS constant literals' not in result.stdout:
        raise SystemExit(result.returncode or 1)


if __name__ == '__main__':
    main()
