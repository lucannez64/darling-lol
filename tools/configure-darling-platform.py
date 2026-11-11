#!/usr/bin/env python3
"""Preview or explicitly save per-prefix IOKit platform overrides; defaults stay unchanged."""
import argparse
import json
import fcntl
import secrets
from contextlib import nullcontext
import os
from pathlib import Path
import plistlib
import shlex
import tempfile
import uuid
from xml.parsers.expat import ExpatError

MAX_SIZE = 65536
BASE34 = '0123456789ABCDEFGHJKLMNPQRSTUVWXYZ'
# Emulated macOS identity. These MUST match the kernel's compile-time
# EMULATED_OSPRODUCTVERSION / EMULATED_OSVERSION (patches/xnu.patch): the plist
# read by NSProcessInfo and the kern.osproductversion / kern.osversion sysctls
# have to name one macOS, or a single reader sees two versions at once.
EMULATED_PRODUCT_VERSION = '14.7'
EMULATED_BUILD_VERSION = '23H124'
KNOWN_SERIAL = 'C02ZL0GVMD6N'
KNOWN_UUID = '6F3B2A1E-8C4D-4E5F-9A7B-1D2C3E4F5A6B'
STRINGS = {'IOPlatformSerialNumber': 64, 'manufacturer': 128, 'model': 128,
           'product-name': 128, 'board-id': 128, 'mlb-serial-number': 64}
BYTES = {'ROM': 6, 'system-id': 16}
ALIASES = {
    'SystemProductName': 'model', 'Type': 'model', 'SMBIOS_MODEL': 'model',
    'SMBIOS_PRODUCT_NAME': 'product-name', 'SMBIOS_MANUFACTURER': 'manufacturer',
    'SMBIOS_BOARD_ID': 'board-id', 'SystemSerialNumber': 'IOPlatformSerialNumber',
    'Serial': 'IOPlatformSerialNumber', 'SMBIOS_SERIAL_NUMBER': 'IOPlatformSerialNumber',
    'SystemUUID': 'IOPlatformUUID', 'SmUUID': 'IOPlatformUUID', 'SMBIOS_PLATFORM_UUID': 'IOPlatformUUID',
    'MLB': 'mlb-serial-number', 'Board Serial': 'mlb-serial-number', 'SMBIOS_MLB': 'mlb-serial-number',
    'SMBIOS_ROM': 'ROM', 'SMBIOS_SYSTEM_ID': 'system-id',
    'SMBIOS_PROVISIONING_UDID': 'provisioning_UDID',
}


def validate(properties):
    if not isinstance(properties, dict) or len(properties) > 10:
        raise ValueError('properties must be a dictionary of supported identity fields')
    result = {}
    for key, value in properties.items():
        if key in STRINGS:
            if not isinstance(value, str) or not 1 <= len(value) <= STRINGS[key] or any(not 32 <= ord(c) <= 126 for c in value):
                raise ValueError('Invalid printable string for ' + key)
        elif key in ('IOPlatformUUID', 'provisioning_UDID'):
            if not isinstance(value, str):
                raise ValueError(key + ' must be a UUID string')
            parsed = uuid.UUID(value)
            if not parsed.int:
                raise ValueError(key + ' must not be zero')
            value = str(parsed).upper()
        elif key in BYTES:
            if isinstance(value, str):
                value = bytes.fromhex(value.replace(':', '').replace('-', ''))
            if not isinstance(value, bytes) or len(value) != BYTES[key]:
                raise ValueError(key + ' has an invalid byte length')
        else:
            raise ValueError('Unsupported identity field: ' + str(key))
        result[key] = value
    if 'model' in result:
        result.setdefault('product-name', result['model'])
    if 'IOPlatformUUID' in result:
        system_id = uuid.UUID(result['IOPlatformUUID']).bytes_le
        if 'system-id' in result and result['system-id'] != system_id:
            raise ValueError('system-id does not match IOPlatformUUID')
        result['system-id'] = system_id
        if result.get('provisioning_UDID', result['IOPlatformUUID']) != result['IOPlatformUUID']:
            raise ValueError('provisioning_UDID must match IOPlatformUUID')
    return result


