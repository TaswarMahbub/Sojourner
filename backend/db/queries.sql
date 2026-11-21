-- Sojourner: example queries. Run with: psql "$TIMESCALE_SERVICE_URL" -f queries.sql
-- (or paste any one into the Tiger Cloud SQL editor)
-- Run `python tiger_store.py refresh` after loading so the aggregates include new data.

-- 1. Sessions at a glance: what was written, and where it came from.
SELECT left(session_id::text, 8) AS session, data_origin, tier, started_at, ended_at,
       (SELECT count(*) FROM signal_samples x WHERE x.session_id = s.session_id) AS samples,
       (SELECT count(*) FROM control_events e WHERE e.session_id = s.session_id) AS events
FROM sessions s
ORDER BY started_at DESC;

-- 2. Per-channel range, mean and tracking quality for every session (continuous aggregate).
SELECT left(session_id::text, 8) AS session, channel_name,
       round(observed_range::numeric, 3) AS observed_range,
       round(mean_value::numeric, 3) AS mean_value,
       round(100 * tracking_ok_frac::numeric, 1) AS tracking_ok_pct
FROM session_channel_stats
ORDER BY channel_name, started_at;

-- 3. Longitudinal trend for one profile and one channel: the drift signal.
--    Replace the profile id. Falling observed_range across sessions is the "needs re-fit" flag.
-- SELECT started_at, tier, round(observed_range::numeric, 3) AS observed_range,
--        round(100 * tracking_ok_frac::numeric, 1) AS tracking_ok_pct
-- FROM session_channel_stats
-- WHERE profile_id = '<profile-uuid>' AND channel_name = 'brow_raise_left'
-- ORDER BY started_at;

-- 4. Same trend with change versus the previous session.
-- SELECT started_at, observed_range,
--        observed_range - lag(observed_range) OVER (ORDER BY started_at) AS change_vs_previous
-- FROM session_channel_stats
-- WHERE profile_id = '<profile-uuid>' AND channel_name = 'brow_raise_left'
-- ORDER BY started_at;

-- 5. Within-session fatigue: range per 30-second window for one channel, from raw samples.
--    Replace the session id.
-- SELECT time_bucket(INTERVAL '30 seconds', ts) AS window_start,
--        max(value) - min(value) AS window_range,
--        avg(value) AS window_mean
-- FROM signal_samples
-- WHERE session_id = '<session-uuid>' AND channel_name = 'brow_raise_left' AND tracking_ok
-- GROUP BY window_start
-- ORDER BY window_start;

-- 6. Safety events per session, by reason.
SELECT left(session_id::text, 8) AS session,
       coalesce(stop_reason, 'ordinary cue') AS reason,
       count(*) AS n
FROM control_events
GROUP BY session_id, stop_reason
ORDER BY session_id, n DESC;

-- 7. Cue spacing (seconds between consecutive control events): a hesitation proxy.
SELECT left(session_id::text, 8) AS session, sequence, cue,
       round(extract(epoch FROM (ts - lag(ts) OVER (PARTITION BY session_id ORDER BY sequence)))::numeric, 2)
         AS seconds_since_previous
FROM control_events
ORDER BY session_id, sequence;

-- 8. Event rate per minute from the control aggregate.
SELECT bucket, left(session_id::text, 8) AS session, n_events, n_stops,
       round(avg_confidence::numeric, 3) AS avg_confidence
FROM control_summary_1min
ORDER BY bucket;

-- 9. Confirm both hypertables exist and see their chunks.
SELECT hypertable_name, num_chunks FROM timescaledb_information.hypertables;

-- 10. What the public demo role can see: aggregates only, no names, no raw ids, no samples.
SELECT * FROM demo_session_summary;
SELECT * FROM demo_channel_trend;

-- 11. One-action deletion (commented: destructive). Cascades to sessions, calibrations,
--     labeled trials, signal_samples and control_events.
-- DELETE FROM profiles WHERE profile_id = '<profile-uuid>';
