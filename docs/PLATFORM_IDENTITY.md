# Explicit platform identity overrides

First-time setup (`scripts/install-runtime.sh` creating a new prefix) runs
`--provision --write` once, so each new prefix gets its own synthetic identity.
A prefix without saved identity files keeps the host-derived UUID and
`DARLING-` serial; launching never generates an override. Import mode uses
values supplied by the user. `--provision` generates synthetic identifiers; it
does not issue authentic Apple identities or change CPU, GPU, or security
metadata. The overrides are for application compatibility; they are not intended
to bypass anti-cheat, telemetry, licensing, or platform security controls.

> **Synthetic, local metadata.** These values are not Apple-issued identifiers,
> credentials or authentication tokens. Do not use them with Apple services or
> to impersonate real hardware. This repository does not pass them to Riot as
> authentication data; the values are exposed to guest applications through
> IOKit, and the official client's later handling of them is outside this
> repository's scope. See the
> [interoperability and compliance scope](INTEROPERABILITY.md).

Preview an OpenCore `config.plist`, a flat GenSMBIOS JSON file, or an existing
`darling-smbios.conf` without changing the selected prefix:

```sh
python3 tools/configure-darling-platform.py --prefix /absolute/prefix --import /path/to/config.plist
```

After reviewing the preview, shut down that prefix and explicitly save:

```sh
DPREFIX=/absolute/prefix python3 build/runtime/tools/stop-darling-runtime.py
python3 tools/configure-darling-platform.py --prefix /absolute/prefix --import /path/to/config.plist --write
```

The default prefix selection is `--prefix`, then `DPREFIX`, then saved
`build/config.json`, then `~/.darling`. Writing refuses a live prefix. The
configuration is activated when that prefix's iokitd starts again; it is an
immutable startup snapshot. An absent or invalid configuration preserves the
existing defaults. Invalid daemon-side configuration is reported once.

The file is `<prefix>/private/etc/darling-platform.plist`. It uses this schema:

```json
{
  "schema": 1,
  "properties": {
    "IOPlatformSerialNumber": "USER-SUPPLIED",
    "model": "UserSuppliedModel"
  }
}
```

Supported properties:

| Property | Configuration value | IOKit value |
|---|---|---|
| `IOPlatformSerialNumber` | Printable ASCII string, 1–64 characters | NSString |
| `provisioning_UDID` | UUID string matching `IOPlatformUUID` | NSString |
| `IOPlatformUUID` | Nonzero UUID; normalized to uppercase | NSString |
| `manufacturer`, `model`, `product-name`, `board-id` | Printable ASCII string, 1–128 characters | NUL-terminated NSData |
| `mlb-serial-number` | Printable ASCII string, 1–64 characters | NUL-terminated NSData |
| `ROM` | Exactly 6 bytes; JSON accepts hexadecimal text | NSData |
| `system-id` | Exactly 16 bytes; JSON accepts hexadecimal text | NSData |

Supplying a model also supplies the matching product name unless one is
explicitly provided. Supplying a UUID derives its EFI little-endian `system-id`;
a conflicting explicit system ID is rejected. No other missing fields are
invented. These properties belong to the existing platform service; the tool
does not manufacture a firmware/NVRAM registry hierarchy.

For OpenCore, the importer reads `PlatformInfo/Generic` (or a standalone Generic
dictionary): `SystemProductName`, `SystemSerialNumber`, `SystemUUID`, `MLB`,
and `ROM`. It ignores unrelated boot and security settings. GenSMBIOS aliases
include `Type`, `Serial`, `Board Serial`, and `SmUUID`. Legacy `SMBIOS_*` identity
keys are accepted from `.conf`; parsing never executes shell expressions.

Existing configured fields survive a partial import. A new explicit UUID
replaces its previously derived system ID. `--set KEY=VALUE` can update an
individual field. Unknown schema versions, unsupported properties, malformed
values, and duplicate JSON fields are rejected.

Writes are atomic with mode 0600. Repeating the same write changes nothing. The
first previous configuration is kept as
`darling-platform.plist.before-platform-change`, also with mode 0600. Importing
an older `.conf` never edits that source. Symlinks escaping the prefix are
rejected. To restore the original defaults, remove the override file while the
prefix is stopped, then restart it.