def load(source):
    with Path(source).open("rb") as stream:
        raw = stream.read(MAX_SIZE + 1)
    if len(raw) > MAX_SIZE:
        raise ValueError('Identity input exceeds 64 KiB')
    if str(source).endswith('.conf'):
        payload = {}
        for line in raw.decode().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep:
                raise ValueError('Malformed legacy identity line')
            values = shlex.split(value, comments=True)
            if len(values) != 1 or key.strip() in payload:
                raise ValueError('Malformed or duplicate legacy identity field')
            payload[key.strip()] = values[0]
    else:
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('Duplicate JSON identity field: ' + key)
                result[key] = value
            return result
        if raw.lstrip()[:1] in (b'{', b'['):
            payload = json.loads(raw, object_pairs_hook=unique_keys)
        else:
            payload = plistlib.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('Identity input must be a dictionary')
    if 'schema' in payload:
        if set(payload) != {'schema', 'properties'} or type(payload['schema']) is not int or payload['schema'] != 1:
            raise ValueError('Unsupported platform identity schema')
        return validate(payload['properties'])
    if 'PlatformInfo' in payload:
        payload = payload['PlatformInfo'].get('Generic') if isinstance(payload['PlatformInfo'], dict) else None
    elif 'Generic' in payload:
        payload = payload['Generic']
    if not isinstance(payload, dict):
        raise ValueError('Missing OpenCore PlatformInfo/Generic dictionary')
    result = {}
    for key, value in payload.items():
        name = ALIASES.get(key, key)
        if name not in {*STRINGS, *BYTES, 'IOPlatformUUID', 'provisioning_UDID'}:
            continue # OpenCore includes unrelated boot, firmware and CPU settings.
        if name in result and result[name] != value:
            raise ValueError('Conflicting imports for ' + name)
        result[name] = value
    if not result:
        raise ValueError('No supported identity fields found')
    return validate(result)


def checked_path(prefix, relative):
    prefix = Path(prefix).expanduser()
    if not prefix.is_absolute():
        raise ValueError('Prefix must be absolute')
    path = prefix / relative
    if path.is_symlink() or not path.resolve().is_relative_to(prefix.resolve()):
        raise ValueError('Identity destination must remain inside the selected prefix')
    return path


def destination(prefix):
    return checked_path(prefix, 'private/etc/darling-platform.plist')


def ensure_stopped(prefix):
    marker = Path(prefix) / '.init.pid'
    if marker.exists():
        text = marker.read_text().strip()
        if not text.isdecimal() or int(text) <= 1:
            raise ValueError('Invalid .init.pid; verify the selected prefix is stopped before writing')
        try:
            os.kill(int(text), 0)
        except ProcessLookupError:
            return
        raise ValueError('Shut down the selected Darling prefix before writing platform identity')


