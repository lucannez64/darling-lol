#!/usr/bin/env python3
"""Read/write the quarantine xattr and detect appended notarization trailers.

This is deliberately a *preparation* tool. A trailer proves only that the
package contains a ticket-shaped blob; it does not validate an Apple
notarization ticket or set a truthful `notarized` bit. `notarized` is always
false until a real ticket validator is implemented.
"""
import argparse
import json
import os
import struct
import sys
from pathlib import Path

QUARANTINE = "user.com.apple.quarantine"
MAGIC = b"t8lr"
TRAILER = struct.Struct("<4sHHI4s")
TRAILER_INVALID = 0
TRAILER_TERMINATOR = 1
TRAILER_TICKET = 2
SCAN_BYTES = 4 * 1024 * 1024


def fail(message):
    raise SystemExit(f"dc-notary: {message}")


def quarantine_get(path):
    try:
        value = os.getxattr(path, QUARANTINE)
    except OSError as error:
        if error.errno in (61, 93):  # ENODATA / ENOATTR
            return None
        raise
    return value.decode("utf-8", "replace")


def quarantine_set(path, value):
    os.setxattr(path, QUARANTINE, value.encode("utf-8"))


def quarantine_clear(path):
    try:
        os.removexattr(path, QUARANTINE)
    except OSError as error:
        if error.errno not in (61, 93):
            raise


def find_ticket(path):
    with open(path, "rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        start = max(0, size - SCAN_BYTES)
        source.seek(start)
        data = source.read()
    position = data.rfind(MAGIC)
    while position >= 0:
        if position + TRAILER.size <= len(data):
            magic, version, kind, length, reserved = TRAILER.unpack_from(data, position)
            absolute_position = start + position
            if kind == TRAILER_TICKET and absolute_position >= length:
                return {
                    "present": True,
                    "kind": "ticket",
                    "version": version,
                    "length": length,
                    "offset": absolute_position - length,
                    "validated": False,
                    "notarized": False,
                }
            if kind == TRAILER_TERMINATOR:
                break
        position = data.rfind(MAGIC, 0, position)
    return {
        "present": False,
        "kind": "none",
        "version": 0,
        "length": 0,
        "offset": None,
        "validated": False,
        "notarized": False,
    }


def command_quarantine_get(args):
    value = quarantine_get(args.path)
    if value is None:
        print("absent")
    else:
        print(value)
    return 0


def command_quarantine_set(args):
    quarantine_set(args.path, args.value)
    print(f"set {QUARANTINE} on {args.path}")
    return 0


def command_quarantine_clear(args):
    quarantine_clear(args.path)
    print(f"cleared {QUARANTINE} on {args.path}")
    return 0


def command_ticket_scan(args):
    result = find_ticket(args.path)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"ticket_present={str(result['present']).lower()}")
        print(f"ticket_length={result['length']}")
        print("ticket_validated=false")
        print("notarized=false")
    return 0


def command_assess(args):
    quarantine = quarantine_get(args.path)
    ticket = find_ticket(args.path)
    result = {
        "path": str(args.path),
        "quarantine": {"present": quarantine is not None, "value": quarantine},
        "ticket": ticket,
        "notarized": False,
        "notarization_validation": "not-run",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    get = sub.add_parser("quarantine-get")
    get.add_argument("path", type=Path)
    get.set_defaults(function=command_quarantine_get)

    set_parser = sub.add_parser("quarantine-set")
    set_parser.add_argument("path", type=Path)
    set_parser.add_argument("value")
    set_parser.set_defaults(function=command_quarantine_set)

    clear = sub.add_parser("quarantine-clear")
    clear.add_argument("path", type=Path)
    clear.set_defaults(function=command_quarantine_clear)

    scan = sub.add_parser("ticket-scan")
    scan.add_argument("path", type=Path)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(function=command_ticket_scan)

    assess = sub.add_parser("assess")
    assess.add_argument("path", type=Path)
    assess.set_defaults(function=command_assess)

    args = parser.parse_args(argv)
    try:
        return args.function(args)
    except OSError as error:
        fail(str(error))


if __name__ == "__main__":
    sys.exit(main())
