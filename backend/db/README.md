# Sojourner: Tiger Cloud data layer

Only needed if you are setting up or using the database.

TimescaleDB on Tiger Cloud. Stores derived channel values, cues, calibration and simulator metrics. Never stores video, images or face templates.

## Setup (admin, once)

1. Create a Tiger Cloud service (free tier is fine). Keep the downloaded `tiger-cloud-db-*-credentials.env` outside the repo.
2. Install psql:
   ```
   brew install libpq
   echo 'export PATH="/opt/homebrew/opt/libpq/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
   ```
3. From `backend/db`: `pip install -r ../../requirements.txt`
4. `export TIMESCALE_SERVICE_URL="postgres://tsdbadmin:<password>@<host>:<port>/tsdb?sslmode=require"` (admin URL; never commit it).
5. `psql "$TIMESCALE_SERVICE_URL" -f schema.sql`. Re-runnable. Don't wrap it in a transaction.
6. Enable the app role and set its password interactively:
   ```
   psql "$TIMESCALE_SERVICE_URL" -c 'ALTER ROLE sojourner_app LOGIN'
   psql "$TIMESCALE_SERVICE_URL" -c '\password sojourner_app'
   ```

7. If the cloud API will use this service, also apply its additions: `psql "$TIMESCALE_SERVICE_URL" -f ../cloud/sql/cloud_schema.sql`.

Teammates: copy `.env.example` to `.env`, fill in the `sojourner_app` URL you were given, then `set -a; source .env; set +a`.

`TIMESCALE_SERVICE_URL` is read by `tiger_store.py` and `psql` only. The cloud API uses `DATABASE_URL_APP` and `DATABASE_URL_DASHBOARD`; see [Which variable points where](../README.md#which-variable-points-where).

## Two-session demonstration

Test with synthetic fixtures first (stored as `synthetic_seed`; never present them as measured):

```
python tiger_store.py make-fixture
python tiger_store.py load-session --new-profile "TEST A" --tier 1 --origin synthetic_seed \
    --frames fixtures/session_a_frames.jsonl --events fixtures/session_a_events.jsonl
python tiger_store.py load-session --profile-id <id from above> --tier 1 --origin synthetic_seed \
    --frames fixtures/session_b_frames.jsonl --events fixtures/session_b_events.jsonl
python tiger_store.py refresh    # admin only; otherwise wait ~1 minute
python tiger_store.py report
```

Real demo: record two sessions of camera feature messages (`docs/contracts.md`, schema `"4"`) and load with `--frame-format contract --origin measured`. Non-conforming frames are skipped and listed. Events still use the interim format (top of `tiger_store.py`).

Other `load-session` options: `--consent granted` (only with real consent; default `pending`), `--demo-visible`, `--calibration cal.json`, `--sim-metrics '{...}'`.

## Privacy

- `demo_reader` sees only `demo_session_summary` and `demo_channel_trend`: hashed aliases and aggregates, for profiles with consent granted and `demo_visible`. Point public dashboards at a login role under it, and keep `data_origin` visible.
- `python tiger_store.py delete-profile --profile-id <uuid>` (admin) deletes the profile, everything referencing it, and its summary rows, then prints the leftover count (should be 0).

## Design notes

- `signal_samples` and `control_events` are hypertables on `ts`, with `profile_id` denormalised for per-profile queries and deletion.
- `control_events` is keyed `(session_id, event_sequence, ts)`. `event_sequence` is cue order within a session, not the contract's motor-command `sequence`.
- Summaries use `tracking_ok` samples only. `session_channel_stats` rolls them up per session and channel.
- The control loop never waits on the database: keep demo profiles local and sync when connected.
- Compression and retention policies are in `schema.sql`, commented out.
- Contract coverage and open items: `backend/README.md` and the top of `schema.sql`.
