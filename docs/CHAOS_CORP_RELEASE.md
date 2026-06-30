# Publishing `libwatchduty` under `github.com/chaos-corp`

This document is the maintainer runbook for cutting the first public release of
`libwatchduty` under the **chaos-corp** GitHub organization and pushing
versioned artifacts to PyPI.

It assumes you already have:

- `gh` authenticated against an account with `admin` rights on
  `github.com/chaos-corp`
- An SSH key registered with GitHub
- A clean `main` branch locally at `/Users/max/source/libwatchduty`
- Maintainer access to the `libwatchduty` project on PyPI (or the ability
  to register the name on first publish)

---

## 1. One-time repository creation

Run these once, from the project root, the first time the repo is published
under `chaos-corp`. Do **not** re-run on subsequent releases.

```bash
gh repo create chaos-corp/libwatchduty --public \
  --source=/Users/max/source/libwatchduty \
  --description "Unofficial WatchDuty client + threat-ranked terminal dashboard"

git remote add chaos git@github.com:chaos-corp/libwatchduty.git
git push -u chaos main
```

After the initial push:

- Confirm the default branch on GitHub is `main`.
- Push all existing tags: `git push chaos --tags`.
- If a personal-account remote (`origin`) still exists, leave it alone or
  rename it — `chaos` is now the canonical upstream.

---

## 2. PyPI Trusted Publisher setup

We publish from GitHub Actions using PyPI's
[Trusted Publishers](https://docs.pypi.org/trusted-publishers/) flow (OIDC,
no long-lived API tokens).

### 2a. On PyPI (`https://pypi.org/manage/account/publishing/`)

1. Sign in as a maintainer of the `libwatchduty` project. If the project
   does not yet exist, use the **"Add a new pending publisher"** form
   instead — PyPI will reserve the name on first successful publish.
2. Fill in:
   - **PyPI project name:** `libwatchduty`
   - **Owner:** `chaos-corp`
   - **Repository name:** `libwatchduty`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
3. Save. Repeat the same form on `https://test.pypi.org/` with environment
   name `testpypi` if you want a staging target.

### 2b. On GitHub (repo settings)

1. **Settings -> Environments -> New environment -> `pypi`.**
2. Add a **deployment branch rule** restricting deploys to tags matching
   `v*` (or to `main` if you prefer manual dispatch).
3. Optional: require reviewers on the `pypi` environment so a human has to
   click "Approve" before each publish run.
4. Repeat for a `testpypi` environment if used.

The `publish.yml` workflow already requests `id-token: write` and targets
the `pypi` environment, so no secrets need to be configured.

---

## 3. Branch protection on `main`

Configure under **Settings -> Branches -> Branch protection rules ->
Add rule** with pattern `main`:

- [x] **Require a pull request before merging**
  - [x] Require approvals: **1**
  - [x] Dismiss stale approvals when new commits are pushed
  - [x] Require review from Code Owners (if a `CODEOWNERS` file is added)
- [x] **Require status checks to pass before merging**
  - [x] Require branches to be up to date before merging
  - Required checks (add once they have run at least once so GitHub
    recognizes them):
    - `ci / test` (from `.github/workflows/ci.yml`)
    - `ci / lint`
    - Any other jobs in `ci.yml` you want to gate on
- [x] **Require linear history**
- [x] **Require signed commits** (recommended)
- [x] **Do not allow bypassing the above settings** — applies to admins too
- [ ] Allow force pushes: **off**
- [ ] Allow deletions: **off**

Tags matching `v*` should also be protected under **Settings -> Tags** so
only maintainers can create release tags.

---

## 4. Release flow

Every release follows the same loop. Cut releases from `main` only, after
CI is green on the tip.

1. **Decide the version.** Follow SemVer. Source of truth is
   `pyproject.toml`'s `[project].version`.
2. **Bump the version** in `pyproject.toml`. If a `__version__` constant
   exists in `src/`, update it too.
3. **Update `CHANGELOG.md`:**
   - Move items out of `## [Unreleased]` into a new
     `## [X.Y.Z] - YYYY-MM-DD` section.
   - Re-add an empty `## [Unreleased]` block at the top.
   - Update the comparison links at the bottom of the file.
4. **Open a release PR.** Title: `release: vX.Y.Z`. Wait for CI green +
   review approval, then squash-merge into `main`.
5. **Tag and push** from the freshly-merged `main`:

   ```bash
   git checkout main
   git pull chaos main
   git tag -s vX.Y.Z -m "libwatchduty vX.Y.Z"
   git push chaos vX.Y.Z
   ```

6. **Watch the workflows.** All three must finish green for the release to
   be considered shipped:

   - `ci.yml` — tests + lint on the tagged commit
   - `publish.yml` — builds the sdist + wheel and uploads to PyPI via the
     Trusted Publisher (`pypi` environment)
   - `release.yml` — creates the GitHub Release, attaches artifacts, and
     posts the changelog section as the release body

   Monitor with:

   ```bash
   gh run watch --repo chaos-corp/libwatchduty
   ```

7. **Post-release sanity checks:**
   - `pip install libwatchduty==X.Y.Z` from a clean venv works.
   - The GitHub Release page shows the right notes + artifacts.
   - `pypi.org/project/libwatchduty/X.Y.Z/` is live.

If any workflow fails: do **not** delete or move the tag. Fix forward —
land a patch PR, bump to `X.Y.Z+1`, and tag again. Yanked tags confuse
downstream consumers and break PyPI's append-only contract.

---

## 5. Hotfix releases

For urgent fixes when `main` has unreleased work you don't want to ship:

1. Branch from the last release tag: `git checkout -b hotfix/vX.Y.Z+1 vX.Y.Z`.
2. Cherry-pick or write the fix; bump to `X.Y.Z+1`; update `CHANGELOG.md`.
3. PR into `main` (so the fix isn't lost) **and** tag the hotfix branch's
   HEAD as `vX.Y.Z+1`. The publish/release workflows run off the tag, not
   the branch, so the order doesn't matter as long as both happen.
