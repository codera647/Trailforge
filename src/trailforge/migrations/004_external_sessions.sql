-- External identity sessions cannot be admitted by an unguarded broker.
ALTER TABLE broker_sessions ADD COLUMN authority_kind text NOT NULL DEFAULT 'LOCAL'
    CHECK (authority_kind IN ('LOCAL', 'EXTERNAL_IDENTITY'));
