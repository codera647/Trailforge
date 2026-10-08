# Host adapters and optional workflow scheduling

The dependency-free base exposes `AdapterCapabilities`, `TaskRequest`,
`FunctionAdapter` and `VerifiedResult`. Register trusted host implementations
explicitly. Capability IDs and manifests bind resume. No import path, provider URL,
command, credential or permission supplied by model data is dynamically loaded.
Callbacks may call a host-selected model/tool; hosts enforce real provider spend,
timeouts and identity. Declared integer units are not metered provider prices.

`LocalRetrieval` implements bounded literal text retrieval from an explicit file
allowlist. It checks host permission before and after reading, binds scope and
revision plus snapshot/content hashes, and labels all retrieved text untrusted
source data. It is not a semantic/vector ranker. Changing files or revision denies
reuse. Do not execute instructions inside retrieved text. `JsonEventSink` accepts
only fixed event fields/kinds, canonical run UUID, scope digest and integer units;
it rejects arbitrary payload/source/credential fields before writing. Hosts own
the stream, access, retention and exporter integration. Its lock covers one
instance, not distributed logging. Neither adapter sends data to a cloud service.

The optional `langgraph` extra pins LangGraph 1.2.14. `LangGraphWorkflow` is a
single-node scheduling facade over `Harness.advance`, preserving the kernel's
scope, goal, reservations, leases, uncertain attempts and terminal replay rules.
It accepts a run ID, not arbitrary graph updates or approval resume values.
Reconstruct it with the same kernel database and goal to resume. It configures
no graph checkpointer or provider, and disables inherited automatic tracing in
its invocation context; no graph durability guarantee
is claimed. Host graphs may compose this facade, with kernel checks remaining
mandatory. Graph replay cannot mint approval or release an UNKNOWN reservation.

Install the optional extra from this package and run its separate conformance:

```console
python -m pip install ".[langgraph]"
python -m unittest discover -s tests_optional -v
```

Optional integration is not release-verified until that exact dependency and the
actual tests pass. Base-wheel conformance explicitly requires LangGraph absent.
Design references: [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
and [persistence](https://docs.langchain.com/oss/python/langgraph/persistence).
Tracing control follows the [official context API](https://reference.langchain.com/python/langsmith/run_helpers/tracing_context).
