# Dependency security

How the project keeps known-vulnerable dependencies out, what the last
scan found, and what is knowingly left. Scan date: 2026-10-10.

## Policy

* `pyproject.toml` floors exclude every release with a known HIGH or
  CRITICAL advisory. For `cryptography` and `python-multipart` the floor
  is the first release with no known advisory at all.
* `tests/test_dependency_declarations.py` fails when a floor drops below
  that line, when a module imports a package pyproject does not declare,
  or when pyproject declares a package nothing imports.
* Dependabot (`.github/dependabot.yml`) opens weekly PRs for pip, npm and
  GitHub Actions. Dependabot security updates (which open PRs outside
  the weekly schedule) are a repository setting: Settings, Code
  security. Turn them on.
* CI (`.github/workflows/ci.yml`) runs the test suite, svelte-check, the
  web build, shellcheck, `tofu fmt` / `tofu validate` and a Docker build
  on every push and PR to main.

## Re-running the scans

```
# Python: fresh venv from pyproject, then audit what pip actually resolved
python3.12 -m venv /tmp/vf-audit && /tmp/vf-audit/bin/pip install -e ".[dev]"
pip-audit --path /tmp/vf-audit/lib/python3.12/site-packages --skip-editable

# Python: audit the declared minimums without resolving anything
#   (one "name==floor" per line)
pip-audit -r floors.txt --no-deps --disable-pip

# Web
cd web && npm ci && npm audit --omit=dev && npm audit
```

## Results (2026-10-10)

Findings from the audits: A1 F21 (cryptography), F22 (python-multipart),
F23 (authlib declared, never imported).

Declared minimums, before and after. This is what `pip install` is
allowed to pick, so it is the worst case a user can land on.

| Package | Floor before | Advisories at old floor | Floor after | Advisories at new floor |
|---|---|---|---|---|
| cryptography | 41 | 5 HIGH, 3 MODERATE, 3 LOW | 50.0.0 | none |
| python-multipart | not declared (Dockerfile floor 0.0.9) | 4 HIGH, 1 MODERATE, 3 LOW | 0.0.31 | none |
| authlib | 1.3 | 1 CRITICAL, 5 HIGH, 5 MODERATE | dropped | n/a |
| joserfc | not declared | 2 HIGH at 1.0 | 1.6.8 | none |
| Pillow (optional extra) | 10 | 1 CRITICAL, 12 HIGH, 4 MODERATE | 12.3.0 | none |
| sentry-sdk | 2.0 | 1 LOW | 2.8.0 | none |

Across every declared package the old floors admitted 2 CRITICAL and 26
HIGH advisories (deduplicated by GHSA id). The new floors admit none.

Installed environments:

| Environment | Packages audited | Known vulnerabilities |
|---|---|---|
| Existing shared `.venv` (before) | 60 | 0 |
| Fresh venv from the new `pyproject.toml` (after) | 46 | 0 |

The shared `.venv` was already on the latest `cryptography` (50.0.2) and
`python-multipart` (0.0.32), so nothing had to be upgraded there. The
audits' F21 and F22 were true for the declared floors, not for that venv.

`authlib` is unimported (`api/oidc.py` uses `joserfc` and httpx), so it
was dropped from `pyproject.toml`. The shared `.venv` still has it
installed. That is harmless; a fresh install no longer pulls it.

### Imported but undeclared before this change

These were imported but missing from `pyproject.toml`, so a clean
`pip install .` could not boot the API: `fastapi`, `uvicorn`,
`python-multipart`, `argon2-cffi`, `bcrypt`, `joserfc`,
`sqlcipher3-binary`. All are declared now.

## Remaining findings

No HIGH or CRITICAL advisory is left in Python or npm. These LOW
findings remain:

### npm: `cookie` < 0.7.0 via `@sveltejs/kit` 2.x (GHSA-pxg6-pf52-xh8x, LOW)

* `npm audit --omit=dev` reports 0, because every package in `web/` is a
  devDependency (SvelteKit bundles its runtime). The full `npm audit`
  reports 3 LOW: `cookie`, `@sveltejs/kit`, `@sveltejs/adapter-auto`.
* The only fix is `@sveltejs/kit` 3.x, a major upgrade of the web
  framework. Not done in a dependency-hygiene change.
* Not reachable here: the advisory is about out-of-bounds characters in
  a cookie name, path or domain. `web/src/lib/server/auth-cookies.ts`
  forces the name to the literal `vf_session`, takes the path from this
  project's own API `Set-Cookie`, and never sets a domain. The other
  cookie names are literals.
* Revisit when Dependabot proposes `@sveltejs/kit` 3.x and the UI has
  been checked against it.

## When a new advisory lands

1. Reproduce with the commands above.
2. Raise the floor in `pyproject.toml` (or the version in
   `web/package.json`) to the first fixed release.
3. Add the package and floor to `SECURITY_FLOORS` in
   `tests/test_dependency_declarations.py`.
4. If no fixed release exists or the fix is a breaking upgrade, record
   the advisory in "Remaining findings" with the reason it is or is not
   reachable.
