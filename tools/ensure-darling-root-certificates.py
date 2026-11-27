#!/usr/bin/env python3
"""Give Darling's system trust domain the host's CA roots.

Security.framework reads the system domain from two immutable files
(libsecurity_keychain TrustSettingsSchema.h): the root store
/System/Library/Keychains/SystemRootCertificates.keychain and its trust record
SystemTrustSettings.plist. Darling ships neither, so SecTrustSettingsCopyCertificates
for the system domain fails with errSecNoTrustSettings (-25263) and Chromium and
Riot's CertBundle report that they cannot read the system roots.

The trust record is generated here from the host CA bundle. The keychain uses the
guest `security` tool, since no host-side writer for its database format exists.
Darling is only started when the bundle (or this script) changed; a stamp next to
the outputs records the input digest.
"""
import argparse
import base64
import binascii
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile

VERSION = 1
KEYCHAIN = 'SystemRootCertificates.keychain'
TRUST_SETTINGS = 'SystemTrustSettings.plist'
STAMP = '.darling-lol-root-certificates.json'
SYSTEM_ROOT = '/Volumes/SystemRoot'
PEM = re.compile(rb'-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----', re.S)

# One guest invocation: build the keychain in the private work directory, list what
# it holds, then publish both files from inside the guest so a running Darling sees
# them (host writes to the prefix are invisible to a mounted overlay). The guest
# cannot create directories in the root-owned base layer; the host then publishes.
GUEST_SCRIPT = r'''#!/bin/sh
set -u
PATH=$PATH:/usr/bin:/bin
work=$1
kc=$work/SystemRootCertificates.keychain
security create-keychain -p '' "$kc" || exit 10
security unlock-keychain -p '' "$kc" || exit 11
# Certificates the guest cannot parse are reported and left out; the host checks found.txt.
security add-certificates -k "$kc" "$work"/certs/*.cer 2> "$work/add-errors.txt"
security find-certificate -a -Z "$kc" > "$work/found.txt" || exit 12
grep -q 'SHA-1 hash:' "$work/found.txt" || exit 13
dest=/System/Library/Keychains
if [ -d "$dest" ] && [ -w "$dest" ] &&
   cp "$kc" "$dest/.SystemRootCertificates.keychain.next" 2>/dev/null &&
   cp "$work/SystemTrustSettings.plist" "$dest/.SystemTrustSettings.plist.next" 2>/dev/null &&
   mv -f "$dest/.SystemRootCertificates.keychain.next" "$dest/SystemRootCertificates.keychain" &&
   mv -f "$dest/.SystemTrustSettings.plist.next" "$dest/SystemTrustSettings.plist"; then
  : > "$work/placed"
else
  rm -f "$dest/.SystemRootCertificates.keychain.next" "$dest/.SystemTrustSettings.plist.next" 2>/dev/null
fi
exit 0
'''


class Failure(Exception):
    pass


def default_bundle():
    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile, '/etc/ssl/certs/ca-certificates.crt',
                      '/etc/pki/tls/certs/ca-bundle.crt', '/etc/ssl/ca-bundle.pem', '/etc/ssl/cert.pem'):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise Failure('no host CA bundle found; pass --bundle')


def der_element(data, offset):
    """Return (tag, content start, end) of the DER element at offset."""
    if offset + 2 > len(data):
        raise ValueError('truncated element')
    tag = data[offset]
    if tag & 0x1f == 0x1f:
        raise ValueError('high tag numbers are not used in certificates')
    length = data[offset + 1]
    start = offset + 2
    if length & 0x80:
        count = length & 0x7f
        if count == 0 or count > 4 or start + count > len(data):
            raise ValueError('unsupported length')
        length = int.from_bytes(data[start:start + count], 'big')
        start += count
    end = start + length
    if end > len(data):
        raise ValueError('truncated element')
    return tag, start, end


