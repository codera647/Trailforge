CREATE TABLE IF NOT EXISTS runs (
    run_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    resource_id text NOT NULL,
    binding_digest text NOT NULL,
    version bigint NOT NULL CHECK (version >= 0),
    payload text NOT NULL,
    checksum text NOT NULL,
    lease_owner text,
    lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    lease_until timestamptz
);
CREATE TABLE IF NOT EXISTS events (
    run_id text NOT NULL REFERENCES runs(run_id),
    sequence bigint NOT NULL CHECK (sequence >= 0),
    kind text NOT NULL,
    state_digest text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs(run_id),
    role text NOT NULL CHECK (role IN ('security', 'correctness', 'tests', 'documentation')),
    payload jsonb NOT NULL,
    UNIQUE (run_id, role)
);
CREATE TABLE IF NOT EXISTS budgets (
    scope_kind text NOT NULL CHECK (scope_kind IN ('GLOBAL', 'TENANT', 'RUN')),
    scope_key text NOT NULL,
    unit_limit bigint NOT NULL CHECK (unit_limit >= 0),
    charged bigint NOT NULL DEFAULT 0 CHECK (charged >= 0 AND charged <= unit_limit),
    PRIMARY KEY (scope_kind, scope_key)
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs(run_id),
    operation_key text NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE (run_id, operation_key)
);
