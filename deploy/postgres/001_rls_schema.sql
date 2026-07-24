-- Managed AutoML API production metadata bootstrap.
-- Apply with a privileged migration role, then run the API with a restricted role.

CREATE SCHEMA IF NOT EXISTS automl;

CREATE OR REPLACE FUNCTION automl.current_tenant_id()
RETURNS text
LANGUAGE sql
STABLE
AS $$
  SELECT NULLIF(current_setting('automl.tenant_id', true), '')
$$;

CREATE TABLE IF NOT EXISTS automl.resource_projection (
    tenant_id text NOT NULL,
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    payload jsonb NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, resource_type, resource_id)
);

ALTER TABLE automl.resource_projection ENABLE ROW LEVEL SECURITY;
ALTER TABLE automl.resource_projection FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_resource_projection ON automl.resource_projection;
CREATE POLICY tenant_isolation_resource_projection
ON automl.resource_projection
USING (tenant_id = automl.current_tenant_id())
WITH CHECK (tenant_id = automl.current_tenant_id());

CREATE TABLE IF NOT EXISTS automl.run_event (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    seq bigint NOT NULL CHECK (seq >= 1),
    event_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, seq),
    UNIQUE (tenant_id, event_id)
);

ALTER TABLE automl.run_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE automl.run_event FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_run_event ON automl.run_event;
CREATE POLICY tenant_isolation_run_event
ON automl.run_event
USING (tenant_id = automl.current_tenant_id())
WITH CHECK (tenant_id = automl.current_tenant_id());

CREATE TABLE IF NOT EXISTS automl.idempotency_record (
    tenant_id text NOT NULL,
    operation_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_fingerprint text NOT NULL,
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, operation_id, idempotency_key)
);

ALTER TABLE automl.idempotency_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE automl.idempotency_record FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_idempotency_record ON automl.idempotency_record;
CREATE POLICY tenant_isolation_idempotency_record
ON automl.idempotency_record
USING (tenant_id = automl.current_tenant_id())
WITH CHECK (tenant_id = automl.current_tenant_id());

CREATE TABLE IF NOT EXISTS automl.webhook_outbox (
    tenant_id text NOT NULL,
    delivery_id text NOT NULL,
    webhook_endpoint_id text NOT NULL,
    event_id text NOT NULL,
    event_type text NOT NULL,
    run_id text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('PENDING', 'DELIVERING', 'SUCCEEDED', 'RETRYING', 'EXHAUSTED')
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    exhausted_at timestamptz,
    PRIMARY KEY (tenant_id, delivery_id)
);

ALTER TABLE automl.webhook_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE automl.webhook_outbox FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_webhook_outbox ON automl.webhook_outbox;
CREATE POLICY tenant_isolation_webhook_outbox
ON automl.webhook_outbox
USING (tenant_id = automl.current_tenant_id())
WITH CHECK (tenant_id = automl.current_tenant_id());

CREATE INDEX IF NOT EXISTS run_event_replay_idx
ON automl.run_event (tenant_id, run_id, seq);

CREATE INDEX IF NOT EXISTS webhook_outbox_dispatch_idx
ON automl.webhook_outbox (status, next_attempt_at);
