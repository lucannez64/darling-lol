#!/usr/bin/env python3
"""Run and analyze a small enforce-mode soak without a game client.

`run --smoke` executes a few representative Darling commands under
DARLING_CODESIGN_ENFORCE=enforce, captures their logs, and fails when a
verification/security error appears. It is not the M10 match soak; it is the
repeatable harness needed before that soak can run on a reference machine.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FAILURE_PATTERNS = [
    re.compile(r"mldr: refusing to load", re.I),
    re.compile(r"code signature", re.I),
    re.compile(r"library validation", re.I),
    re.compile(r"segmentation fault", re.I),
    re.compile(r"abort trap", re.I),
    re.compile(r"bus error", re.I),
    re.compile(r"failed to register signing state", re.I),
    re.compile(r"ENOTSUP", re.I),
]
SMOKE_COMMANDS = [
    ("true", ["/usr/bin/true"]),
    ("sw_vers", ["/usr/bin/sw_vers"]),
    ("shell", ["/bin/sh", "-c", "printf 'soak-smoke\\n'"]),
]


def analyze_text(text):
    hits = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for pattern in FAILURE_PATTERNS:
            if pattern.search(line):
                hits.append({"line": line_number, "pattern": pattern.pattern, "text": line[:500]})
                break
    return hits


def command_run(args):
    if args.out is None:
        out = Path(tempfile.mkdtemp(prefix="darling-soak-"))
    else:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["DARLING_CODESIGN_ENFORCE"] = "enforce"
    env.setdefault("DARLING_SIP_ENFORCE", "off")
    results = []
    failures = 0
    for name, command in SMOKE_COMMANDS:
        started = time.monotonic()
        log = out / f"{name}.log"
        try:
            with log.open("w", encoding="utf-8") as output:
                completed = subprocess.run(
                    [args.darling, "shell", *command],
                    env=env, stdout=output, stderr=subprocess.STDOUT,
                    text=True, timeout=args.timeout,
                )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
        duration = time.monotonic() - started
        text = log.read_text(encoding="utf-8", errors="replace")
        hits = analyze_text(text)
        failed = exit_code != 0 or bool(hits)
        if failed:
            failures += 1
        results.append({
            "name": name,
            "command": command,
            "exit": exit_code,
            "duration_seconds": round(duration, 3),
            "log": str(log),
            "failures": hits,
        })
        print(f"{name}: exit={exit_code} duration={duration:.2f}s failures={len(hits)}")
    summary = {
        "mode": "smoke",
        "enforce": True,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
        "failures": failures,
    }
    (out / "soak.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"soak: failures={failures} report={out / 'soak.json'}")
    return 0 if failures == 0 else 1


def command_analyze(args):
    failures = 0
    for path in args.paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = analyze_text(text)
        print(f"{path}: security_findings={len(hits)}")
        for hit in hits:
            print(f"  line {hit['line']}: {hit['text']}")
        failures += len(hits)
    print(f"analyze: security_findings={failures}")
    return 0 if failures == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--smoke", action="store_true", required=True)
    run.add_argument("--darling", default=os.environ.get("DARLING", "darling"))
    run.add_argument("--out", default=None, help="output directory (default: a private temp directory)")
    run.add_argument("--timeout", type=int, default=120)
    run.set_defaults(function=command_run)

    analyze = sub.add_parser("analyze")
    analyze.add_argument("paths", nargs="+", type=Path)
    analyze.set_defaults(function=command_analyze)

    args = parser.parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    sys.exit(main())
