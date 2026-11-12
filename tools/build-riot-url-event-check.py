#!/usr/bin/env python3
"""Build RiotURLEventCheck.app, the guest check for URL events to running apps.

The bundle's Info.plist declares the URL schemes the check opens; run
build/RiotURLEventCheck.app/Contents/MacOS/RiotURLEventCheck inside the guest.
"""
from pathlib import Path
import plistlib
import shlex
import subprocess

root = Path(__file__).resolve().parent.parent
appkit = root / "build/darling-src/build/src/external/cocotron/AppKit"
flags = {}
for line in (appkit / "CMakeFiles/AppKit.dir/flags.make").read_text().splitlines():
    if line.startswith("C_") and " = " in line:
        key, value = line.split(" = ", 1)
        flags[key] = shlex.split(value)

source = root / "tools/riot-url-event-check.m"
obj = root / "build/riot-url-event-check.o"
bundle = root / "build/RiotURLEventCheck.app/Contents"
(bundle / "MacOS").mkdir(parents=True, exist_ok=True)
with open(bundle / "Info.plist", "wb") as info:
    plistlib.dump({
        "CFBundleExecutable": "RiotURLEventCheck",
        "CFBundleIdentifier": "org.darling-lol.url-event-check",
        "CFBundleName": "RiotURLEventCheck",
        "CFBundlePackageType": "APPL",
        "CFBundleURLTypes": [
            {"CFBundleURLName": "Apple event handler", "CFBundleURLSchemes": ["darling-lol-ae", "DARLING-LOL-AE"]},
            {"CFBundleURLName": "Delegate fallback", "CFBundleURLSchemes": ["darling-lol-delegate"]},
        ],
    }, info)
subprocess.run(["clang", *flags["C_DEFINES"], *flags["C_INCLUDES"],
                *flags["C_FLAGS"], "-fblocks", "-c", str(source),
                "-o", str(obj)], check=True)
subprocess.run(["clang", "-target", "x86_64-apple-macosx11.0",
                "-fuse-ld=lld", "-nostdlib", "-isysroot",
                "@DARLING_SDK@", str(obj), "-lSystem",
                "-framework", "AppKit", "-framework", "Foundation",
                "-framework", "CoreServices", "-framework", "CoreGraphics",
                "-lobjc", "-o", str(bundle / "MacOS/RiotURLEventCheck")],
               check=True)
