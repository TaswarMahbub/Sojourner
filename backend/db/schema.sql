-- Sojourner: Tiger Cloud (TimescaleDB) schema
--
-- Run with:  psql "$TIMESCALE_SERVICE_URL" -f schema.sql   (as the admin role, e.g. tsdbadmin)
-- Do NOT wrap in a single transaction: continuous aggregates cannot be created inside one.
-- Safe to re-run (idempotent).
--
-- Privacy design:
--   * No raw video or images are stored anywhere. Only derived numeric channel values.
--   * Every record is keyed by a pseudonymous profile_id (random UUID). display_name is optional.
--   * Deleting a profile cascades to every row that references it, including both hypertables.
--   * demo_reader (below) can only see aggregated, consented, demo_visible data.
--   * sojourner_app (below) is the least-privilege role teammates and tiger_store.py connect as.
--
-- Contract coverage (docs/contracts.md, schema "4", as merged to main in #1):
--   Camera feature stream -> signal_samples. Stored: capture time (timestamp_ms), the 52
--     blendshape scores under MediaPipe's names, head_pose yaw/pitch/roll in degrees,
--     tracking_ok (tracking_state = 'tracking'), and confidence (NULL when the producer had none).
--   Gesture classification -> control_events. OPEN: not in contract form yet. cue still holds
--     interim labels and the window and model_version fields are not stored. Waits on a trained
--     model and a reviewed label list.
--   Motor command and motor status -> not stored, by decision. Storage is not part of the
--     real-time stop path. The contract's boot_nonce/session_id are UDP link sessions and are
--     not sessions.session_id; if datagrams are ever logged they get their own table.
--   Persisted status-gate state (challenge and status_sequence high-water marks) -> not stored
--     here. The contract requires a durable local file on the laptop, replaced atomically and
--     written before a challenge is transmitted; a network database cannot be on that path.
--   OPEN, not decided:
--     - tracking_state is reduced to tracking_ok, so partial and lost are not distinguished, and
--       a lost frame (no features, no pose) writes no row. tracking_ok fractions therefore
--       count only frames that carried values.
--     - head_pose.matrix (translation and scale) is validated on load but not stored.
--     - The schema_version of loaded data is not recorded.

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
-- Hypertables
-- profile_id is denormalised into both hypertables so that per-profile time queries,
-- per-profile indexes and one-action deletion do not need a join against sessions.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signal_samples (
    ts           timestamptz NOT NULL,
    session_id   uuid NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    profile_id   uuid NOT NULL REFERENCES profiles (profile_id) ON DELETE CASCADE,
    channel_name text NOT NULL,
    value        double precision NOT NULL,
    tracking_ok  boolean NOT NULL,
    -- Producer's per-frame score when one exists. NULL when the frame carried none
    -- (stock MediaPipe publishes null). Never replaced with 0, 1 or a threshold.
    confidence   double precision
);
-- Brings a database created before the confidence column up to date.
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
    event_sequence integer NOT NULL,          -- order of the cue within its session (1, 2, ...);
                                              -- not the contract's motor-command sequence
    cue         text NOT NULL,
    confidence  double precision,
    stop_reason text,                         -- NULL for ordinary cues; set when the event halted motion
    PRIMARY KEY (session_id, event_sequence, ts) -- a hypertable unique key must include the time column
);

SELECT create_hypertable('control_events', 'ts',
                         chunk_time_interval => INTERVAL '7 days',
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS control_events_profile_ts_idx
    ON control_events (profile_id, ts DESC);

-- Renames the column on a database created when it was called "sequence". The primary key
-- follows the rename. Skipped once the column is event_sequence.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'control_events'
                 AND column_name = 'sequence') THEN
        ALTER TABLE control_events RENAME COLUMN sequence TO event_sequence;
    END IF;
END
$$;

-- Optional once data volume grows (left off so small demo deletes stay simple):
-- ALTER TABLE signal_samples SET (timescaledb.compress,
--     timescaledb.compress_segmentby = 'session_id, channel_name',
--     timescaledb.compress_orderby = 'ts DESC');
-- SELECT add_compression_policy('signal_samples', INTERVAL '7 days');
-- SELECT add_retention_policy('signal_samples', INTERVAL '180 days');

-- ---------------------------------------------------------------------------
-- Time-window summaries (continuous aggregates)
-- Value statistics use tracking_ok samples only, so lost tracking cannot inflate range.
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

SELECT add_continuous_aggregate_policy('signal_summary_1min',
    start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('control_summary_1min',
    start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);

-- Per-session, per-channel statistics. This is the longitudinal trend input:
-- order by started_at for one profile and one channel to see range / tracking drift across sessions.
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
-- Public-demo safety: a role that can read ONLY these two views.
-- Views run with the owner's rights, so the role needs no access to base tables.
-- Exposed: an alias derived from a hash, aggregated numbers, data_origin. Never display_name,
-- never profile_id, never raw samples, and only profiles with consent granted AND demo_visible.
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

-- To expose it to a public dashboard, create a login role yourself (do not commit the password):
--   CREATE ROLE demo_dashboard LOGIN PASSWORD '<set-me>' IN ROLE demo_reader;

-- ---------------------------------------------------------------------------
-- Application role: what teammates and tiger_store.py connect as. Not the admin.
-- It can insert and read the app tables, close a session (ended_at, simulator_metrics),
-- and delete a profile (the cascade runs with the table owner's rights). It cannot
-- DROP, TRUNCATE, ALTER, update samples or events, or refresh the continuous aggregates
-- (the 1-minute policies refresh them; a manual refresh needs the admin connection).
--
-- Created NOLOGIN with no password. To let people connect, the admin sets a password
-- interactively so it never lands in a file or shell history:
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
