#!/usr/bin/env python3
"""Write a minimal, signable x86-64 Mach-O dylib.

Used by the tests and the fuzz corpus builder when no real Mach-O is
available (for example on a Linux CI runner). rcodesign needs at least one
section to sign, so the file has a single __text section with eight NOPs.

Usage: minimal_macho.py <output-path>
"""
import struct
import sys

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_X86_64 = 0x01000007
MH_DYLIB = 6
LC_SEGMENT_64 = 0x19

TEXT_ADDR = 0x100000200
TEXT_OFFSET = 0x200
TEXT_SIZE = 8


def segment(name, vmaddr, vmsize, fileoff, filesize, prot, sections=b""):
    nsects = 1 if sections else 0
    header = struct.pack(
        "<II16sQQQQIIII",
        LC_SEGMENT_64,
        72 + len(sections),
        name.encode(),
        vmaddr,
        vmsize,
        fileoff,
        filesize,
        prot,
        prot,
        nsects,
        0,
    )
    return header + sections


def main(path):
    text_section = struct.pack(
        "<16s16sQQIIIIIIII",
        b"__text",
        b"__TEXT",
        TEXT_ADDR,
        TEXT_SIZE,
        TEXT_OFFSET,
        4,
        0,
        0,
        0x80000400,
        0,
        0,
        0,
    )
    commands = segment("__TEXT", 0x100000000, 0x1000, 0, 0x1000, 5, text_section)
    commands += segment("__LINKEDIT", 0x100001000, 0x1000, 0x1000, 0, 1)
    header = struct.pack(
        "<IIIIIIII", MH_MAGIC_64, CPU_TYPE_X86_64, 3, MH_DYLIB, 2, len(commands), 0, 0
    )
    data = bytearray(header + commands)
    data += b"\0" * (0x1000 - len(data))
    data[TEXT_OFFSET : TEXT_OFFSET + TEXT_SIZE] = b"\x90" * TEXT_SIZE
    with open(path, "wb") as f:
        f.write(data)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: minimal_macho.py <output-path>")
    main(sys.argv[1])
