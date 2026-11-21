---
name: open-pr
description: "Use when a contributor wants to turn local changes into a pull request for darling-lol: syncs main with upstream, creates the branch, writes the signed commit and the PR text, runs the checks and opens the PR against main."
---

# darling-lol: branch, commit and pull request

Takes local changes to an open pull request that follows `CONTRIBUTING.md` and `AGENTS.md`. Read both first if they were not read in this session; their rules win over this file. Commit messages and PR text are in English.

Each harness keeps its own copy and `tests/test_agent_skills.py` requires them to be identical: edit `.opencode/skills/open-pr/SKILL.md` and copy it over `.claude/skills/open-pr/SKILL.md`.

## Remotes

The canonical repository is `lucannez64/darling-lol`.

1. Run `git remote -v` and `gh auth status`.
2. `upstream` must point to the canonical repository. Add it when missing: `git remote add upstream https://github.com/lucannez64/darling-lol`.
3. Find where branches are pushed:
   - `origin` is a fork of the canonical repository: push there and open the PR from `<fork-owner>:<branch>`.
   - `origin` is the canonical repository itself (maintainer with push access): there is no fork. Skip `upstream`, push the branch to `origin` and open the PR from `<branch>`.
   - Neither can be told apart: ask the user once.
4. Take `<fork-owner>` from the `origin` URL. Never push to a repository the user does not own or cannot write to.

## Steps

1. Run `git fetch upstream` (or `origin` when there is no fork) first, so `<remote>/main` exists even when the remote was just added; if the fetch fails, stop and report. Then run `git status --short` and `git log --oneline <remote>/main..HEAD`. Uncommitted changes and commits already ahead of main are the content of the PR. If there are neither, stop and say so.
2. Sync main without losing the work:
   - keep the commits ahead of main found in step 1; they must land on the new branch
   - `git stash push -u -m open-pr` when the tree is dirty, and remember that this run created that stash
   - `git checkout main && git merge --ff-only <remote>/main`
   - `git push origin main` when `origin` is a fork
   - If the fast-forward fails, return to the starting branch, restore this run's stash, and report. Never reset main, rebase it or force push.
3. Create the PR branch: `git checkout -b <type>/<slug>` from the updated main.
   - When step 1 found commits ahead of main, branch from the branch that holds them instead (`git checkout -b <type>/<slug> <branch>`) and merge the updated main into it, so those commits stay in the PR.
   - Restore the stash only when step 2 created it, once the branch exists and any ahead commits are on it, with `git stash pop --index` so a partially staged tree comes back staged. When the tree was clean, skip it so an unrelated stash is neither applied nor dropped, and report a restore conflict instead of resolving it.
   - Types: `fix`, `feat`, `docs`, `tests`, `scripts`, `build`, `refactor`.
   - Slug: lowercase, hyphenated, at most five words (`fix/quiet-sudo-update`, `feat/patchkit`).
4. Run the checks the change needs and report the result:
   - `python3 scripts/configure.py` when `tools/*` changed, and test the rendered copy
   - `python3 scripts/validate.py`
   - the closest test file, then `python3 -m unittest discover -s tests`
   - `python3 scripts/prepare.py --check-only --source <darling-src>` only when a patch or pin changed
   A behavior change needs a new or updated test. If a check fails, fix it or stop. Do not open a PR over a failing check. A check that could not be run is stated in the PR body.
5. Stage files by name. Never stage `build/`, logs, prefixes, `custom-icon.*`, binaries or archives.
6. Commit with `git commit -s` and the format below when step 5 staged changes; when the tree was clean, skip the commit and push the existing commits.
7. `git push -u origin <branch>`.
8. Open the PR: `gh pr create --repo lucannez64/darling-lol --base main --head <head> --title <title> --body-file <file>`. Put the body in a temporary file outside the repository. Reply with the PR URL and one line of summary.

## Commit message

`type(scope): description`

- Type is one of the branch types above.
- Scope is the area touched, for example `toolbox`, `launcher`, `updater`, `compat`, `patchkit`, `installer`, `shortcuts`, `security`, `metal`, `opengl`, `libdispatch`. Check `git log --format=%s -40` for the current ones. Omit the scope only for a cross-cutting change.
- Description: lowercase, imperative, no trailing period, about 72 characters at most, says what the change does.
- Optional body wrapped at 72 columns that explains why.
- Always `-s`: the sign-off certifies the Developer Certificate of Origin.
- One focused commit per concern. Do not write `fix: code review`; name what the review changed.
- Add the attribution trailers the agent harness or the contributor requires, after the sign-off.

## PR title and body

The title is the commit title when the PR has one commit, otherwise the same format summarizing the whole PR.

There is no template. Write plain prose in this order:

1. Problem: the compatibility issue or failure the user sees, and when it happens.
2. Fix: what changes and the main design decision.
3. Flags: new or changed environment variables, defaults and opt-out. Keep an old variable name as a deprecated alias for at least one release.
4. Validation: the checks that passed and the ones not run, such as a real Toolbox run or a guest probe.
5. Credit: when carrying work from another PR or author, link it and say authorship is preserved.

Mention explicitly any change that touches platform identity, sandbox or AMFI probes, anti-cheat or account risk. Use "compatibility issue", "client check" and "platform identity". Never write "stealth", "evasion", "spoofing", "bypass" or "unbannable".

## Hard rules

- Do not launch or stop the client or game, run installers or `scripts/uninstall.sh`, attach debuggers or run guest probes.
- Do not commit Riot or game files, assets, tokens, logs or prefixes.
- Do not edit `build/runtime/tools/*`; edit `tools/*` and run `configure.py`.
- Preserve copyright, license and attribution notices.
- No force push, no history rewrite, no `--no-verify`.
- When something is ambiguous (which remote is the fork, which files belong to the PR), ask one short question instead of guessing.
