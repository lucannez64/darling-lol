#!/usr/bin/env python3
"""Check an overlay dylib against the library it replaces, statically.

An overlay is only safe to install if the guest cannot tell the two apart:
same install name, same versions, same dependent libraries and re-exports, the
same exported symbols, no new undefined symbol, and the same segment
protections. The optimization estimate of tools/riot_overlay_audit.py has to
improve. Nothing here runs a guest process.

Usage: riot_overlay_verify.py <overlay.dylib> <effective.dylib> [--allow-missing-symbol]
"""
import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llvm_tools

ROOT = Path(__file__).resolve().parent.parent


def load_audit():
    path = ROOT / "tools/riot_overlay_audit.py"
    spec = importlib.util.spec_from_file_location("riot_overlay_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*arguments):
    """Run an LLVM tool, resolving versioned names from PATH or llvm-config."""
    return subprocess.run((llvm_tools.require(arguments[0]), *arguments[1:]),
                          capture_output=True, text=True, errors="replace").stdout


def headers(path):
    return run("llvm-objdump", "--macho", "--private-headers", str(path))


def identity(path):
    """Install name and the two versions from LC_ID_DYLIB."""
    text = headers(path)
    match = re.search(r"cmd LC_ID_DYLIB\n.*?\n\s+name (\S+).*?\n.*?\n\s+current version ([\d.]+)\n"
                      r"compatibility version ([\d.]+)", text, re.S)
    if not match:
        return None
    return {"name": match.group(1), "current": match.group(2), "compatibility": match.group(3)}


def dependencies(path):
    text = run("llvm-objdump", "--macho", "--dylibs-used", str(path))
    return sorted(re.findall(r"^\t(\S+) \(compatibility version", text, re.M))


def reexports(path):
    text = headers(path)
    return sorted(re.findall(r"cmd LC_REEXPORT_DYLIB\n.*?\n\s+name (\S+)", text, re.S))


def segments(path):
    protections = {}
    name = None
    for line in headers(path).splitlines():
        match = re.match(r"\s+segname (\S+)", line)
        if match:
            name = match.group(1)
        match = re.match(r"\s+(initprot|maxprot) (\S+)", line)
        if match and name:
            protections.setdefault(name, {})[match.group(1)] = match.group(2)
    return protections


def weak_definitions(path):
    """Symbols the library defines only weakly: the -O0 build emits every
    inline function out of line, an optimized build may drop the unused ones,
    and the ABI makes a weak definition optional."""
    text = run("llvm-nm", "-m", str(path))
    weak = set()
    for line in text.splitlines():
        if "weak external" in line:
            weak.add(line.split()[-1])
    return weak


def symbols(path, flag):
    text = run("llvm-nm", flag, str(path))
    found = set()
    for line in text.splitlines():
        parts = line.split()
        if flag == "-gU" and len(parts) == 3 and parts[1] in "TDBRS":
            found.add(parts[2])
        elif flag == "-u" and len(parts) == 2:
            found.add(parts[1])
    return found


def verify(overlay, effective, allow_missing):
    audit = load_audit()
    failures, notes = [], []
    if identity(overlay) != identity(effective):
        failures.append("install name or versions differ: %s vs %s" % (identity(overlay), identity(effective)))
    if dependencies(overlay) != dependencies(effective):
        notes.append("dependent libraries differ:\n  overlay:   %s\n  effective: %s"
                     % (dependencies(overlay), dependencies(effective)))
    if reexports(overlay) != reexports(effective):
        notes.append("re-exports differ: %s vs %s" % (reexports(overlay), reexports(effective)))
    if segments(overlay) != segments(effective):
        failures.append("segment protections differ: %s vs %s" % (segments(overlay), segments(effective)))
    overlay_exports, effective_exports = symbols(overlay, "-gU"), symbols(effective, "-gU")
    missing = effective_exports - overlay_exports
    added = overlay_exports - effective_exports
    weak = weak_definitions(effective)
    missing_strong = missing - weak
    if missing & weak:
        notes.append("weak definitions dropped by the optimized build (optional by ABI): %s"
                     % sorted(missing & weak)[:20])
    if missing_strong and not allow_missing:
        failures.append("exported symbols missing from the overlay: %s" % sorted(missing_strong)[:20])
    if added:
        notes.append("new exported symbols in the overlay: %s" % sorted(added)[:20])
    overlay_undefined, effective_undefined = symbols(overlay, "-u"), symbols(effective, "-u")
    new_undefined = overlay_undefined - effective_undefined
    if new_undefined:
        failures.append("new undefined symbols: %s" % sorted(new_undefined)[:20])
    total, stack, functions, error = audit.optimization_estimate(overlay)
    before, stack_before, functions_before, _ = audit.optimization_estimate(effective)
    share, share_before = 100.0 * stack / max(total, 1), 100.0 * stack_before / max(before, 1)
    if error:
        failures.append("cannot read the overlay: " + error)
    if share > share_before:
        failures.append("optimization estimate got worse: %.1f%% -> %.1f%% stack references" % (share_before, share))
    print("OVERLAY %s vs %s" % (Path(overlay).name, Path(effective).name))
    print("  exports=%d (was %d) undefined=%d (was %d)" % (len(overlay_exports), len(effective_exports),
                                                           len(overlay_undefined), len(effective_undefined)))
    print("  stack references %.1f%% (was %.1f%%), functions %d (was %d)" % (share, share_before, functions, functions_before))
    for note in notes:
        print("  note: " + note)
    for failure in failures:
        print("  FAIL: " + failure)
    print("OVERLAY_VERIFY pass=%d %s" % (not failures, Path(overlay).name))
    return not failures


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("overlay", type=Path)
    parser.add_argument("effective", type=Path)
    parser.add_argument("--allow-missing-symbol", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    arguments = parse_arguments()
    sys.exit(0 if verify(arguments.overlay, arguments.effective, arguments.allow_missing_symbol) else 1)
