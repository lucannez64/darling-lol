#!/usr/bin/env python3
"""Verify the CMS signature and local trust pin on a Mach-O code signature.

This is M9 preparation. It verifies the CMS signature and a caller-supplied
local CA/trust anchor. It does not validate Apple's Developer ID policy,
notarization tickets, or Apple's trust cache, and it always reports
`notarized=false`. Apple's CMS content binding still needs a real-macOS
ground-truth probe; the helper therefore reports `content_binding=unverified`.
"""
import argparse
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

CPU_TYPE_X86_64 = 0x01000007
LC_CODE_SIGNATURE = 0x1D
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_CODEDIRECTORY = 0xFADE0C02
CSMAGIC_BLOBWRAPPER = 0xFADE0B01
CS_BLOB_CODEDIRECTORY = 0
CS_BLOB_CMS = 0x10000


def fail(message):
    raise SystemExit(f"dc-cms-verify: {message}")


def parse_slice(data):
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic == 0xCAFEBABE:
        count = struct.unpack_from(">I", data, 4)[0]
        candidates = []
        for index in range(count):
            cputype, _subtype, offset, size, _align = struct.unpack_from(">IIIII", data, 8 + index * 20)
            candidates.append((cputype, offset, size))
        for cputype, offset, size in candidates:
            if cputype == CPU_TYPE_X86_64:
                return data[offset:offset + size]
        if candidates:
            _, offset, size = candidates[0]
            return data[offset:offset + size]
        fail("fat Mach-O has no slices")
    return data


def find_signature(data):
    if len(data) < 32:
        fail("file is too small to be a Mach-O")
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic in (0xFEEDFACF, 0xCFFAEDFE):
        header = 32
    elif magic in (0xFEEDFACE, 0xCEFAEDFE):
        header = 28
    else:
        fail("not a thin little-endian Mach-O")
    ncmds = struct.unpack_from("<I", data, 16)[0]
    offset = header
    for _ in range(ncmds):
        if offset + 8 > len(data):
            fail("load command table is truncated")
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > len(data):
            fail("invalid load command size")
        if command == LC_CODE_SIGNATURE:
            dataoff, datasize = struct.unpack_from("<II", data, offset + 8)
            if dataoff + datasize > len(data):
                fail("code signature runs past the file")
            return data[dataoff:dataoff + datasize]
        offset += size
    fail("no LC_CODE_SIGNATURE")


def split_superblob(superblob):
    if len(superblob) < 12:
        fail("signature SuperBlob is truncated")
    magic, length, count = struct.unpack_from(">III", superblob, 0)
    if magic != CSMAGIC_EMBEDDED_SIGNATURE:
        fail("bad signature SuperBlob magic")
    if length > len(superblob) or 12 + count * 8 > length:
        fail("signature SuperBlob bounds are invalid")
    code_directory = None
    cms = None
    for index in range(count):
        kind, offset = struct.unpack_from(">II", superblob, 12 + index * 8)
        if offset + 8 > length:
            fail("signature blob index is out of bounds")
        blob_length = struct.unpack_from(">I", superblob, offset + 4)[0]
        if offset + blob_length > length:
            fail("signature blob runs past the SuperBlob")
        blob = superblob[offset:offset + blob_length]
        if kind == CS_BLOB_CODEDIRECTORY:
            if code_directory is not None:
                fail("multiple CodeDirectory blobs")
            if struct.unpack_from(">I", blob, 0)[0] != CSMAGIC_CODEDIRECTORY:
                fail("CodeDirectory blob has the wrong magic")
            code_directory = blob
        elif kind == CS_BLOB_CMS:
            if cms is not None:
                fail("multiple CMS blobs")
            if struct.unpack_from(">I", blob, 0)[0] != CSMAGIC_BLOBWRAPPER:
                fail("CMS blob wrapper has the wrong magic")
            cms = blob[8:]
    if code_directory is None:
        fail("no CodeDirectory blob")
    return code_directory, cms


OPENSSL_TIMEOUT = 60


def run_openssl(args):
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=OPENSSL_TIMEOUT
        )
    except FileNotFoundError:
        fail("openssl not found")
    except subprocess.TimeoutExpired:
        fail(f"openssl timed out after {OPENSSL_TIMEOUT}s")


def signer_fingerprint(cert_path):
    result = run_openssl(["openssl", "x509", "-in", str(cert_path), "-noout", "-fingerprint", "-sha256"])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if "Fingerprint=" in line:
            return line.split("=", 1)[1].strip()
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--ca", type=Path, help="local CA/trust-anchor PEM")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    data = args.path.read_bytes()
    signature = find_signature(parse_slice(data))
    code_directory, cms = split_superblob(signature)
    if cms is None:
        result = {
            "cms_present": False,
            "signature_valid": False,
            "chain": "unverified",
            "signer": None,
            "content_binding": "unverified",
            "notarized": False,
        }
    else:
        with tempfile.TemporaryDirectory(prefix="dc-cms-verify-") as tmp:
            tmp = Path(tmp)
            cms_path = tmp / "cms.der"
            cd_path = tmp / "codedirectory.bin"
            signer_path = tmp / "signer.pem"
            cms_path.write_bytes(cms)
            cd_path.write_bytes(code_directory)
            command = [
                "openssl", "cms", "-verify", "-binary", "-inform", "DER",
                "-in", str(cms_path), "-content", str(cd_path),
                "-purpose", "any", "-no_check_time", "-no_content_verify",
                "-signer", str(signer_path), "-out", "/dev/null",
            ]
            if args.ca is not None:
                command += ["-CAfile", str(args.ca)]
            else:
                command += ["-noverify"]
            verified = run_openssl(command)
            signer = signer_fingerprint(signer_path) if signer_path.exists() else None
            ca_fingerprint = signer_fingerprint(args.ca) if args.ca is not None else None
            if verified.returncode != 0:
                result = {
                    "cms_present": True,
                    "signature_valid": False,
                    "chain": "invalid",
                    "signer": signer,
                    "content_binding": "unverified",
                    "notarized": False,
                    "error": (verified.stderr or verified.stdout).strip().splitlines()[-1:],
                }
            elif args.ca is not None:
                result = {
                    "cms_present": True,
                    "signature_valid": True,
                    "chain": "valid",
                    "signer": signer,
                    "trust_anchor": ca_fingerprint,
                    "content_binding": "unverified",
                    "notarized": False,
                }
            else:
                result = {
                    "cms_present": True,
                    "signature_valid": True,
                    "chain": "unverified",
                    "signer": signer,
                    "content_binding": "unverified",
                    "notarized": False,
                }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"cms_present={str(result['cms_present']).lower()}")
        print(f"signature_valid={str(result['signature_valid']).lower()}")
        print(f"chain={result['chain']}")
        print(f"content_binding={result['content_binding']}")
        print("notarized=false")
    return 0 if result["signature_valid"] and result["chain"] != "invalid" else 1


if __name__ == "__main__":
    sys.exit(main())
