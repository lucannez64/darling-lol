#!/usr/bin/env python3
"""Build the frameworks Electron 43 loads but Darling does not provide.

Riot Client 142 (Electron 43 / Chromium 150) has non-weak imports from
QuickLookUI, MediaPlayer, CoreML, CoreHaptics and Accessibility. Without those
images dyld aborts before the client's first window, whatever else works.

Each source here implements only the classes and constants that the Electron
Framework binds, with local state and no service behind it: a media key does
nothing, a haptic pattern never plays, CoreML refuses to load a model. The
frameworks are installed into the guest's RuntimeFrameworks overlay, the same
mechanism the AppKit, Metal and Foundation overlays use, so the prefix and the
installed SDK are untouched.

    python3 tools/build-riot-electron43-frameworks.py
    python3 tools/build-riot-electron43-frameworks.py --install
    python3 tools/build-riot-electron43-frameworks.py --check 'path/to/Electron Framework'

--check lists every non-weak import of the given Mach-O against these five
frameworks and fails if one is not defined by the stubs, so an Electron update
that needs a new class shows up as a build error instead of a dyld abort.

Sources are compiled with the AppKit target's own configured flags, which is
what makes the Cocotron/SDK headers resolve without re-deriving them here.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / 'tools/riot-electron43-frameworks'
OUTPUT = ROOT / 'build/riot-frameworks'
APPKIT = ROOT / 'build/darling-src/build/src/external/cocotron/AppKit'

# (extra link flags, symbols that must be exported)
FRAMEWORKS = {
    'QuickLookUI': (['-framework', 'AppKit'], ['_OBJC_CLASS_$_QLPreviewPanel']),
    'MediaPlayer': ([], [
        '_MPMediaItemPropertyAlbumTitle', '_MPMediaItemPropertyArtist',
        '_MPMediaItemPropertyArtwork', '_MPMediaItemPropertyPlaybackDuration',
        '_MPMediaItemPropertyTitle', '_MPNowPlayingInfoPropertyCurrentPlaybackDate',
        '_MPNowPlayingInfoPropertyElapsedPlaybackTime', '_MPNowPlayingInfoPropertyPlaybackRate',
        '_OBJC_CLASS_$_MPMediaItemArtwork', '_OBJC_CLASS_$_MPNowPlayingInfoCenter',
        '_OBJC_CLASS_$_MPRemoteCommandCenter']),
    'CoreML': ([], [
        '_OBJC_CLASS_$_MLFeatureValue', '_OBJC_CLASS_$_MLModel',
        '_OBJC_CLASS_$_MLModelConfiguration', '_OBJC_CLASS_$_MLMultiArray',
        '_OBJC_CLASS_$_MLPredictionOptions', '_OBJC_CLASS_$_MLCPUComputeDevice',
        '_OBJC_CLASS_$_MLGPUComputeDevice', '_OBJC_CLASS_$_MLNeuralEngineComputeDevice',
        '_OBJC_CLASS_$_MLComputePlan']),
    'CoreHaptics': ([], [
        '_CHHapticDynamicParameterIDHapticIntensityControl',
        '_CHHapticDynamicParameterIDHapticSharpnessControl',
        '_CHHapticEventParameterIDHapticIntensity',
        '_CHHapticEventParameterIDHapticSharpness',
        '_CHHapticEventTypeHapticContinuous',
        '_OBJC_CLASS_$_CHHapticDynamicParameter', '_OBJC_CLASS_$_CHHapticEvent',
        '_OBJC_CLASS_$_CHHapticEventParameter', '_OBJC_CLASS_$_CHHapticPattern']),
    'Accessibility': ([], [
        '_OBJC_CLASS_$_AXCustomContent',
        '_AXPrefersNonBlinkingTextInsertionIndicator',
        '_AXPrefersNonBlinkingTextInsertionIndicatorDidChangeNotification']),
}


def configuration():
    path = ROOT / 'build/config.json'
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise SystemExit('run scripts/configure.py first (build/config.json is missing)')


def compiled_flags():
    """The AppKit target's configured defines, includes and flags."""
    file = APPKIT / 'CMakeFiles/AppKit.dir/flags.make'
    if not file.is_file():
        raise SystemExit(f'{file} is missing; build the prepared tree first '
                         '(see docs/SETUP.md)')
    flags = {}
    resource = subprocess.check_output(['clang', '-print-resource-dir'], text=True).strip()
    for line in file.read_text().splitlines():
        if line.startswith('C_') and ' = ' in line:
            name, value = line.split(' = ', 1)
            # The saved flags name the clang resource directory of the compiler
            # that configured the tree; follow the installed one.
            flags[name] = [re.sub(r'/usr/lib/clang/[^/]+/include', resource + '/include', part)
                           for part in shlex.split(value)]
    flags['C_DEFINES'] = [item for item in flags['C_DEFINES'] if not item.endswith('AppKit_EXPORTS')]
    return flags


def guest_home():
    config = configuration()
    prefix = Path(os.environ.get('DPREFIX') or config['prefix'])
    return prefix / config['guest_home'].lstrip('/')


