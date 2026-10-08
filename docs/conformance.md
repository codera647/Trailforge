# Conformance and compatibility policy

The first public candidate is 0.1.0a1: there is no stable compatibility promise.
The base package requires Python >=3.11. Prepared CI covers Linux/Windows at
Python 3.11 and 3.13; only actually executed platform/version receipts establish
coverage. Other interpreter versions and operating systems are uncertified.
Optional LangGraph pins its tested engine version; resolved transitive versions
are captured per verification run. Provider/model evaluation belongs to the host.

`verify_local.py` builds clean distributions and runs all base conformance tests
against the installed wheel outside the checkout. `verify_optional.py` requires
that exact passed wheel and independently installs the optional workflow extra.
No successful older report establishes acceptance after source changes.

Release exercises include real separate-process neutral recovery, hard worker
termination after durable admission, UNKNOWN retention and explicit bounded retry,
terminal replay without additional calls, and sixteen runs contending under four
workers for shared tenant/global allowances. These establish bounded local
behavior; they are not throughput/latency benchmarks or distributed conformance.
Original offline Review and neutral execution records can coexist in one SQLite
authority file without coercing their protocols or changing existing Review state.
Unknown protocol versions are denied. Postgres migration files remain immutable;
optional Postgres is exercised independently by `verify_postgres.py` against the
exact freshly passed wheel. It requires an explicit disposable database URL and
installs the pinned PostgreSQL extra into a separate environment. The 56 original
authority, migration, approval, dispatch and recovery cases use bundled synthetic
fixtures. Separate installed worker processes cover database recovery; the
standalone CLI continues to use SQLite. Neutral generic distributed PostgreSQL
execution is not supplied. Stopped Windows database attempts remain historical.

Hosts must version adapter IDs when behavior/authority contracts change. Manifest
digests bind capability metadata, not arbitrary Python implementation bytes. Do
not reuse an old ID to silently change permissions or resume incompatible logic.
New goal fields/criteria/capabilities change the goal binding and require explicit
migration/new work; the harness never auto-upgrades a persisted approval or goal.
Future stable version changes need exact wheel, migration and supported-mode
conformance plus release notes before a compatibility guarantee is made.

Actual Linux isolation is verified separately through `verify_sandbox.py`, with
positive engine confirmation of owned-container absence. An unavailable engine
or failing inspection cannot count as cleanup success. Resolve an original test
image without installing it:

```console
python scripts/resolve_sandbox_image.py --architecture amd64 --output image-selection.json
```

This reads bounded official metadata, selects one active Linux platform digest
and makes no runtime claim. On the selected engine, explicitly preload that exact
image and run the [Linux suite](isolation.md). The suite reports BLOCKED when a
runtime/image is absent. No host shell is substituted for untrusted execution.

The active repository workflow and standalone export both run Linux/Windows
Python 3.11 and 3.13.15 installed-wheel adoption, Linux pinned optional LangGraph
and PostgreSQL, and the five Linux isolation cases. Each optional/environment
check requires its own freshly passed base wheel and unchanged source manifest.
Empty suites, skipped cases and expected failures cannot count as acceptance.
PostgreSQL receipts include actual server/dependency versions and positive
absence checks for each exact test-owned schema; no unrelated schema is deleted.
Artifacts admit only named JSON reports and clean wheel/sdist distributions.
Logs, databases, arbitrary `.local` files, credentials and customer source are
excluded. `verify_workflows.py` enforces the read-only configuration and negative
boundary mutations; this remains repository-owned, not independently protected.
The workflows use JSON, a YAML subset, for standard-library inspection as
defined by the [YAML specification](https://yaml.org/spec/1.2.2/).
