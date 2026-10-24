# Contributing to NetAdmin

Thanks for helping improve NetAdmin. This page is the short version; the
[Development](AGENTS.md) page has the full architecture and trap list.

## Ground rules

- **Conventional commit messages** (`feat:`, `fix:`, `docs:`, `test:`, …) are
  required — release-drafter and the autolabeler parse them.
- **Signed commits** (GPG or SSH) are preferred; every commit on `main` is
  signed today.
- Never commit live service configuration: `dns/`, `dhcp/`, `freeradius/`,
  `watchtower/`, `pki/` and `.env` are gitignored because they contain real
  credentials. Tests build their own `Settings(...)` and never need them.

## Development setup

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pre-commit install    # runs ruff + mypy automatically on commit
```

## Checks

Run these before considering work done (same order CI runs them):

```sh
ruff check .
ruff format --check .
mypy
pylint netadmin
pytest
```

A single test:

```sh
pytest tests/test_dhcp_agent.py::test_name --no-cov -q
```

`addopts` enforces a coverage gate of ≥80%, so partial runs need `--no-cov`.

## Documentation site

- The site is published from `main` by `.github/workflows/pages.yml`
  (MkDocs + Material).
- Canonical documents live at the repository root (`README.md`,
  `MIGRATION.md`, `SECURITY.md`, `AGENTS.md`, this file). Pages written for the
  site itself live in `docs/`.
- `scripts/docs.sh` stages everything into `site-src/` (gitignored) before
  building — never commit that directory. In-repo links should use the root
  filenames (including links from `docs/*.md`); the script rewrites them for
  the site slugs.
- Preview locally: `pip install -r docs/requirements.txt && scripts/docs.sh serve`.

## Pull requests

- Branch from `main`, open a PR, and keep **all checks green** (CI, lint,
  CodeQL, SonarCloud quality gate).
- Preserve the architecture boundary: the web app never writes service config
  directly — privileged writes stay in the agents, which validate before
  installing and roll back on reload failure.
- Add or extend tests for behaviour changes; ruff's security (`S`) rules are
  intentional, don't blanket-`noqa`.
- Reference the issue you're fixing and describe how you verified the change.

## Security issues

Please **do not** open public issues for vulnerabilities — follow
[SECURITY.md](SECURITY.md) instead.
