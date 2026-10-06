# Contributing

Thank you for helping make privacy-by-design a working default for social protection systems.

## Ground rules

- **The catalogue and the conformance vectors are the specification.** A behaviour change to the
  authorisation model needs a vector; a vector needs both engines to pass.
- **No personal data anywhere.** Fixtures, tests and docs use obviously fictional identifiers
  (`NID-1001`, "Amina Example"). Audit events carry tokens and attribute names only.
- **Fail closed.** New sensitive endpoints go through `pep.enforce`; new disclosures emit a
  must-record audit event.
- **Every service owns its data.** No cross-service imports of models or sessions.

## Workflow

```bash
pip install -e ".[dev]"
make check          # ruff, pytest, opa test, bundle + contracts freshness, catalogue validation
```

After changing `catalog/*.yaml` or the vectors: `python scripts/build_bundle.py`.
After changing any endpoint or model: `python scripts/export_contracts.py`.

Commit the regenerated `policy/bundle/data.json` and `contracts/openapi/*.yaml`; CI fails when
they are stale.

## Pull requests

- Describe which guide section the change serves (see `docs/guide-mapping.md`).
- Add or update tests; keep the end-to-end suite green on SQLite.
- Keep `docs/` accurate: a new purpose, store or obligation is a documentation change too.
- Record significant design decisions as an ADR in `docs/adr/`.

## Code style

Ruff for linting and formatting (`make lint` / `make format`), 110-column lines, type hints
everywhere, docstrings that explain the privacy intent of a module, not just its mechanics.
