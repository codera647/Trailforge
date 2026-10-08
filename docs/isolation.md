# Linux container validation

`DockerSandbox` requires a host-selected local Linux Docker engine and a preloaded
digest-pinned image providing python3 >=3.11. It never pulls images or falls back
to host execution. Candidate and protected checks are read-only mounts. Container
network is disabled, user is nonroot, capabilities are dropped, root filesystem
is read-only and declared time/output/memory/PID limits apply. Owner labels bind
cleanup to its own container. Returned execution receipts bind image, command,
goal, candidate and checks, while criterion_verdict stays UNKNOWN.
Cleanup requires a successful exact-name engine listing confirming absence;
daemon/inspection errors fail closed rather than becoming a missing-container PASS.

On a selected actual engine, run the original boundary suite:

```console
python scripts/verify_sandbox.py --image IMAGE_NAME@sha256:ACTUAL_IMAGE_DIGEST
```

Use an actual digest; the example above is a placeholder. The script checks
read-only candidate/protected/root writes, absent host canary/environment/socket,
nonroot UID, dropped capabilities, outbound network denial, cgroup memory bound,
PID exhaustion, output cap, timeout, owned cleanup and changed-candidate denial.
It uses original maker-owned checks, not independently protected acceptance.
An absent engine/image returns BLOCKED and nonzero, never skipped/PASS.
Docker command-construction tests cannot establish these isolation properties.

Local validation snapshot (8 October 2026): five actual cases PASS on Docker
Desktop 4.94.0, Linux Engine 29.8.2, WSL2 kernel 6.18.40.1, amd64. The exact
original test image was
`docker.io/library/python@sha256:f040863673aea2570c3ff6a5c3fb4c673a016cbc5375005ad145915922b6b78a`.
Docker may render its repository as `python@sha256:...`; preflight normalizes
Docker Hub's familiar/default names while requiring the same repository and
digest in bounded strict JSON metadata. Other repositories/registries/digests
are denied. This local result does not certify a different platform, image,
candidate, privileged host, malicious daemon or independently protected checks.

The `.github/workflows/conformance.yml` template is intended for the standalone
repository root. It prepares Linux/Windows portable lanes and an explicitly
selected manual Linux image lane. In the Diffwise monorepo this nested workflow
is inactive. No remote run, protected branch setting, public release or deployment
has occurred. CI validates code within its selected runner; it does not constitute
a sandbox for a privileged hostile host or malicious Docker daemon. Hosts own
engine access, filesystem permissions, image trust and private check provenance.
