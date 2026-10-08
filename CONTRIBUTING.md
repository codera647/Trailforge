# Contributing

Trailforge is maintained by [codera647](https://github.com/codera647). Open an issue or pull request at [codera647/Trailforge](https://github.com/codera647/Trailforge). Use the [private security channel](SECURITY.md) for vulnerabilities.

Keep the base package usable without host applications or optional provider SDKs. Add adapters behind versioned contracts, declare effects and guarantees, and include meaningful interruption/authority tests. Applied migrations are immutable: add a new versioned migration. Do not weaken policy, assertions or version claims to hide a failing gate.

## Validate a change

Use Python 3.11 or 3.13 and a fresh environment:

```text
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -r requirements-dev.lock
python scripts/verify_workflows.py
python scripts/verify_local.py
python scripts/verify_optional.py
```

The base verifier builds and installs a wheel outside the checkout and rejects empty, skipped and expected-failing suites. Optional PostgreSQL needs an explicit disposable database; Linux isolation needs an explicitly preloaded digest-pinned image. See the [README](README.md) and [conformance policy](docs/conformance.md). CI runs the supported OS/Python matrix plus the optional gates. Include the triggering problem, resulting behavior, verification and limitations in your PR.

Original contributions are MIT licensed. Include required dependency notices and permission-cleared examples. Never contribute credentials, customer data, private reference documents or source books. Fixture success does not establish model quality; subprocesses alone do not isolate untrusted code.

Alpha APIs may change. Preserve existing versioned protocol semantics and applied migrations. New guarantees require corresponding conformance evidence. The maintainer controls releases and repository protection; self-authored checks are not independent human review.
