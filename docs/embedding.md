# Embed Trailforge in another product

The base package requires only Python's standard library. Explicit host composition supplies trusted adapter implementations and scope. Models, source documents and callback data do not choose adapter imports or grant roles. The host is responsible for actual customer authentication, provider terms/keys, independently controlled criteria and deployment.

## Application-neutral workflow

`trailforge.runtime.Harness` runs a typed `Goal` with topologically ordered `Task` records through registered `TaskAdapter` implementations. This is separate from the backward-compatible fixed `offline/0.1` Review controller. A `FunctionAdapter` is trusted Python host code, not sandboxed agent code; never dynamically import a module specified by model output.

See [data_pipeline.py](../examples/data_pipeline.py) for a complete non-PR example. The host registers a data-transform tool and a separate verifier, scopes the harness to a tenant/resource, starts a goal, pauses after a task and resumes through a fresh harness. Run it from this package after installation:

```text
python examples/data_pipeline.py
```

Goals bind tasks, inputs, permitted effects, required check IDs, adapter metadata and limits. A changed goal or adapter cannot resume the old run. Every task is durably admitted and charged before its callback; output is bounded JSON and runner-issued receipts bind result digests to the exact goal. Models returning JSON containing `verdict: PASS` cannot construct the typed host `VerifiedResult` authority.

`COMPLETE` means the explicitly registered host checks returned PASS. Those checks remain the host's responsibility; the harness does not independently prove an arbitrary objective or make same-process/plugin code adversary-proof. A model cannot be registered as a required check merely by returning a verdict-shaped dictionary.

## Budgets and recovery

The generic local runtime conserves declared per-run reservation units and a maximum of three attempts per task. Shared tenant/global declared-unit ledgers in the same SQLite authority file are updated atomically with task admission and journal/state commits. Creating a new run or host instance does not reset them. Changing configured caps implicitly is denied. They are not shared across separate database files and do not provide provider monetary guarantees. Existing Postgres mock-call ledgers belong to the original offline Review profile; do not advertise those for the generic pipeline.

A callback exception or invalid output becomes UNKNOWN, with its reservation retained. A live local worker prevents reentry; an expired worker cannot commit. Recovery retires the expired attempt into WAITING_HUMAN. Trusted host `resolve_unknown(..., disposition="RETRY" or "CANCEL")` does not reset consumed attempts or units. `cancel()` revokes commit ownership and retains admitted uncertainty; it cannot undo a running callback or provider charge.

Leases use the trusted host clock and do not preempt synchronous Python handlers. Thread/process/remote execution belongs to a tested adapter. External-write effects are denied by the generic runtime until a guarded publisher integration is implemented. This is intentional fail-closed behavior, not a functioning arbitrary publication adapter.

## Artifact and integration ports

`LocalArtifactStore` stores bounded content under content hashes in an explicitly configured host scope. Receipts include scope/content hashes, size and media type; read/dedup verify bytes. Links/junctions and hard-linked files are rejected. This is local access filtering and integrity, not customer authentication, encrypted customer storage or privileged filesystem-tamper defense.

Versioned ports describe task/model/tool/verifier, artifacts, workflow, retrieval, publisher and telemetry integration. A protocol declaration is not an implemented adapter or a conformance receipt. Actual optional adapters and capability matrices remain release work. Never give an untrusted callback the broker/store, provider keys or a general authenticated shell.

## Linux execution preparation

`DockerSandbox` prepares a pinned-image, local Linux-engine check boundary: read-only candidate/check mounts, no container network, non-root user, dropped capabilities, no added privileges, CPU/memory/PID/time/output limits and cleanup restricted to the owned container label. It never pulls an image automatically or falls back to the host shell. Its default receipt has criterion_verdict UNKNOWN; execution/exit code alone does not establish semantic completion.

Current tests exercise command/binding/preflight logic only. An actual Docker engine and vetted pinned image are required for Linux conformance. Develop candidate edits, full loop/profile integration, adversarial container checks and actual cleanup/timeout tests remain incomplete. Do not advertise these preparation tests as sandbox guarantees.