def parse_certificate(der):
    """Fields TrustSettings::copyIssuerAndSerial stores: the issuer Name as encoded
    (CSSMOID_X509V1IssuerNameStd) and the serial INTEGER's content octets."""
    tag, start, end = der_element(der, 0)
    if tag != 0x30 or end != len(der):
        raise ValueError('not a single DER certificate')
    tag, offset, tbs_end = der_element(der, start)
    if tag != 0x30:
        raise ValueError('missing TBSCertificate')
    tag, content, end = der_element(der, offset)
    if tag == 0xa0:  # explicit version
        offset = end
        tag, content, end = der_element(der, offset)
    if tag != 0x02 or content == end:
        raise ValueError('missing serial number')
    serial = der[content:end]
    offset = end
    for name in ('signature', 'issuer', 'validity', 'subject'):
        tag, content, end = der_element(der, offset)
        if tag != 0x30 or end > tbs_end:
            raise ValueError(f'missing {name}')
        if name == 'issuer':
            issuer = der[offset:end]
        elif name == 'subject':
            subject = der[offset:end]
        offset = end
    return {'sha1': hashlib.sha1(der).hexdigest().upper(), 'issuer': issuer,
            'serial': serial, 'subject': subject, 'der': der}


def load_roots(data, report=print):
    roots, seen, identities, skipped = [], set(), {}, []
    for index, match in enumerate(PEM.finditer(data)):
        try:
            der = base64.b64decode(b''.join(match.group(1).split()), validate=True)
            cert = parse_certificate(der)
        except (binascii.Error, ValueError) as error:
            skipped.append(f'#{index + 1}: unreadable ({error})')
            continue
        if cert['sha1'] in seen:
            continue
        seen.add(cert['sha1'])
        if cert['subject'] != cert['issuer']:
            skipped.append(f'{cert["sha1"]}: not self-issued, not a root')
            continue
        # The keychain keys certificates by issuer and serial; a second one is a duplicate item.
        identity = (cert['issuer'], cert['serial'])
        if identity in identities:
            skipped.append(f'{cert["sha1"]}: same issuer and serial as {identities[identity]}')
            continue
        identities[identity] = cert['sha1']
        roots.append(cert)
    for line in skipped:
        report(f'Root certificates: skipped {line}')
    return roots


