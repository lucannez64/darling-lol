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

## Recommended configuration

Provision your own prefix instead of keeping Darling's base identity. Do not
copy another user's identifiers. First-time runtime installation does this for
new prefixes, and `scripts/update.sh` provisions an existing prefix that has
none (see below); you can also run it explicitly. With the game and clients closed, stop the selected prefix,
then follow the commands below. `--provision --write` fills missing identifiers
and replaces the known shared defaults, while preserving an already custom
identity. It does not rotate custom identifiers on every run.

Keep `profile=compat` (the default) in
`<prefix>/private/etc/darling-system.prop` for the recommended application
compatibility configuration. See [setup](SETUP.md#recommended-identity-and-compatibility-profile)
for file precedence and activation. This profile is independent of the saved
serial and UUID; provisioning alone does not enable it. Full application support
has not been established across machines.

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
provisions an existing prefix only if it has no saved identity files (iokitd
then reports a host-derived UUID and a `DARLING-` serial that no Mac has) or its
saved identity still contains the shared serial or UUID below. Prefixes whose
saved values are all custom keep their identity files byte-for-byte unchanged,
and a missing prefix is not created. `darling-lol --doctor` warns about a prefix
with no saved identity or with the shared values.
On existing prefixes, `scripts/install-runtime.sh` also runs
`configure-darling-platform.py --normalize-system-version --write` so
`System/Library/CoreServices/SystemVersion.plist` stays aligned to the emulated
macOS version and build without creating or altering identity overrides.

Missing serials use 12 characters and missing MLBs use 17 characters, sampled
with Python's `secrets` module from `0123456789ABCDEFGHJKLMNPQRSTUVWXYZ` (Base-34).
These are synthetic strings, with no Apple model, factory, date or checksum
validation, allocation, or guarantee of acceptance by Apple services. Missing
UUIDs use RFC4122 v4; `system-id` contains the UUID's 16 little-endian bytes.
For this project `provisioning_UDID` is the uppercase platform UUID alias, not
a separate Apple provisioning credential or a claim about hardware UDIDs.

Missing ROMs default to a random Apple-registered OUI followed by three random
bytes. iokitd publishes the ROM as en0's `IOMACAddress`, and a Mac's built-in
interface never has a locally administered address, so the earlier
locally-administered default was a compatibility discrepancy (issue #31). You
can explicitly supply a three-byte OUI using `--rom-oui AA:BB:CC` (replace the
example with your chosen unicast OUI). It only affects a missing ROM; run
`scripts/clean-prefix.sh` to regenerate an existing one. An OUI does not make a
generated identity Apple-issued.

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
- `System/Library/CoreServices/SystemVersion.plist`: metadata normalized to the
  emulated macOS version and build reported by the patched kernel (15.5 /
  24F74, Darwin 24.5.0), the release of the macOS reference machine used for
  parity checks. This does not install macOS. The source runtime root defaults to the
  saved SDK path or `/usr/local/libexec/darling`; override with
  `--runtime-root /path/to/runtime`.

Framework bundle metadata describes the same macOS release. Darling's runtime
ships an `Info.plist` in only a few of its system frameworks, so readers such
as the client's WebView check (WebKit's `CFBundleVersion`) found no version.
`tools/provision-framework-info.py` writes the factual bundle fields measured
on macOS 15.5 (24F74) for every framework that both that release and the
installed runtime have (`tools/data/macos-15.5-framework-info.json`); WebKit
reports `20621.2.5.11.8`. It writes XML plists only into the prefix overlay,
never the runtime root, follows each bundle's versioned or shallow layout, and
adds a missing `Resources` alias for versioned bundles. `install-runtime.sh`
and identity-regenerating `clean-prefix.sh` runs call it; writes need a stopped
prefix, are idempotent, and `--check` reports missing or differing metadata.

For a MacBookPro16,1 identity under the compat GPU presentation,
`system_profiler SPDisplaysDataType` describes that model's Intel UHD Graphics
630 and AMD Radeon Pro 5500M, with their VRAM, revision IDs, automatic graphics
switching, gMux and `Metal Support: Metal 3` fields. It describes the model; it
does not add Metal devices or change host drivers. Displays appear under the
discrete GPU and use the active XRandR geometry Darling's NSScreen backend
exposes (scale 1.0) rather than an invented Retina panel; unavailable
geometry is omitted. Each display is named, as on macOS, after the monitor name
in its EDID (for example `PL3466WQ`); a display without one keeps the
`Display N` name. The host presentation keeps the physical GPU
list. Text and XML output carry the same fields.

A valid runtime version file is required. The command checks all destinations
before changing identity files, rejects live prefixes and escaping symlinks,
backs up each previous file once with `.before-platform-change`, and replaces
each file atomically. The set of three files is not a single transaction: the
canonical identity is saved first so rerunning after interruption completes
provisioning with the same identity. Concurrent CLI writers serialize through
a per-prefix lock. Repeated writes preserve identifiers and unchanged files.
Previewing a fresh identity generates a sample; only `--write` makes it stable.
To explicitly regenerate the synthetic per-prefix identifiers (serial, MLB,
UUID, `system-id`, `provisioning_UDID` and ROM) on a stopped prefix while
keeping any configured `model`, `product-name`, `board-id` or `manufacturer`,
pass `--regenerate`:

```sh
python3 tools/configure-darling-platform.py --prefix /absolute/prefix --regenerate --write
```

`scripts/clean-prefix.sh` wraps that step together with archiving and clearing
the prefix's local Riot/League logs, `machine.cfg`, client profiles, crash
reports, local config/caches and `.before-platform-change` identity copies
(passing `--no-backup` so no copy of the replaced identity stays in
`private/etc/`; the host-side cleanup archive is mode `0600` for the same user)
without removing the Darling runtime or the installed game.
To restore host-derived defaults after provisioning, remove both identity files
while stopped, so a later provisioning run cannot reimport the legacy copy.

## Hardware presentation profile

`tools/system.prop` is the single source for the CPU, GPU, VRAM and memory
values that the launcher exports to the patched guest. `scripts/install-runtime.sh`
copies it to `$DPREFIX/private/etc/darling-system.prop` once; later updates keep an
existing file so a user's choice is not reset. The launcher reads the installed
copy first and falls back to the checkout copy.

`profile=compat` is the default (issue #31; an installed `host` profile from before
is moved to `compat` once by `tools/migrate-system-profile.py`). It reports the
MacBookPro16,1 compatibility values
(`i7-9750H`, `Radeon Pro 5500M` on AMD, `UHD Graphics 630` on Intel, 4096 MB VRAM
and 16 GiB). An NVIDIA card is presented as the `Radeon Pro 5500M` too: no Mac
that runs macOS 14 has an NVIDIA GPU, and the game's rendering goes through the
same Metal-to-Vulkan layer on every vendor. `compat.gpu.name=host` keeps the real
card name and IDs while the rest of the profile stays compat. The cache sysctls
(`hw.cacheconfig`, `hw.cachesize`) and the game's cache and topology `cpuid`
leaves report the i7-9750H's 6 cores / 12 threads and its 32 KiB / 256 KiB /
12 MiB caches, whatever the host CPU. Leaf 1 and `machdep.cpu.features` report
the i7-9750H's feature words: its EDX, and its ECX with each instruction-set bit
kept only when the host has it, plus the ring-0-only bits (VMX, EST, PCID,
x2APIC...) every Coffee Lake reports. `profile=host` reports the real host CPU,
GPU, VRAM and memory. `RCLOL_PLATFORM_PROFILE=host|compat` overrides
the file for one launch; `RCLOL_SYSTEM_PROP=/path/to/system.prop` selects a different
file for diagnostics.

The profile does not contain serial numbers, UUIDs, MLB/ROM values, tokens or
account data. Serial numbers, UUIDs and MLB/ROM values are managed separately by
`tools/configure-darling-platform.py`; the tool does not manage tokens or account
data. See the sections above for platform-identity metadata. The profile does
not replace the host hardware, driver or GPU that presents frames. It does not
establish licensing, authentication, or acceptance by a third-party service.

The current compat profile also enables a Security fallback: when the
notarization lookup returns false, it treats the code as notarized without
validating an offline ticket. This is not equivalent to macOS notarization.

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

`SPDisplaysDataType` reads the host DRM cards. In the `host` profile it
reports the host card name, device and VRAM. In the default `compat` profile an AMD
card is reported as `AMD Radeon Pro 5500M`, vendor `0x1002`, device `0x7340`, bus
PCIe and VRAM from `compat.gpu.vram` (4096 by default); an Intel card is reported
as `Intel UHD Graphics 630` on bus Built-In with shared memory, and an NVIDIA
card like an AMD one. An AMD card beside the discrete GPU (an NVIDIA card, or an
AMD card with a larger `mem_info_vram_total`, such as a Ryzen APU next to a
Radeon dGPU) is reported as the `Intel UHD Graphics 630`, in IOKit and Metal too,
so the host shows the MacBookPro16,1's pair instead of two 5500Ms. With `compat.gpu.name=host` or `profile=host`, NVIDIA
cards keep host-derived names; the profiler uses the PCI ID database and can
fall back to `NVIDIA Graphics`, while Metal uses the Vulkan device name, so the
two names may differ. IOKit also publishes a synthetic `IOPCIDevice` per host
card with the same `vendor-id`/`device-id` values and an `IOGraphicsAccelerator2`
child, so Metal and Dawn resolve the GPU without the host driver having to
publish anything in the guest registry. Each accelerator's registry ID is
derived from the card's real PCI IDs, and `-[MTLDevice registryID]` derives the
same value from its Vulkan device, so on a hybrid or multi-GPU host the lookup
finds the card Metal actually renders on, whichever one the Vulkan loader,
`/tmp/metal-gpu` or `METAL_INDIUM_GPU` picked. A software Metal device names no
card, so Dawn falls back to the Metal device name.
`RCLOL_GPU_*`
variables are the launcher-to-guest handoff; `RCLOL_SYSTEM_ROOT` is a test-only
host-root override.

## Loaded kernel extensions (kext_request)

`kext_request` is another platform identity source: it is the Mach call behind
`KextManagerCopyLoadedKextInfo` and `kextstat`. darlingserver answers its
read-only predicates (`Get Loaded Kext Info`, `... By UUID`, `Get Kexts in
Collection`, `Get Kext UUID by Address`, `Get All Load Requests`) from a fixed
list of the Apple kexts a MacBookPro16,1 loads on macOS 15.5 (24F74): the
kernel, its KPI interfaces and drivers such as the Intel CPU power management,
`AMDRadeonX6000` and the Broadcom Wi-Fi driver. Predicates that load, start,
stop or unload a kext, `Daemon Ready` included, are refused like on macOS
without root (`kOSKextReturnNotPrivileged`) and unsupported with it. Nothing is
loaded.

The list describes the compat Mac, not the host, so it is served only under
`profile=compat`. darlingserver reads `RCLOL_PLATFORM_PROFILE` from the
environment it starts with: the launcher exports the resolved profile before it
starts the runtime, and a running darlingserver keeps that value until the
runtime restarts. Under `profile=host`, or in a runtime started without the
launcher's profile, `kext_request` returns `KERN_NOT_SUPPORTED` as it did
before the list existed.

The values are fixed for the OS build, not salted per prefix like the serial
number and UUID above. A kext's UUID and the unslid load address `kext_request`
reports come from that macOS build's kernel collection, so every Mac running
24F74 reports the same ones; a per-prefix value would describe no real build.
Kexts that also ship on Apple silicon carry the versions read from a macOS 15.5
system; AppleHDA and IONDRVSupport follow IOAudioFamily and IOGraphicsFamily,
and the remaining Intel-only drivers keep their last measured versions.
`python3 -m unittest tests.test_kext_request` checks the list and the request
handling without starting Darling.

## Process view: descriptors, limits and language

A guest process sees its own descriptors and limits as a macOS 15.5 application
does (measured on the reference Mac, 2026-11-26):

- `/dev/fd` and `proc_pidinfo(PROC_PIDLISTFDS)` list only the program's own
  files, sockets, pipes and kqueues. Darling's per-thread darlingserver sockets
  and lifetime pipe, the GPU driver's `/dev/nvidia*` and `/dev/dri/*` handles
  and the eventfd/timerfd/perf_event helpers behind Darling's kqueue and timers
  have no macOS counterpart and are not listed. The rule lives in
  `fd_view.c` of the xnu patch.
- `getrlimit(RLIMIT_NOFILE)` starts at 256 soft / unlimited hard, as launchd
  sets for applications; `setrlimit` stores the guest's pair (soft ≤ hard,
  raising the hard limit needs root) without lowering the real Linux limit
  Darling's own descriptors depend on. The soft value is inherited across
  `fork` and `exec` like a real limit. `getdtablesize()` is
  `MIN(soft limit, kern.maxfilesperproc)`.
- `AppleLanguages` and `AppleLocale` live in the global domain
  (`.GlobalPreferences.plist`), filled from the configured locale by
  `tools/provision-user-locale.py`; Darling's Foundation no longer copies them
  into every application's own preferences.
- The launcher passes the environment launchd gives an application (see
  `RCLOL_FILTER_HOST_ENV` in `docs/SETUP.md`).
