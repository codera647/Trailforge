# Security policy

Maintainer: [codera647](https://github.com/codera647).

Report vulnerabilities privately through [GitHub private vulnerability reporting](https://github.com/codera647/Trailforge/security/advisories/new). Do not include exploits, credentials or customer source in public issues. Reports should identify the affected version, supported mode, impact and a minimal original reproduction. The maintainer will acknowledge reports as availability permits; no response-time SLA is promised.

## Supported versions

| Version | Security fixes |
| --- | --- |
| Latest 0.1 alpha | Best effort; upgrade to the latest alpha |
| Older development/alpha candidates | Upgrade required |

There is no stable release or production security certification. The alpha API may change. See [release status](docs/release-status.md) for exact validation scope.

## Boundaries

SQLite assumes a trusted host/operator and trusted database access. Checksums establish consistency, not authentication against a privileged writer. Hosts own user identity, credential storage, permissions, model handling and publication scope. Source and model output never grant authority.

Linux containers are the supported untrusted execution target. The digest-pinned runner is tested on native Linux; Windows supports the CLI and trusted local development. Plain local callbacks and shells are trusted execution. Keep authority records, acceptance checks, credentials, Docker sockets and unrelated projects outside candidate-writable execution. Docker access itself is privileged host infrastructure.

Installed PostgreSQL conformance passes on Linux for the legacy Review authority, dispatch and broker adapters. Neutral generic execution and the standalone CLI use SQLite. Historical Windows database timeouts remain unresolved and do not establish Windows PostgreSQL support.

The supplied Review fixtures and publication adapter are mocks. No real GitHub publisher, customer authentication, model-quality evaluation or paid-provider budget guarantee is supplied. Optional LangGraph schedules work over kernel authority; it does not add independent policy or tracing.

Inspect/export can contain source and evidence quotes. Protect stores and exports as project data and review them before disclosure. Synthetic examples are original; supplied books, private PDFs, credentials and runtime diagnostics are excluded from distributions.
