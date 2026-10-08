# Exact approval and publication recovery

`GuardedPublisher` is a trusted host library component. Hosts construct an `Intent`
with tenant/resource, goal and policy digests, current revision, action and payload.
Approval binds every field and the configured publisher ID/mode. Host-supplied
`live_guard(binding, actor, action)` must authenticate the actor and check current
membership, session, repository/revision, allowed action and policy. Model output
and arbitrary actor strings cannot establish those facts. Real provider adapters
must enforce authority again at their actual write boundary. Local SQLite files
and plugins are trusted host resources, with no database-admin tamper protection.

`approve` issues a bounded expiring approval. `revoke` marks it unavailable for new
dispatch. `publish` consumes it and atomically records SENDING before calling the
provider. Replaying that exact intent returns the existing operation, including
SENDING/UNKNOWN, even with a second approval. Intentional repetition needs a new
meaningful operation contract; changing a random payload field to force a retry
is unsafe. One authority file must own the operation across workers. Independent
authority databases do not provide shared deduplication.

An adapter acknowledgement must match operation ID and exact binding digest.
Exceptions, response loss and invalid acknowledgements become UNKNOWN. Recovery
calls `reconcile` and only the adapter's read/inspection endpoint; it never calls
execute again. Missing or ambiguous remote evidence stays UNKNOWN. A receipt is
provider evidence, not proof of authorization or universal exactly-once delivery.
Revocation cannot reverse an already accepted remote operation. The host owns
the incident/manual disposition of unresolved outcomes.

`DurableSimulationPublisher` uses a separate SQLite file as an original mock remote.
It supports response loss after durable acceptance, so fresh processes can exercise
reconciliation without credentials, network calls or publication to GitHub.

After completing the offline quickstart, exercise the CLI with its run ID:

```console
trailforge approve --run-id RUN_ID --actor local-operator --simulate --lose-response
trailforge reconcile --run-id RUN_ID --operation-id OPERATION_ID --simulate
```

The first command explicitly approves and dispatches only to the local simulator;
the second inspects its result. `--simulate` is mandatory. The actor is a local
operator label, and output declares LOCAL_SIMULATION and external_effect NONE.
CLI approval requires a fully validated Review draft; partial coverage is denied.
Use the library with a configured scoped host publisher for real integration.
No live GitHub publisher, customer identity, or monetary guarantee is supplied here.
