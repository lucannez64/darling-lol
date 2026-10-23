# Interoperability and compliance scope

> This document explains how the project stays within interoperability and
> responsible-use practices. It is **not legal advice**. Software, contract and
> platform rules are fact-specific and differ by country. Consult qualified
> counsel before distributing, modifying or using this project.

## 1. Purpose

`darling-lol` (<https://github.com/lucannez64/darling-lol/>) is an unofficial
interoperability effort. It applies source patches to
[Darling](https://github.com/darlinghq/darling), a free-software macOS
compatibility layer, so that the official macOS Riot client can run on
x86-64 Linux. The user obtains the client from Riot and signs in through Riot's
own client and services.

The project is not a game, a game server, an account service, a launcher for
pirated content, or a substitute for Riot's platform. It does not provide Riot
accounts, authentication, matchmaking, payments, telemetry or game servers.

## 2. What we distribute and what we do not

We keep the project on the interoperability side by distributing only:

- source patches for Darling, a free-software compatibility layer;
- original integration, build and test tooling;
- project artwork;
- documentation and license notices.

We do **not** distribute, bundle, mirror or sublicense:

- the Riot client or game binaries;
- game assets, art, audio, video, fonts or data;
- account databases, tokens or credentials;
- Riot installers or installers for pirated content.

The user obtains the client from Riot and installs it through Riot's own
installer. The project does not provide accounts, authentication, matchmaking,
payments, telemetry or game servers, and it does not sell the game, a license,
access or priority.

## 3. Platform identity

Darling needs macOS-like platform metadata. `tools/configure-darling-platform.py`
can generate synthetic serial numbers, UUIDs and related fields, or import
values supplied by the user, so that the guest environment is internally
consistent.

These values are **local compatibility metadata**:

- they are not Apple-issued identifiers, credentials or authentication tokens;
- they are not used with Apple services and should not be used to impersonate
  real hardware;
- this repository does not pass them to Riot as authentication data. The values
  are exposed to guest applications through IOKit, and the official client's
  later handling of them is outside this repository's scope. The official client
  authenticates through Riot's own flow.

See `docs/PLATFORM_IDENTITY.md` for the schema, validation rules and warnings.

## 4. Local sandbox/AMFI probe

Darling has no macOS MAC, Seatbelt or AMFI framework. Chromium/CEF, which the
Riot client embeds, probes `___mac_syscall`. An `ENOSYS` response is treated as
fatal and prevents CEF helper processes from starting. The compatibility layer
therefore reports the local probe as satisfied. The relevant code and comments
are in `patches/xnu.patch` (`mac_syscall.c`).

This compatibility shim:

- does not implement a sandbox and does not make the guest secure;
- does not interact with Riot's servers or alter Riot's account, payment or
  server-side integrity flows;
- does not grant access to Riot's content or services.

Treat the guest as **unsandboxed**. Do not run untrusted content inside it. This
is a local compatibility limitation, not a claim of security. See
`docs/COMPLIANCE.md` for the project's terminology and scope guidance.

## 5. Copyright and licenses

We keep the project's copyright and licensing position clear:

- the game, client, code and assets remain Riot's; we do not reproduce, store,
  distribute or commercialise them;
- we distribute patches to free-software Darling and original tooling, not a
  copy of the game or the upstream runtime;
- each patch keeps the license and notices of the upstream file it modifies;
  original tooling is GPL-3.0-only;
- we keep the upstream license and attribution notices in the patched files and
  describe the project's licensing position in `NOTICE.md`;
- we do not remove copyright or license notices from patched files;
- we review good-faith licensing and removal requests.

We do not sell the game, a license, access or priority. Donations are voluntary
and support development of the free tooling; they do not purchase the game or
any Riot service.

## 6. Access, authentication and integrity controls

The project is designed for compatibility, not for defeating access,
authentication, payment or integrity controls:

- we do not provide tools or features whose purpose is to defeat those controls;
- we do not forge authentication tokens, session data, account data or
  server-side integrity attestations;
- we do not proxy or emulate Riot accounts, matchmaking, payments, telemetry or
  game servers;
- we do not patch Riot's client or game binaries, CEF or anti-cheat;
- we do not automate gameplay input or provide cheats;
- the opt-in hide helper manages local client windows while a match runs; the
  opt-in close helper terminates eligible League client UI processes. Both are
  off by default and do not change client binaries, CEF or anti-cheat.

The synthetic platform identity in §3 is local metadata; it is not a credential.
This repository does not pass it as Riot authentication data.

## 7. User agreements and account risk

Use of the project is at your own risk. You must:

- own a legitimate Riot account and obtain the client from Riot;
- comply with Riot's terms and user agreement;
- not cheat, exploit, automate gameplay or use the project to gain an unfair
  advantage;
- accept that Riot does not support this environment and may suspend or ban
  accounts, including accounts used only for testing. Do not use a main or
  ranked account.

The project does not authorise any violation of Riot's terms. If you do not
agree, do not use the project. Account restrictions are a real risk and are
documented in the README announcement.

## 8. Trademarks and presentation

League of Legends and Riot Games are trademarks or registered trademarks of Riot
Games, Inc. The names are used only to describe the compatibility target
(nominative use). The project:

- does not use Riot logos, game art or other Riot brand assets;
- uses an original project mark;
- is not affiliated, endorsed or sponsored by Riot Games;
- asks forks to use their own name and icon and not to imply endorsement.

See `NOTICE.md` and `docs/COMPLIANCE.md`.

## 9. Historical material

Earlier revisions of the repository included a third-party game logo and an
embedded gameplay clip. They were removed from the current tree; the current
tree uses an original project mark and contains no gameplay media. Git history
and third-party forks may still contain those earlier revisions. They are not
part of the current project, are not licensed by this project, and should not be
reused. The project will review good-faith removal requests.

## 10. Notices and contact

For a licensing, trademark or interoperability question, contact the maintainer
using the address in `README.md`, or open an issue in the repository. Include:

- the repository URL, file or commit at issue;
- the right or license you assert;
- the action you request;
- a way to contact you.

For a formal copyright notice, use the hosting platform's copyright notice
process. This document is not a designation of an agent for any platform and
does not replace that process.

## 11. No legal advice and no warranty

This document is a technical and scope statement, not legal advice. The project
is provided "as is", without warranty of any kind. Nothing here guarantees that
a particular use is lawful in a particular country, that Riot will not restrict
an account, or that a court or regulator will accept the project's position. If
you need legal certainty, consult a lawyer qualified in your country.
