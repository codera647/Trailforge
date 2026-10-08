CREATE TABLE dispatch_outbox (
    message_id text PRIMARY KEY,
    run_id text NOT NULL,
    event_sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    checksum text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'CLAIMED', 'DELIVERED', 'EXHAUSTED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    claim_owner text,
    claim_epoch bigint NOT NULL DEFAULT 0 CHECK (claim_epoch >= 0),
    claim_until timestamptz,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    error_code text CHECK (error_code IN ('TRANSPORT_FAILURE', 'CLAIM_EXPIRED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    FOREIGN KEY (run_id, event_sequence) REFERENCES events(run_id, sequence),
    UNIQUE (run_id, event_sequence)
);
CREATE INDEX dispatch_ready ON dispatch_outbox (available_at, created_at)
    WHERE status IN ('PENDING', 'CLAIMED');