def build():
    flags = compiled_flags()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, (extra, _) in FRAMEWORKS.items():
        source = SOURCES / f'{name}.m'
        bundle = OUTPUT / f'{name}.framework'
        binary = bundle / name
        bundle.mkdir(parents=True, exist_ok=True)
        obj = ROOT / f'build/riot-{name}.o'
        subprocess.run([
            'clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
            '-fobjc-arc', '-fblocks', '-O2', '-c', str(source), '-o', str(obj),
        ], check=True)
        subprocess.run([
            'clang', '-target', 'x86_64-apple-darwin20', '-fuse-ld=lld', '-nostdlib',
            '-isysroot', configuration()['sdk'],
            '-L', str(Path(configuration()['sdk']) / 'usr/lib'),
            str(obj), '-lSystem', '-lobjc', *extra, '-framework', 'Foundation',
            '-dynamiclib', '-install_name',
            f'/System/Library/Frameworks/{name}.framework/Versions/A/{name}',
            '-o', str(binary),
        ], check=True)
        link_versions(bundle, name)
        verify(binary, FRAMEWORKS[name][1])
    return [OUTPUT / f'{name}.framework' for name in FRAMEWORKS]


def link_versions(bundle, name):
    """Add the versioned layout dyld looks for first; the flat one is the same file."""
    versioned = bundle / 'Versions/A'
    versioned.mkdir(parents=True, exist_ok=True)
    current = bundle / 'Versions/Current'
    if not current.is_symlink():
        current.symlink_to('A')
    target = versioned / name
    if target.is_symlink() and os.readlink(target) != f'../../{name}':
        target.unlink()
    if not target.exists():
        target.symlink_to(f'../../{name}')
    (bundle / 'Resources').mkdir(exist_ok=True)
    (bundle / 'Resources/Info.plist').write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>'
        f'<key>CFBundleExecutable</key><string>{name}</string>'
        f'<key>CFBundleIdentifier</key><string>com.darling-lol.{name}</string>'
        f'<key>CFBundleName</key><string>{name}</string>'
        '<key>CFBundlePackageType</key><string>FMWK</string>'
        '</dict></plist>\n')


def exported(binary):
    out = subprocess.run(['llvm-nm', '-gU', '--arch=x86_64', str(binary)],
                         capture_output=True, text=True, check=True).stdout
    return {line.split()[-1] for line in out.splitlines() if line.strip()}


def verify(binary, required):
    have = exported(binary)
    missing = [symbol for symbol in required if symbol not in have]
    if missing:
        raise SystemExit(f'{binary}: missing {", ".join(missing)}')


def imports(binary, framework):
    """Non-weak imports of one framework, from `nm -m`'s "from <framework>" tag."""
    out = subprocess.run(['llvm-nm', '-m', '-u', '--arch=x86_64', str(binary)],
                         capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines():
        if re.search(r'\bweak\b', line.split('(from', 1)[0]) or f'(from {framework})' not in line:
            continue
        found.append(line.split()[-3])
    return found


def check(binary):
    """Fail when the given Mach-O imports something these stubs do not define."""
    failures = 0
    for name in FRAMEWORKS:
        wanted = imports(binary, name)
        have = exported(OUTPUT / f'{name}.framework' / name)
        missing = [symbol for symbol in wanted if symbol not in have]
        print(f'{name}: {len(wanted)} import(s), {len(missing)} missing')
        for symbol in missing:
            print(f'  missing {symbol}')
        failures += len(missing)
    return 1 if failures else 0


def install(backup):
    home = guest_home()
    target = home / 'RuntimeFrameworks'
    target.mkdir(parents=True, exist_ok=True)
    backup = Path(backup)
    installed = []
    for name in FRAMEWORKS:
        source = OUTPUT / f'{name}.framework' / name
        destination = target / f'{name}.framework'
        destination.mkdir(parents=True, exist_ok=True)
        final = destination / name
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if final.exists() and hashlib.sha256(final.read_bytes()).hexdigest() == digest:
            link_versions(destination, name)
            installed.append({'framework': name, 'path': str(final), 'already_current': True})
            continue
        kept = None
        if final.exists():
            backup.mkdir(parents=True, exist_ok=True)
            kept = backup / f'{name}-{digest[:16]}.dylib'
            shutil.copy2(final, kept)
        staged = final.with_name(final.name + '.new')
        shutil.copy2(source, staged)
        os.replace(staged, final)
        link_versions(destination, name)
        installed.append({'framework': name, 'path': str(final),
                          'backup': str(kept) if kept else None})
    print(json.dumps({'installed': installed, 'activation':
                      'guest processes must restart to load the new frameworks'}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--install', action='store_true',
                        help='install the built frameworks into the guest overlay')
    parser.add_argument('--backup-dir', default=str(ROOT / 'build/framework-backups'),
                        help='where replaced overlay files are kept')
    parser.add_argument('--check', metavar='MACHO',
                        help='list imports the stubs do not cover and exit non-zero')
    args = parser.parse_args()
    build()
    if args.check:
        raise SystemExit(check(Path(args.check).expanduser()))
    if args.install:
        install(args.backup_dir)


if __name__ == '__main__':
    main()
