CREATE TABLE broker_policies (
    tenant_id text NOT NULL, resource_id text NOT NULL,
    version bigint NOT NULL CHECK (version > 0), enabled boolean NOT NULL,
    PRIMARY KEY (tenant_id, resource_id)
);
CREATE TABLE broker_principals (
    tenant_id text NOT NULL, resource_id text NOT NULL, actor_id text NOT NULL,
    roles jsonb NOT NULL, version bigint NOT NULL CHECK (version > 0),
    PRIMARY KEY (tenant_id, resource_id, actor_id),
    FOREIGN KEY (tenant_id, resource_id) REFERENCES broker_policies
);
CREATE TABLE broker_sessions (
    token_hash text PRIMARY KEY, tenant_id text NOT NULL, resource_id text NOT NULL,
    actor_id text NOT NULL, expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false,
    FOREIGN KEY (tenant_id, resource_id, actor_id) REFERENCES broker_principals
);
CREATE TABLE broker_targets (
    run_id text PRIMARY KEY REFERENCES runs, head_revision text NOT NULL,
    epoch bigint NOT NULL CHECK (epoch > 0)
);
CREATE TABLE broker_usage (
    run_id text PRIMARY KEY REFERENCES runs,
    accepted bigint NOT NULL DEFAULT 0 CHECK (accepted BETWEEN 0 AND 64)
);
CREATE TABLE broker_requests (
    request_id text PRIMARY KEY, run_id text NOT NULL REFERENCES runs,
    requester text NOT NULL, policy_digest text NOT NULL, target_epoch bigint NOT NULL,
    payload_digest text NOT NULL, preview jsonb NOT NULL, expires_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('PENDING_HUMAN','APPROVED','REJECTED','REVOKED','SIMULATED')),
    decision jsonb, issuer_session text REFERENCES broker_sessions(token_hash),
    issuer_version bigint, receipt jsonb,
    CHECK ((status='PENDING_HUMAN') = (decision IS NULL)),
    CHECK ((status='SIMULATED') = (receipt IS NOT NULL))
);
CREATE TABLE broker_audit (
    sequence bigserial PRIMARY KEY, tenant_id text NOT NULL, resource_id text NOT NULL,
    actor_id text NOT NULL, run_id text, kind text NOT NULL, details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, resource_id) REFERENCES broker_policies
);
