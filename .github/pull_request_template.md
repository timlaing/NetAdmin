## Change

Describe the problem and the resulting behaviour.

## Validation

List relevant tests and any deployment evidence against a lab or production network.

## Checks

- [ ] No TSIG secrets, RADIUS shared secrets or password hashes, lease files, zone files or live service configurations included.
- [ ] Tests cover changed behaviour and the local check sequence passes (`ruff`, `ruff format`, `mypy`, `pylint`, `pytest`).
- [ ] Documentation and `.env.example` updated where needed.
- [ ] Security invariants considered: no privileged writes moved into the web app, no secrets stored in SQLite.
