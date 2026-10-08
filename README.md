# Trailforge

A provider-independent agent execution harness for bounded work, evidence and durable recovery. First public MIT alpha: **0.1.0a1**. The shared kernel records bounded runs, exact evidence and recovery. Host applications supply identities, models, tools and product policy.

The alpha package provides a standalone offline Review CLI, an application-neutral execution API, a trusted-host Develop profile, exact approvals with durable simulated publication recovery, and explicit adapter ports. Installed-wheel conformance covers Linux and Windows, optional adapters and native Linux container boundaries; exact candidate results are recorded in CI. Linux containers are the selected isolation target; Windows supports local development and CLI. Original code and docs are MIT licensed.

## Install

Clone the [standalone repository](https://github.com/codera647/Trailforge) and select a verified release tag. A GitHub source install is also available:

```text
python -m pip install "git+https://github.com/codera647/Trailforge.git@v0.1.0a1"
```

The tag is published only after its candidate passes CI. There is no PyPI publication yet. For a local source checkout:

Use Python 3.11 or newer. From the directory containing this `pyproject.toml`:

```text
python -m pip install .
trailforge --version
trailforge init trailforge-demo
trailforge evaluate --suite trailforge-demo
trailforge run --fixture trailforge-demo/buggy-python --store runs.sqlite3 --pause-after snapshot
```

Copy the returned run ID, then resume and inspect it:

```text
trailforge resume --fixture trailforge-demo/buggy-python --store runs.sqlite3 --run-id YOUR_RUN_ID
trailforge inspect --store runs.sqlite3 --run-id YOUR_RUN_ID
trailforge export --store runs.sqlite3 --run-id YOUR_RUN_ID --output review.json
trailforge approve --store runs.sqlite3 --run-id YOUR_RUN_ID --actor local-operator --simulate --lose-response
trailforge reconcile --store runs.sqlite3 --run-id YOUR_RUN_ID --operation-id YOUR_OPERATION_ID --simulate
```

`python -m trailforge` also works. Init requires a new directory; export refuses to overwrite. JSON goes to stdout and run/error metadata to stderr. The source checkout and installed wheel both work without Diffwise, GitHub, a provider key or optional SDKs. A public PyPI release has not occurred; the command above installs this local package.

All ten examples are original configured mocks. They test Python/JavaScript/TypeScript evidence and honest unsupported/truncated/timeout coverage. The synthetic suite measures workflow smoke behavior, not real defect detection, confidence or model quality. Source code is read as data and never executed by these commands.

## Embed in another application

```python
from trailforge.adapters.mock import FixtureModel
from trailforge.controller import OfflineController
from trailforge.fixtures import load_fixture
from trailforge.store import SQLiteRunStore

controller = OfflineController(SQLiteRunStore("runs.sqlite3"), FixtureModel())
review_input = load_fixture("trailforge-demo/buggy-python")
run_id = controller.start(review_input)
record = controller.advance(run_id, review_input)
```

The original `offline/0.1` controller remains a compatible fixed Review profile. Use
the public `Goal`, `Task` and `Harness` API for other applications; see
[embedding](docs/embedding.md) and `examples/data_pipeline.py`. The
[Develop profile](docs/develop.md) binds candidate/check hashes and records six
phases; `examples/develop_data.py` exercises pause, recovery and verified handoff.
[Publication recovery](docs/publication.md) documents exact approvals, host guards
and conservative reconciliation. CLI approval is explicitly simulated, with no
verified customer identity or external effect. [Adapters](docs/adapters.md) cover
retrieval, allowlisted telemetry and optional LangGraph scheduling. Replacing a
mock with a network client does not create paid-budget or sandbox guarantees.

## Modes and limits

| Mode | Implemented properties | Limits |
| --- | --- | --- |
| Trusted local SQLite | Atomic journal/checkpoint and declared-unit admission, optimistic writes, scoped API/artifacts, bounded attempts, uncertain-outcome recovery, exact approvals and simulated publication reconciliation | Shared allowance only within one authority file; callbacks trusted; no customer authentication, distributed fencing, monetary budget, untrusted-code sandbox or privileged database-tamper defense |
| Optional Postgres | Scoped host authority, fenced leases, shared integer mock-unit ledgers, durable dispatch and approval simulation | 56 installed-extra cases pass on native Linux at the preceding checkpoint; each release requires a fresh exact-candidate result. Explicit host composition; neutral generic execution and standalone CLI remain SQLite. Stopped Windows database failure remains unresolved. |
| Develop host callbacks | Bound rounds, protected check hashes, host verdicts, candidate-bound handoff | Detects changed contracts; host callbacks are trusted, checks are maker-owned in examples |
| Linux-container checks | Pinned-image Docker runner; actual local Linux boundary suite passes under Docker Desktop/WSL2 | Five installed-wheel boundaries pass on native Linux at the preceding checkpoint; release CI repeats them. Independent protection remains unverified; Windows shell execution is trusted host mode. |
| Optional LangGraph | Explicit scheduler facade over kernel authority | Separate optional conformance; no graph checkpointer or provider/tracing configured |

Inspect/export include draft findings and quotes. `--include-source` additionally includes the full admitted source snapshot. Treat local stores and exports as project data and protect them accordingly. The base package does not request or manage provider credentials.

The alpha has no stable API or production certification. Independent acceptance and independent human review remain stable-release gates. Maintainer-controlled repository protection and CI have narrower scope. See [security](SECURITY.md), [contributing](CONTRIBUTING.md) and [change log](CHANGELOG.md). Third-party dependencies keep their own licenses; supplied PDFs/books/private references are excluded.

## Verify this package alone

```text
python -m pip install -r requirements-dev.lock
python scripts/verify_local.py
python scripts/verify_optional.py
```

The base verifier builds wheel/source distributions, installs the wheel alone,
requires Diffwise and optional SDKs absent, and exercises standalone tests, ten
fixtures, non-PR/Develop examples, separate-process recovery and simulated
publication reconciliation. The optional verifier installs the exact passed wheel
with its pinned workflow extra into a separate environment. Receipts live under
`.local/verification/`. Neither check reruns the stopped Postgres gate or proves
Linux-container isolation. See [release status](docs/release-status.md) and
[conformance and compatibility](docs/conformance.md) for the supported-mode
policy, actual process-crash/shared-budget exercises and image selection.

After that exact base wheel passes, optional authority and isolation are separate
checks:

```text
# Supply TRAILFORGE_DATABASE_URL for a disposable PostgreSQL database first.
python scripts/verify_postgres.py
# Explicitly preload a digest-pinned Linux Python image first.
python scripts/verify_linux.py --image <preloaded-image-at-sha256-digest>
```

The active repository CI and standalone export run four Linux/Windows Python
3.11 and 3.13.15 combinations, optional LangGraph/Postgres and five actual Linux
boundaries. Each uses clean installed distributions and reports full nonempty
execution without skipped cases. Presence of a workflow is not a passing result.
Only named reports and clean distributions are uploaded; runtime databases,
logs, credentials and customer files are excluded. Configuration checks remain
repository-owned and do not establish independently protected acceptance.