def trust_settings(roots, now):
    # An empty trustSettings array means "trusted as root for every use"
    # (TrustSettings::evaluateCert / qualifyUsageWithCertDict, numSpecs == 0).
    return plistlib.dumps({
        'trustVersion': 1,
        'trustList': {cert['sha1']: {'issuerName': cert['issuer'], 'serialNumber': cert['serial'],
                                     'modDate': now, 'trustSettings': []} for cert in roots},
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def bundle_digest(data):
    return hashlib.sha256(b'darling-root-certificates-v%d\0' % VERSION + data).hexdigest()


def current(folder, digest):
    try:
        stamp = json.loads((folder / STAMP).read_text())
    except (OSError, ValueError):
        return False
    return (stamp.get('digest') == digest and (folder / KEYCHAIN).is_file()
            and (folder / TRUST_SETTINGS).is_file())


def server_running(prefix):
    try:
        pid = int((prefix / '.init.pid').read_text().strip())
        status = (Path('/proc') / str(pid) / 'status').read_text()
    except (OSError, ValueError):
        return False
    return not any(line.split()[1:2] == ['Z'] for line in status.splitlines() if line.startswith('State:'))


def publish_host(work, folder):
    folder.mkdir(parents=True, exist_ok=True)
    for name in (KEYCHAIN, TRUST_SETTINGS):
        temporary = folder / f'.{name}.next'
        shutil.copyfile(work / name, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, folder / name)


def write_stamp(folder, digest, bundle, roots, present):
    temporary = folder / (STAMP + '.next')
    temporary.write_text(json.dumps({'version': VERSION, 'digest': digest, 'bundle': str(bundle),
                                     'roots': len(roots), 'in_keychain': len(present)}, indent=1) + '\n')
    os.replace(temporary, folder / STAMP)


def ensure(prefix, bundle, darling, force=False, timeout=600):
    data = bundle.read_bytes()
    digest = bundle_digest(data)
    folder = prefix / 'System/Library/Keychains'
    if not force and current(folder, digest):
        return 0
    roots = load_roots(data)
    if not roots:
        raise Failure(f'no root certificates in {bundle}')
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    work = Path(tempfile.mkdtemp(prefix='darling-root-certificates-')).resolve()
    keep = True
    try:
        (work / 'certs').mkdir()
        for index, cert in enumerate(roots):
            (work / 'certs' / f'{index:04d}.cer').write_bytes(cert['der'])
        (work / TRUST_SETTINGS).write_bytes(trust_settings(roots, now))
        script = work / 'build.sh'
        script.write_text(GUEST_SCRIPT)
        # Before Darling starts, the overlay picks up a host-created directory, which
        # lets the guest publish into it.
        running = server_running(prefix)
        if not running:
            folder.mkdir(parents=True, exist_ok=True)
        print(f'Building Darling system root store from {len(roots)} certificates in {bundle}', flush=True)
        try:
            with open(work / 'guest.log', 'wb') as log:
                result = subprocess.run([*darling, 'shell', '/bin/sh', SYSTEM_ROOT + str(script),
                                         SYSTEM_ROOT + str(work)], stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            raise Failure(f'could not run the guest security tool: {error}; work files kept in {work}')
        if result.returncode or not (work / 'found.txt').is_file():
            raise Failure(f'guest keychain build failed (exit {result.returncode}); see {work}/guest.log')
        found = set(re.findall(r'SHA-1 hash:\s*([0-9A-Fa-f]{40})', (work / 'found.txt').read_text(errors='replace')))
        found = {value.upper() for value in found}
        wanted = {cert['sha1'] for cert in roots}
        present = wanted & found
        if not present:
            raise Failure(f'the guest keychain holds none of the certificates; see {work}')
        if present != wanted:
            print(f'Root certificates: the guest rejected {len(wanted - present)} of {len(wanted)}; '
                  f'see {work}/add-errors.txt', file=sys.stderr, flush=True)
            # Kept for inspection; the outputs are still published.
        else:
            keep = False
        if not (work / 'placed').exists():
            # A running guest could not see a new directory; publish on the host for
            # the next Darling start.
            publish_host(work, folder)
            if running:
                print('Darling system root store installed; it takes effect after `darling shutdown`',
                      file=sys.stderr, flush=True)
        write_stamp(folder, digest, bundle, roots, present)
        print(f'Darling system root store has {len(present)} certificates', flush=True)
        return 0
    except OSError as error:
        raise Failure(f'{error}; work files kept in {work}')
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--bundle', type=Path, help='PEM CA bundle (default: the host system bundle)')
    parser.add_argument('--prefix', type=Path, default=Path(os.environ.get('DPREFIX', str(Path.home() / '.darling'))))
    parser.add_argument('--darling', default='darling', help='Darling command (for tests)')
    parser.add_argument('--force', action='store_true', help='rebuild even when the stamp is current')
    parser.add_argument('--check', action='store_true',
                        help='exit 0 when the root store is current without starting Darling')
    args = parser.parse_args(argv)
    if args.check:
        try:
            bundle = args.bundle or default_bundle()
            folder = args.prefix.expanduser() / 'System/Library/Keychains'
            return 0 if current(folder, bundle_digest(bundle.read_bytes())) else 1
        except (Failure, OSError):
            return 1
    try:
        return ensure(args.prefix.expanduser(), args.bundle or default_bundle(), args.darling.split(), args.force)
    except (Failure, OSError) as error:
        print(f'Darling system root certificates were not installed: {error}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