## Opt-in provisioning

Preview a fresh or repaired identity without writing anything:

```sh
python3 tools/configure-darling-platform.py --prefix /absolute/prefix --provision
```

Stop the prefix, then save the identity and prefix metadata:

```sh
DPREFIX=/absolute/prefix python3 build/runtime/tools/stop-darling-runtime.py
python3 tools/configure-darling-platform.py --prefix /absolute/prefix --provision --write
```

Launch Darling normally afterwards. This requires the updated iokitd from this
PR; the config tool alone cannot update an older running daemon. Besides
first-time setup above, provisioning runs automatically in one other case:
after installing, `scripts/update.sh` runs
`configure-darling-platform.py --replace-known-defaults --write`, which
provisions the prefix only if its saved identity still contains the shared
serial or UUID below. Prefixes without saved identity files, or whose saved
values are all custom, are left byte-for-byte unchanged.

Missing serials use 12 characters and missing MLBs use 17 characters, sampled
with Python's `secrets` module from `0123456789ABCDEFGHJKLMNPQRSTUVWXYZ` (Base-34).
These are synthetic strings, with no Apple model, factory, date or checksum
validation, allocation, or guarantee of acceptance by Apple services. Missing
UUIDs use RFC4122 v4; `system-id` contains the UUID's 16 little-endian bytes.
For this project `provisioning_UDID` is the uppercase platform UUID alias, not
a separate Apple provisioning credential or a claim about hardware UDIDs.

Missing ROMs default to six random bytes with the local/unicast bits set. You
can explicitly supply a three-byte OUI using `--rom-oui AA:BB:CC` (replace the
example with your chosen unicast OUI). It only affects a missing ROM. An OUI
does not make a generated identity Apple-issued.

Provisioning replaces the known shared serial `C02ZL0GVMD6N` and UUID
`6F3B2A1E-8C4D-4E5F-9A7B-1D2C3E4F5A6B`, along with the latter's derived fields
(including a standalone `system-id` or `provisioning_UDID` derived from it).
It preserves other existing custom values, including imported serials and UUIDs
that do not follow the generated formats. A standalone custom `system-id` is
preserved by deriving its UUID instead of generating a different one. Model,
board and manufacturer values are only set when supplied by the user.

The command reads an existing prefix `darling-smbios.conf` for migration, then
applies the canonical plist, an explicit import, and `--set` values in that
order. It writes:

- `private/etc/darling-platform.plist`: canonical runtime overrides.
- `private/etc/darling-smbios.conf`: matching shell-quoted `SMBIOS_*` values,
  including `SMBIOS_PROVISIONING_UDID`; unrelated legacy settings are not exported.
- `System/Library/CoreServices/SystemVersion.plist`: an exact copy of the selected
  runtime's metadata, not a fabricated macOS release. The runtime root defaults
  to the saved SDK path or `/usr/local/libexec/darling`; override with
  `--runtime-root /path/to/runtime`.

A valid runtime version file is required. The command checks all destinations
before changing identity files, rejects live prefixes and escaping symlinks,
backs up each previous file once with `.before-platform-change`, and replaces
each file atomically. The set of three files is not a single transaction: the
canonical identity is saved first so rerunning after interruption completes
provisioning with the same identity. Concurrent CLI writers serialize through
a per-prefix lock. Repeated writes preserve identifiers and unchanged files.
Previewing a fresh identity generates a sample; only `--write` makes it stable.
To restore host-derived defaults after provisioning, remove both identity files
while stopped, so a later provisioning run cannot reimport the legacy copy.

## system_profiler

The prefix's `/usr/sbin/system_profiler` (installed by
`tools/install-darling-system-profiler.py`) answers `SPHardwareDataType` and
`SPSoftwareDataType` from the running guest: model, serial number, platform UUID
and provisioning UDID from IOKit's `IOPlatformExpertDevice` (via `ioreg`), CPU,
cache and memory from `sysctl`, and the OS version from `sw_vers` and `uname`. It
keeps no identity of its own, so it reports the same values as IOKit, including any
override saved by this tool. A field the guest does not provide is left out.
`python3 -m unittest tests.test_system_profiler` checks the output with stub
commands.
