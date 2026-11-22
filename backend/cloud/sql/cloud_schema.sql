-- Cloud API additions. Apply as admin after backend/db/schema.sql. Re-runnable.
--   psql "$TIMESCALE_SERVICE_URL" -f sql/cloud_schema.sql

-- Idempotent uploads: the laptop names each session; a retry finds the existing row.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS client_session_id uuid UNIQUE;

-- Device tokens. Only an HMAC of the token is stored. Managed with scripts/create_token.py.
CREATE TABLE IF NOT EXISTS api_clients (
    client_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL UNIQUE,
    token_hash text NOT NULL UNIQUE,
    scopes     text[] NOT NULL
               CHECK (scopes <@ ARRAY['profiles:write', 'sessions:write', 'aggregates:read']),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

GRANT SELECT ON api_clients TO sojourner_app;   -- token lookup; demo_reader gets nothing
-- Granting consent, and the FOR SHARE lock the API takes on a profile during an upload.
GRANT UPDATE (consent_state, demo_visible) ON profiles TO sojourner_app;