def atomic_write(path, data):
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + '.before-platform-change')
        if not backup.exists():
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
    fd, temporary = tempfile.mkstemp(prefix='.darling-platform-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def save(prefix, properties):
    path = destination(prefix)
    ensure_stopped(prefix)
    encoded = plistlib.dumps({'schema': 1, 'properties': validate(properties)}, sort_keys=True)
    checked_path(prefix, 'private/etc/darling-platform.plist.before-platform-change')
    if path.exists():
        load(path)  # refuse to silently overwrite an unknown schema
    path.parent.mkdir(parents=True, exist_ok=True)
    return atomic_write(destination(prefix), encoded)


def has_known_defaults(properties):
    """True when an identity still carries the shared serial, UUID, or a field derived from that UUID."""
    return (properties.get('IOPlatformSerialNumber') == KNOWN_SERIAL
            or str(properties.get('IOPlatformUUID', '')).upper() == KNOWN_UUID
            or str(properties.get('provisioning_UDID', '')).upper() == KNOWN_UUID
            or properties.get('system-id') == uuid.UUID(KNOWN_UUID).bytes_le)


def provision(properties, rom_oui=None):
    """Fill missing identifiers, replacing only the two known shared defaults."""
    result = validate(properties)
    if result.get('IOPlatformSerialNumber') == KNOWN_SERIAL:
        result.pop('IOPlatformSerialNumber')
    if (result.get('IOPlatformUUID', '').upper() == KNOWN_UUID
            or result.get('provisioning_UDID', '').upper() == KNOWN_UUID
            or result.get('system-id') == uuid.UUID(KNOWN_UUID).bytes_le):
        result.pop('IOPlatformUUID', None)
        result.pop('system-id', None)
        result.pop('provisioning_UDID', None)
    for key, length in [('IOPlatformSerialNumber', 12), ('mlb-serial-number', 17)]:
        if key not in result:
            result[key] = ''.join(secrets.choice(BASE34) for _ in range(length))
    if 'IOPlatformUUID' not in result:
        # Preserve a custom standalone EFI system ID during legacy migration.
        result['IOPlatformUUID'] = (result['provisioning_UDID'] if 'provisioning_UDID' in result else
            str(uuid.UUID(bytes_le=result['system-id']) if 'system-id' in result else uuid.uuid4()).upper())
    result.setdefault('provisioning_UDID', result['IOPlatformUUID'])
    if 'ROM' not in result:
        if rom_oui is not None:
            result['ROM'] = rom_oui + secrets.token_bytes(3)
        else:
            value = bytearray(secrets.token_bytes(6))
            value[0] = (value[0] | 2) & 0xfe  # locally administered, unicast
            result['ROM'] = bytes(value)
    return validate(result)


def normalize_system_version(metadata):
    """Rewrite SystemVersion.plist to the emulated macOS identity.

    The stock Darling runtime ships ProductVersion 11.7.4 and the literal build
    string "Darling"; left in place, NSProcessInfo reports that while
    kern.osproductversion / kern.osversion report 14.7 / 23H124, so one reader
    sees two macOS versions and a build string no real install ever carries.
    Converge every version field a reader can see on the one emulated release.
    Other keys (iOSSupportVersion, copyright) are preserved.
    """
    updated = dict(metadata)
    updated['ProductName'] = 'macOS'
    updated['ProductVersion'] = EMULATED_PRODUCT_VERSION
    updated['ProductUserVisibleVersion'] = EMULATED_PRODUCT_VERSION
    updated['ProductBuildVersion'] = EMULATED_BUILD_VERSION
    # The stock template credits "Lubos Dolezel"; a real install never does.
    updated['ProductCopyright'] = '1983-2024 Apple Inc.'
    return plistlib.dumps(updated, fmt=plistlib.FMT_XML, sort_keys=True)


def provisioning_files(prefix, properties, runtime_root):
    """Preflight every file before making any identity changes."""
    version = Path(runtime_root) / 'System/Library/CoreServices/SystemVersion.plist'
    with version.open('rb') as stream:
        raw = stream.read(MAX_SIZE + 1)
    if len(raw) > MAX_SIZE:
        raise ValueError('Runtime SystemVersion.plist exceeds 64 KiB')
    metadata = plistlib.loads(raw)
    if not isinstance(metadata, dict) or any(not isinstance(metadata.get(key), str) or not metadata[key]
            for key in ('ProductName', 'ProductVersion', 'ProductBuildVersion')):
        raise ValueError('Runtime SystemVersion.plist is missing version metadata')
    version_plist = normalize_system_version(metadata)
    reverse = {value: key for key, value in ALIASES.items() if key.startswith('SMBIOS_')}
    lines = ['# Synthetic per-prefix identity; generated by configure-darling-platform.py.']
    for key, value in sorted(properties.items()):
        value = value.hex().upper() if isinstance(value, bytes) else value
        lines.append(reverse[key] + '=' + shlex.quote(value))
    files = {
        'private/etc/darling-smbios.conf': ('\n'.join(lines) + '\n').encode(),
        'System/Library/CoreServices/SystemVersion.plist': version_plist,
        'private/etc/darling-platform.plist': plistlib.dumps({'schema': 1, 'properties': properties}, sort_keys=True),
    }
    for relative in files:
        path = checked_path(prefix, relative)
        checked_path(prefix, relative + '.before-platform-change')
        if path.exists():
            if relative.endswith('darling-platform.plist'):
                load(path)
            elif relative.endswith('.conf'):
                load(path)
            else:
                if not isinstance(plistlib.loads(path.read_bytes()), dict):
                    raise ValueError('Existing SystemVersion.plist is invalid')
    return files


def save_provisioning(prefix, properties, runtime_root):
    ensure_stopped(prefix)
    files = provisioning_files(prefix, properties, runtime_root)
    # Save the canonical identity first: an interrupted run reuses it on retry.
    order = ['private/etc/darling-platform.plist', 'private/etc/darling-smbios.conf',
             'System/Library/CoreServices/SystemVersion.plist']
    changed = False
    for relative in order:
        changed = atomic_write(checked_path(prefix, relative), files[relative]) or changed
    return changed


def prefix_lock(prefix):
    ensure_stopped(prefix)
    path = checked_path(prefix, 'private/etc/.darling-platform.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(fd, 'r+')
    fcntl.flock(stream, fcntl.LOCK_EX)
    return stream


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path)
    parser.add_argument('--import', dest='source', type=Path, help='OpenCore plist, GenSMBIOS JSON, legacy .conf, or schema-1 plist/JSON')
    parser.add_argument('--set', action='append', default=[], metavar='KEY=VALUE')
    parser.add_argument('--provision', action='store_true', help='Generate missing synthetic identifiers and provision prefix metadata')
    parser.add_argument('--runtime-root', type=Path, help='Runtime supplying SystemVersion.plist (default: saved SDK or /usr/local/libexec/darling)')
    parser.add_argument('--rom-oui', help='Explicit three-byte unicast OUI for a missing ROM; default is locally administered')
    parser.add_argument('--replace-known-defaults', action='store_true',
                        help='Provision only if the prefix identity still has the shared serial or UUID; otherwise change nothing')
    parser.add_argument('--write', action='store_true', help='Write validated overrides to a stopped prefix (otherwise preview only)')
    args = parser.parse_args(argv)
    if args.replace_known_defaults:
        if args.source or args.set:
            parser.error('--replace-known-defaults cannot be combined with --import or --set')
        args.provision = True
    config = Path(__file__).resolve().parents[1] / 'build/config.json'
    saved = json.loads(config.read_text()) if config.exists() else {}
    prefix = args.prefix or Path(os.environ.get('DPREFIX') or saved.get('prefix', str(Path.home() / '.darling')))
    try:
        prefix = prefix.expanduser()
        rom_oui = None
        if args.rom_oui:
            if not args.provision:
                raise ValueError('--rom-oui requires --provision')
            rom_oui = bytes.fromhex(args.rom_oui.replace(':', '').replace('-', ''))
            if len(rom_oui) != 3 or rom_oui[0] & 1:
                raise ValueError('--rom-oui requires exactly three unicast bytes')
        identity_files = [checked_path(prefix, 'private/etc/darling-smbios.conf'), destination(prefix)]
        if args.replace_known_defaults and not any(path.exists() for path in identity_files):
            print('No saved platform identity; nothing to replace.')
            return 0
        with prefix_lock(prefix) if args.write else nullcontext():
            path = destination(prefix)
            properties = {}
            if args.replace_known_defaults and not any(
                    path.exists() and has_known_defaults(load(path)) for path in identity_files):
                print('Platform identity has no shared defaults; nothing changed.')
                return 0
            if args.provision:
                legacy = identity_files[0]
                if legacy.exists():
                    properties.update(load(legacy))
            if path.exists():
                incoming = load(path)
                if 'IOPlatformUUID' in incoming:
                    properties.pop('system-id', None)
                    properties.pop('provisioning_UDID', None)
                properties.update(incoming)
            if args.source:
                incoming = load(args.source)
                if 'IOPlatformUUID' in incoming:
                    properties.pop('system-id', None)
                    properties.pop('provisioning_UDID', None)
                properties.update(incoming)
            for assignment in args.set:
                key, sep, value = assignment.partition('=')
                if not sep:
                    raise ValueError('--set requires KEY=VALUE')
                if key == 'IOPlatformUUID':
                    properties.pop('system-id', None)
                    properties.pop('provisioning_UDID', None)
                if key == 'model': properties.pop('product-name', None)
                properties[key] = value
            runtime_root = args.runtime_root or Path(saved.get('sdk', '/usr/local/libexec/darling'))
            if args.provision:
                properties = provision(properties, rom_oui)
                provisioning_files(prefix, properties, runtime_root)
            else:
                properties = validate(properties)
            if args.write:
                changed = (save_provisioning(prefix, properties, runtime_root) if args.provision else save(prefix, properties))
                print('Platform identity ' + ('saved' if changed else 'unchanged') + '; restart iokitd to apply.')
            else:
                print(json.dumps({'schema': 1, 'properties': properties}, default=lambda value: value.hex().upper(), indent=2))
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException, ExpatError) as error:
        parser.exit(1, str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
