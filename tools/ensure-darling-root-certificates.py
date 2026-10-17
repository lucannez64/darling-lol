#!/usr/bin/env python3
"""Ensure system root certificates and SystemTrustSettings.plist exist in Darling."""
import datetime
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import ssl
import subprocess

prefix = Path(os.environ.get("DPREFIX", str(Path.home() / ".darling")))
keychains_dir = prefix / "System/Library/Keychains"
root_kc = keychains_dir / "SystemRootCertificates.keychain"
plist_path = keychains_dir / "SystemTrustSettings.plist"

if root_kc.is_file() and plist_path.is_file():
    exit(0)

keychains_dir.mkdir(parents=True, exist_ok=True)

if root_kc.exists():
    root_kc.unlink()

try:
    subprocess.run(["darling", "shell", "security", "create-keychain", "-p", "",
                    "/System/Library/Keychains/SystemRootCertificates.keychain"], check=True)
except Exception as e:
    print(f"Warning: Failed to create root keychain: {e}")
    exit(0)

ctx = ssl.create_default_context()
der_certs = ctx.get_ca_certs(binary_form=True)

tmp_certs_dir = Path("/tmp/darling_root_certs")
if tmp_certs_dir.exists():
    shutil.rmtree(tmp_certs_dir)
tmp_certs_dir.mkdir(parents=True)

now = datetime.datetime.now(datetime.timezone.utc)
trust_list = {}

for idx, der in enumerate(der_certs):
    cert_file = tmp_certs_dir / f"cert_{idx}.der"
    cert_file.write_bytes(der)
    sha1_hex = hashlib.sha1(der).hexdigest().upper()
    trust_list[sha1_hex] = {
        "issuerName": b"Apple Root CA",
        "serialNumber": b"\x01",
        "modDate": now,
    }

cert_files = [f"/Volumes/SystemRoot{tmp_certs_dir / f'cert_{i}.der'}" for i in range(len(der_certs))]
for chunk_start in range(0, len(cert_files), 50):
    chunk = cert_files[chunk_start:chunk_start + 50]
    subprocess.run(["darling", "shell", "security", "add-certificates", "-k",
                    "/System/Library/Keychains/SystemRootCertificates.keychain", *chunk], check=True)

shutil.rmtree(tmp_certs_dir, ignore_errors=True)

plist_data = {
    "trustVersion": 1,
    "trustList": trust_list
}
with open(plist_path, "wb") as f:
    plistlib.dump(plist_data, f, fmt=plistlib.FMT_XML)

print(f"Provisioned Darling system root certificates ({len(trust_list)} roots)")
