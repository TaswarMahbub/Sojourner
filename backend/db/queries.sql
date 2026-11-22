-- Sojourner example queries: psql "$TIMESCALE_SERVICE_URL" -f queries.sql, or paste one at a time.
-- Commented queries need ids filled in. Channel 'brow_raise_left' is synthetic; measured data
-- uses MediaPipe names, e.g. 'browInnerUp'.

-- 1. Sessions: counts and origin.
SELECT left(session_id::text, 8) AS session, data_origin, tier, started_at, ended_at,
       (SELECT count(*) FROM signal_samples x WHERE x.session_id = s.session_id) AS samples,
       (SELECT count(*) FROM control_events e WHERE e.session_id = s.session_id) AS events
FROM sessions s
ORDER BY started_at DESC;

-- 2. Per-channel range, mean, tracking quality per session.
SELECT left(session_id::text, 8) AS session, channel_name,
       round(observed_range::numeric, 3) AS observed_range,
       round(mean_value::numeric, 3) AS mean_value,
       round(100 * tracking_ok_frac::numeric, 1) AS tracking_ok_pct
FROM session_channel_stats
ORDER BY channel_name, started_at;

-- 3. Drift for one profile and channel. Falling observed_range suggests a re-fit.
-- SELECT started_at, tier, round(observed_range::numeric, 3) AS observed_range,
--        round(100 * tracking_ok_frac::numeric, 1) AS tracking_ok_pct
-- FROM session_channel_stats
-- WHERE profile_id = '<profile-uuid>' AND channel_name = 'brow_raise_left'
-- ORDER BY started_at;

-- 4. Same, with change vs. previous session.
-- SELECT started_at, observed_range,
--        observed_range - lag(observed_range) OVER (ORDER BY started_at) AS change_vs_previous
-- FROM session_channel_stats
-- WHERE profile_id = '<profile-uuid>' AND channel_name = 'brow_raise_left'
-- ORDER BY started_at;

-- 5. Within-session fatigue: 30-second range for one channel.
-- SELECT time_bucket(INTERVAL '30 seconds', ts) AS window_start,
--        max(value) - min(value) AS window_range,
--        avg(value) AS window_mean
-- FROM signal_samples
-- WHERE session_id = '<session-uuid>' AND channel_name = 'brow_raise_left' AND tracking_ok
-- GROUP BY window_start
-- ORDER BY window_start;

-- 6. Stops per session, by reason.
SELECT left(session_id::text, 8) AS session,
       coalesce(stop_reason, 'ordinary cue') AS reason,
       count(*) AS n
FROM control_events
GROUP BY session_id, stop_reason
ORDER BY session_id, n DESC;

-- 7. Seconds between cues (hesitation proxy).
SELECT left(session_id::text, 8) AS session, event_sequence, cue,
       round(extract(epoch FROM (ts - lag(ts) OVER (PARTITION BY session_id ORDER BY event_sequence)))::numeric, 2)
         AS seconds_since_previous
FROM control_events
ORDER BY session_id, event_sequence;

-- 8. Events per minute.
SELECT bucket, left(session_id::text, 8) AS session, n_events, n_stops,
       round(avg_confidence::numeric, 3) AS avg_confidence
FROM control_summary_1min
ORDER BY bucket;

-- 9. Hypertables and chunk counts.
SELECT hypertable_name, num_chunks FROM timescaledb_information.hypertables;

-- 10. What the public demo role sees.
SELECT * FROM demo_session_summary;
SELECT * FROM demo_channel_trend;

-- 11. Deletion: use `python tiger_store.py delete-profile --profile-id <uuid>`, which also
--     clears the profile's summary rows. A bare DELETE leaves them until the next policy run.
