# Contributing

Thank you for improving `darling-lol`. The project is an unofficial
interoperability effort; contributions must keep it in that scope.

## Before you start

- Read [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) and
  [`docs/INTEROPERABILITY.md`](docs/INTEROPERABILITY.md).
- Do not add Riot client files, game binaries, game assets, logos, screenshots,
  gameplay audio/video, account data, tokens or credentials.
- Do not add DRM/anti-cheat/authentication circumvention, telemetry spoofing,
  credential handling, or gameplay automation. Changes that present the project
  as an evasion tool are out of scope.
- Keep existing copyright, license and attribution notices. New third-party code
  or assets need a license and provenance note in the pull request.

## License of contributions

- Original integration tooling (scripts, tools, tests, docs and project artwork)
  is licensed under **GPL-3.0-only**.
- Patches to upstream components are licensed under the license of the upstream
  file(s) they modify. If a patch touches files under different licenses, each
  hunk keeps the license of its target file.
- By submitting a contribution, you confirm that you have the right to submit it
  and agree to license it under the applicable terms above.

## Developer Certificate of Origin

Sign off every commit:

```sh
git commit -s -m "area: short description"
```

The sign-off certifies the Developer Certificate of Origin 1.1
(<https://developercertificate.org/>): you created the contribution or have the
right to submit it under the project's license, and you understand that the
contribution and its sign-off are public and may be redistributed.

## Checks

Run the source audit and the relevant tests before opening a pull request:

```sh
python3 scripts/validate.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

The audit checks tracked source, rendered Python/shell, pinned revision format,
patch existence and patch parsing. It does not check patch applicability to the
pinned revisions. Do not bypass it; fix the source or document the exception in
the pull request.

## Pull requests

- Keep changes focused and describe the compatibility problem and the fix.
- Mention any change that touches platform identity, sandbox/AMFI probes,
  anti-cheat or account risk. Do not describe it as stealth, evasion, spoofing
  or an anti-cheat bypass.
- If you change a patch, run `python3 scripts/prepare.py --check-only` against
  an existing pinned source checkout to verify that it applies to the pinned
  revision.
- If you change a user-facing variable, keep the old name as a deprecated alias
  for at least one release.

This is not legal advice. See [`docs/INTEROPERABILITY.md`](docs/INTEROPERABILITY.md).
