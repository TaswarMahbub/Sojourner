-- Sojourner: Tiger Cloud (TimescaleDB) schema.
-- Run as admin: psql "$TIMESCALE_SERVICE_URL" -f schema.sql
-- Re-runnable. Not in one transaction (continuous aggregates can't be created in one).
--
-- Privacy:
--   * Only derived numbers; no video or images.
--   * Rows are keyed by a random profile_id; display_name is optional.
--   * Deleting a profile cascades to all base tables. `tiger_store.py delete-profile` also
--     refreshes the summaries so the profile's aggregate rows go too.
--   * demo_reader sees only aggregated, consented, demo_visible data.
--   * sojourner_app is the least-privilege role for teammates and tiger_store.py.
--
-- Contract coverage (docs/contracts.md, schema "4"):
--   Camera feature stream -> signal_samples: capture time, 52 blendshapes (MediaPipe names),
--     head_pose yaw/pitch/roll, tracking_ok, confidence (NULL when absent).
--   Gesture classification -> control_events. OPEN: interim cue labels; window and
--     model_version not stored. Waits on a trained model and label list.
--   Motor command/status: not stored, by decision (not on the stop path). Their
--     boot_nonce/session_id are UDP sessions, not sessions.session_id.
--   Status-gate state: not stored; the contract requires a durable local file.
--   OPEN:
--     - tracking_state reduced to tracking_ok; lost frames write no row.
--     - head_pose.matrix validated but not stored.
--     - schema_version not recorded.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Relational tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS profiles (
    profile_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name  text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    consent_state text NOT NULL DEFAULT 'pending'
                  CHECK (consent_state IN ('pending', 'granted', 'withdrawn')),
    demo_visible  boolean NOT NULL DEFAULT false   -- opt-in per profile for any public demo view
);

CREATE TABLE IF NOT EXISTS calibrations (
    calibration_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id     uuid NOT NULL REFERENCES profiles (profile_id) ON DELETE CASCADE,
    ts             timestamptz NOT NULL DEFAULT now(),
    tier           smallint NOT NULL CHECK (tier BETWEEN 1 AND 3),
    thresholds     jsonb NOT NULL DEFAULT '{}'::jsonb,       -- per-channel thresholds, deadbands, bindings
    metric_summary jsonb NOT NULL DEFAULT '{}'::jsonb        -- range, SNR, repeatability, latency, fatigue drift, FP rate
);
CREATE INDEX IF NOT EXISTS calibrations_profile_ts_idx ON calibrations (profile_id, ts DESC);

CREATE TABLE IF NOT EXISTS model_versions (
    model_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    algorithm            text NOT NULL,
    feature_schema       jsonb NOT NULL DEFAULT '{}'::jsonb,
    training_set_version text,
    evaluation_summary   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id        uuid NOT NULL REFERENCES profiles (profile_id) ON DELETE CASCADE,
    calibration_id    uuid REFERENCES calibrations (calibration_id) ON DELETE SET NULL,
    model_id          uuid REFERENCES model_versions (model_id) ON DELETE SET NULL,
    tier              smallint CHECK (tier BETWEEN 1 AND 3),
    started_at        timestamptz NOT NULL DEFAULT now(),
    ended_at          timestamptz,
    simulator_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,    -- time to clear, collisions, cues per metre, hesitation...
    -- Honest provenance: never let seeded test data pass as a measured session.
    data_origin       text NOT NULL DEFAULT 'measured'
                      CHECK (data_origin IN ('measured', 'synthetic_seed'))
);
CREATE INDEX IF NOT EXISTS sessions_profile_started_idx ON sessions (profile_id, started_at DESC);

CREATE TABLE IF NOT EXISTS labeled_trials (
    trial_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id           uuid NOT NULL REFERENCES profiles (profile_id) ON DELETE CASCADE,
    cue                  text NOT NULL,
    started_at           timestamptz NOT NULL,
    ended_at             timestamptz NOT NULL,
    feature_artifact_ref text,    -- path or object key of the derived-feature file. Never raw video.
    CHECK (ended_at >= started_at)
);
CREATE INDEX IF NOT EXISTS labeled_trials_profile_cue_idx ON labeled_trials (profile_id, cue, started_at DESC);

-- ---------------------------------------------------------------------------
-- Hypertables (profile_id denormalised: per-profile queries and deletion need no join)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signal_samples (
    ts           timestamptz NOT NULL,
    session_id   uuid NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    profile_id   uuid NOT NULL REFERENCES profiles (profile_id) ON DELETE CASCADE,
    channel_name text NOT NULL,
    value        double precision NOT NULL,
    tracking_ok  boolean NOT NULL,
    -- NULL when the frame had no score (stock MediaPipe). Never substituted.
    confidence   double precision
);
-- Upgrade for databases created before this column.
ALTER TABLE signal_samples ADD COLUMN IF NOT EXISTS confidence double precision;

SELECT create_hypertable('signal_samples', 'ts',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS signal_samples_profile_ts_idx
    ON signal_samples (profile_id, ts DESC);
CREATE INDEX IF NOT EXISTS signal_samples_session_channel_ts_idx
    ON signal_samples (session_id, channel_name, ts DESC);

CREATE TABLE IF NOT EXISTS control_events (
    ts          timestamptz NOT NULL,
    session_id  uuid NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    profile_id  uuid NOT NULL REFERENCES profiles (profile_id) ON DELETE CASCADE,
    event_sequence integer NOT NULL,          -- cue order in session; not the motor-command sequence
    cue         text NOT NULL,
    confidence  double precision,
    stop_reason text,                         -- set when the event halted motion
    PRIMARY KEY (session_id, event_sequence, ts) -- hypertable keys must include ts
);

SELECT create_hypertable('control_events', 'ts',
                         chunk_time_interval => INTERVAL '7 days',
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS control_events_profile_ts_idx
    ON control_events (profile_id, ts DESC);

-- Upgrade: rename the old "sequence" column.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'control_events'
                 AND column_name = 'sequence') THEN
        ALTER TABLE control_events RENAME COLUMN sequence TO event_sequence;
    END IF;
END
$$;

-- Optional, for larger data volumes:
-- ALTER TABLE signal_samples SET (timescaledb.compress,
--     timescaledb.compress_segmentby = 'session_id, channel_name',
--     timescaledb.compress_orderby = 'ts DESC');
-- SELECT add_compression_policy('signal_samples', INTERVAL '7 days');
-- SELECT add_retention_policy('signal_samples', INTERVAL '180 days');

-- ---------------------------------------------------------------------------
-- 1-minute summaries (continuous aggregates). Value stats use tracking_ok samples only.
-- ---------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS signal_summary_1min
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket(INTERVAL '1 minute', ts) AS bucket,
    session_id,
    profile_id,
    channel_name,
    count(*)                                         AS n_samples,
    sum(CASE WHEN tracking_ok THEN 1 ELSE 0 END)     AS n_ok,
    avg(CASE WHEN tracking_ok THEN value END)        AS avg_ok,
    min(CASE WHEN tracking_ok THEN value END)        AS min_ok,
    max(CASE WHEN tracking_ok THEN value END)        AS max_ok,
    stddev(CASE WHEN tracking_ok THEN value END)     AS sd_ok
FROM signal_samples
GROUP BY bucket, session_id, profile_id, channel_name
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS control_summary_1min
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket(INTERVAL '1 minute', ts) AS bucket,
    session_id,
    profile_id,
    count(*)                                              AS n_events,
    sum(CASE WHEN stop_reason IS NOT NULL THEN 1 ELSE 0 END) AS n_stops,
    avg(confidence)                                       AS avg_confidence
FROM control_events
GROUP BY bucket, session_id, profile_id
WITH NO DATA;

-- start_offset NULL: invalidated regions of any age get re-materialised (a fixed 30-day offset
-- left deleted profiles' older rows behind). Remove-then-add replaces an existing policy.
SELECT remove_continuous_aggregate_policy('signal_summary_1min', if_exists => TRUE);
SELECT add_continuous_aggregate_policy('signal_summary_1min',
    start_offset => NULL, end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');
SELECT remove_continuous_aggregate_policy('control_summary_1min', if_exists => TRUE);
SELECT add_continuous_aggregate_policy('control_summary_1min',
    start_offset => NULL, end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');

-- Per-session, per-channel stats: the cross-session drift trend.
CREATE OR REPLACE VIEW session_channel_stats AS
SELECT
    s.profile_id,
    s.session_id,
    s.started_at,
    s.tier,
    s.data_origin,
    c.channel_name,
    max(c.max_ok) - min(c.min_ok)                          AS observed_range,
    sum(c.avg_ok * c.n_ok) / nullif(sum(c.n_ok), 0)        AS mean_value,
    sum(c.n_samples)                                       AS n_samples,
    sum(c.n_ok)::double precision / nullif(sum(c.n_samples), 0) AS tracking_ok_frac
FROM signal_summary_1min c
JOIN sessions s ON s.session_id = c.session_id
GROUP BY s.profile_id, s.session_id, s.started_at, s.tier, s.data_origin, c.channel_name;

-- ---------------------------------------------------------------------------
-- Public demo: demo_reader reads only these two views (they run with owner rights).
-- Hashed aliases and aggregates for consented, demo_visible profiles. No names, ids or samples.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW demo_session_summary AS
SELECT
    'P-' || left(md5(p.profile_id::text), 6)                AS profile_alias,
    left(md5(s.session_id::text), 8)                        AS session_alias,
    s.data_origin,
    s.tier,
    s.started_at,
    round(extract(epoch FROM (s.ended_at - s.started_at))::numeric, 1) AS duration_s,
    s.simulator_metrics
FROM sessions s
JOIN profiles p ON p.profile_id = s.profile_id
WHERE p.consent_state = 'granted' AND p.demo_visible;

CREATE OR REPLACE VIEW demo_channel_trend AS
SELECT
    'P-' || left(md5(p.profile_id::text), 6)                AS profile_alias,
    left(md5(v.session_id::text), 8)                        AS session_alias,
    v.data_origin,
    v.started_at,
    v.channel_name,
    round(v.observed_range::numeric, 3)                     AS observed_range,
    round(v.tracking_ok_frac::numeric, 3)                   AS tracking_ok_frac
FROM session_channel_stats v
JOIN profiles p ON p.profile_id = v.profile_id
WHERE p.consent_state = 'granted' AND p.demo_visible;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo_reader') THEN
        CREATE ROLE demo_reader NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM demo_reader;
GRANT USAGE ON SCHEMA public TO demo_reader;
GRANT SELECT ON demo_session_summary, demo_channel_trend TO demo_reader;

-- For a dashboard (don't commit the password):
--   CREATE ROLE demo_dashboard LOGIN PASSWORD '<set-me>' IN ROLE demo_reader;

-- ---------------------------------------------------------------------------
-- sojourner_app: insert/read app tables, close sessions, delete profiles. Cannot DROP,
-- TRUNCATE, ALTER, update samples/events, or refresh aggregates (admin only; the policy
-- refreshes every minute). Created NOLOGIN; enable it and set the password interactively:
--   psql "$TIMESCALE_SERVICE_URL" -c 'ALTER ROLE sojourner_app LOGIN'
--   psql "$TIMESCALE_SERVICE_URL" -c '\password sojourner_app'
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sojourner_app') THEN
        CREATE ROLE sojourner_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO sojourner_app;
GRANT SELECT, INSERT ON profiles, calibrations, model_versions, sessions, labeled_trials,
                        signal_samples, control_events TO sojourner_app;
GRANT UPDATE (ended_at, simulator_metrics) ON sessions TO sojourner_app;
GRANT DELETE ON profiles TO sojourner_app;
GRANT SELECT ON signal_summary_1min, control_summary_1min, session_channel_stats,
                demo_session_summary, demo_channel_trend TO sojourner_app;
