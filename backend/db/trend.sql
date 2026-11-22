-- Longitudinal trend: one channel's observed range across one profile's measured sessions.
--
--   psql "$TIMESCALE_SERVICE_URL" -v profile_id=<profile-uuid> -v channel=jawOpen -f trend.sql
--
-- Channel names are the stored ones: MediaPipe blendshape names ('jawOpen', 'browInnerUp', ...)
-- or 'head_pose.yaw_deg' / 'head_pose.pitch_deg' / 'head_pose.roll_deg'. Query 2 in queries.sql
-- lists every channel a session has.
--
-- The metric is observed_range, not SNR. SNR needs the rest segment and probe structure of a
-- calibration session, and the database stores neither (calibration profiles are not loaded
-- yet). observed_range is max - min of the channel over the session's tracking frames
-- (tracking_ok only, so lost tracking can't widen it). What it can and can't show:
--   * It measures what the person did in that session, so sessions are comparable only if
--     they followed the same protocol. A session with less movement has a smaller range.
--   * One spike sets the max. Read it next to n_samples and tracking_ok_pct, and check the
--     within-session view (queries.sql, query 5) if a value looks out of line.
--   * Lighting changes noise and frame rate, and the database doesn't record brightness.
--   * Two sessions give one change, not a trend.
-- Synthetic sessions (data_origin = 'synthetic_seed') are excluded.

\if :{?profile_id}
\else
  \echo 'Set the profile: -v profile_id=<profile-uuid> -v channel=<channel name>'
  \quit
\endif
\if :{?channel}
\else
  \echo 'Set the channel: -v channel=<channel name>, e.g. jawOpen'
  \quit
\endif

SELECT row_number() OVER (ORDER BY started_at)                    AS session_no,
       left(session_id::text, 8)                                  AS session,
       started_at,
       tier,
       channel_name,
       round(observed_range::numeric, 3)                          AS observed_range,
       round((observed_range
              - lag(observed_range) OVER (ORDER BY started_at))::numeric, 3) AS change_vs_previous,
       round((100 * (observed_range
              / nullif(first_value(observed_range) OVER (ORDER BY started_at), 0) - 1))::numeric, 1)
                                                                  AS pct_vs_first,
       round(mean_value::numeric, 3)                              AS mean_value,
       n_samples,
       round(100 * tracking_ok_frac::numeric, 1)                  AS tracking_ok_pct
FROM session_channel_stats
WHERE profile_id = :'profile_id'::uuid
  AND channel_name = :'channel'
  AND data_origin = 'measured'
ORDER BY started_at;
