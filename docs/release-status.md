# Alpha release status

Trailforge 0.1.0a1 is the first public MIT alpha candidate, maintained by [codera647](https://github.com/codera647) at [codera647/Trailforge](https://github.com/codera647/Trailforge). The owner explicitly authorized completing and publishing this standalone harness. This authorization is not a claim that the owner personally executed tests or that an independent human reviewer approved the implementation.

## Implemented and evaluated scope

The SDK-free kernel provides bounded generic execution, SQLite authority, artifacts, recovery, trusted-host Develop, exact retrieval/telemetry ports and simulated publication. Optional LangGraph is a scheduler facade. PostgreSQL covers the legacy Review/controller, dispatch and broker contracts; generic execution and the CLI remain SQLite.

The preceding implementation checkpoint passed all six conformance jobs and the original foundation checks, including the latest merged source. The first-alpha candidate must pass fresh standalone CI before tagging. Use the repository's [Actions history](https://github.com/codera647/Trailforge/actions/workflows/conformance.yml) and commit-bound receipts to identify the exact verified version.

| Gate | Required execution |
| --- | --- |
| Portable installed wheel | Ubuntu 24.04 / Windows 2022 × Python 3.11 / 3.13.15; 62 cases each, eight CLI journeys, ten fixtures and separate-process recovery |
| Optional LangGraph | Two installed-extra cases on the exact passed base wheel |
| Optional PostgreSQL | 56 installed-extra cases on PostgreSQL 18.6, exact base wheel and positive owned-schema cleanup |
| Linux isolation | Five actual installed-wheel container boundary cases on native Linux |
| Configuration | Pinned read-only conformance workflow and negative unsafe-configuration cases |

Each admitted suite must be nonempty, complete and free of skips, expected failures, failures and errors. Passing older source is historical evidence after changes. Check hashes and actual reports rather than assuming a workflow's presence means PASS.

## Release limits

Alpha APIs can change. There is no stable/production certification, real provider quality evaluation, actual GitHub publisher, real customer authorization, monetary budgeting guarantee or generic distributed PostgreSQL execution. Windows supports local trusted development and the CLI, not untrusted isolation or validated PostgreSQL authority.

Repository protection is a maintainer-controlled merge gate. Candidate-owned workflow/tests do not establish independently administered acceptance or independent human review. A separately trusted reviewer/acceptance authority remains a stable-release gate; it cannot be created merely by assigning CODEOWNERS to the author. Public alpha publication is explicitly authorized despite that remaining stable-release limit. Diffwise cloud deployment still requires the owner's personal validation and separate deployment choice.

The historical Windows PostgreSQL timeout and local Docker download failure remain preserved in the private project records. Successful native Linux verification does not diagnose those Windows failures.

See [security policy](../SECURITY.md), [contributing](../CONTRIBUTING.md), [dependency notices](../THIRD_PARTY_NOTICES.md) and [conformance](conformance.md). Private references, runtime stores, credentials and host product history are excluded from this standalone repository and its distributions.
