# Sojourner: Tiger Cloud data layer

Managed Postgres with TimescaleDB for derived channel metrics, control events and per-user profiles.

**Stored:** derived numeric channel values, cues, calibration metrics, simulator metrics.
**Never stored:** raw video, images, face templates.

## Setup

1. Create a Tiger Cloud service (the free tier is enough) and copy its connection string.
2. Install psql to your computer, if not already installed.
   `brew install libpq
    echo 'export PATH="/opt/homebrew/opt/libpq/bin:$PATH"' >> ~/.zshrc
    source ~/.zshrc
    psql --version`
3. `export TIMESCALE_SERVICE_URL="postgres://tsdbadmin:<password>@<host>:<port>/tsdb?sslmode=require"`
   Keep this out of git. Add `.env` to `.gitignore` if you use one.
4. `psql "$TIMESCALE_SERVICE_URL" -f schema.sql`
   (Run it with psql, or paste statements one at a time into the console editor. Do not run it as one transaction; continuous aggregates cannot be created in one.)

## Two-session demonstration

Test the pipeline first with synthetic fixtures. These are labelled `synthetic_seed` in the database and must never be presented as measured:

```
python tiger_store.py make-fixture
python tiger_store.py load-session --new-profile "Test A" --tier 1 --origin synthetic_seed \
    --frames fixtures/session_a_frames.jsonl --events fixtures/session_a_events.jsonl
python tiger_store.py load-session --profile-id e2218a9d-e343-4254-98cc-4811b276cd4b --tier 1 --origin synthetic_seed \
    --frames fixtures/session_b_frames.jsonl --events fixtures/session_b_events.jsonl
python tiger_store.py refresh
python tiger_store.py report
```

For the real demonstration, record two sessions from the MediaPipe channel stream into the JSONL format described at the top of `tiger_store.py`, then load them with `--origin measured`. The report and `queries.sql` then show genuine writes and queries on measured data.

Optional extras on `load-session`: `--calibration cal.json` (thresholds and metric summary), `--sim-metrics '{"cues_per_metre": 3.1}'`, `--demo-visible` (opt this profile into the public views).

## Public demo safety

- `demo_reader` can only read `demo_session_summary` and `demo_channel_trend`. They show a hashed alias and aggregates for profiles with consent granted and `demo_visible` set, and never names, ids or raw samples.
- Point any public dashboard at a login role under `demo_reader`, not at your admin connection.
- For a public demo, use seeded or consenting data, and keep `data_origin` visible.
- `python tiger_store.py delete-profile --profile-id <uuid>` removes everything about a profile in one action.

## Design notes

- `signal_samples` and `control_events` are hypertables on `ts`. `profile_id` is denormalised into both so per-profile queries, indexes and deletion do not need a join.
- Hypertable primary keys must include the time column, so `control_events` is keyed `(session_id, sequence, ts)`.
- `signal_summary_1min` uses `tracking_ok` samples only, so lost tracking cannot inflate a channel's range. `session_channel_stats` rolls it up per session and channel for the drift trend.
- Compression and retention policies are included, commented out, in `schema.sql`.
- Keep local SQLite as the working store during the demo; sync here when connected, since the control loop must never wait on the network.
