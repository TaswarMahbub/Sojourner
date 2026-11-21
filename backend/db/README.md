# Sojourner: Tiger Cloud data layer 

## ONLY READ IF SETTING UP TIGER DATA

Managed Postgres with TimescaleDB for derived channel metrics, control events and per-user profiles.

**Stored:** derived numeric channel values, cues, calibration metrics, simulator metrics.
**Never stored:** raw video, images, face templates.

## Setup

1. Create a Tiger Cloud service (the free tier is enough) and copy its connection string. Keep the downloaded `tiger-cloud-db-*-credentials.env` outside the repo; never `git add -f` it.
2. Install psql to your computer, if not already installed.
   `brew install libpq
    echo 'export PATH="/opt/homebrew/opt/libpq/bin:$PATH"' >> ~/.zshrc
    source ~/.zshrc
    psql --version`
3. Install the Python dependency. Run this from `backend/db`; `requirements.txt` is at the repository root.
   `pip install -r ../../requirements.txt`
4. `export TIMESCALE_SERVICE_URL="postgres://tsdbadmin:<password>@<host>:<port>/tsdb?sslmode=require"`
   This is the admin URL. Keep it out of git and out of `.env.example`. `.env` is already git-ignored.
5. `psql "$TIMESCALE_SERVICE_URL" -f schema.sql`
   (Run it with psql, or paste statements one at a time into the console editor. Do not run it as one transaction; continuous aggregates cannot be created in one.)
6. Enable the least-privilege app role and set its password interactively (never in a file or a command line):
   `psql "$TIMESCALE_SERVICE_URL" -c 'ALTER ROLE sojourner_app LOGIN'`
   `psql "$TIMESCALE_SERVICE_URL" -c '\password sojourner_app'`
   Teammates connect as `sojourner_app` using `.env.example` (copy to `.env`). Give them that URL, not the admin URL.

## Two-session demonstration

Test the pipeline first with synthetic fixtures. These are labelled `synthetic_seed` in the database and must never be presented as measured:

```
python tiger_store.py make-fixture
python tiger_store.py load-session --new-profile "Test A" --tier 1 --origin synthetic_seed \
    --frames fixtures/session_a_frames.jsonl --events fixtures/session_a_events.jsonl
# use the profile id printed by the previous command
python tiger_store.py load-session --profile-id <profile-id> --tier 1 --origin synthetic_seed \
    --frames fixtures/session_b_frames.jsonl --events fixtures/session_b_events.jsonl
python tiger_store.py refresh
python tiger_store.py report
```

`refresh` needs the admin connection; with `sojourner_app`, skip it and wait about a minute for the refresh policy.

For the real demonstration, record two sessions of camera feature messages (`docs/contracts.md`, schema `"4"`), one per line, then load them with `--frame-format contract --origin measured`. Frames that do not match the contract are skipped and listed. The events file still uses the interim format (see the top of `tiger_store.py`); contract gesture messages are not supported yet. The report and `queries.sql` then show genuine writes and queries on measured data.

New profiles start with consent `pending`; pass `--consent granted` only with real consent.

Optional extras on `load-session`: `--calibration cal.json` (thresholds and metric summary), `--sim-metrics '{"cues_per_metre": 3.1}'`, `--demo-visible` (opt this profile into the public views).

## Public demo safety

- `demo_reader` can only read `demo_session_summary` and `demo_channel_trend`. They show a hashed alias and aggregates for profiles with consent granted and `demo_visible` set, and never names, ids or raw samples.
- Point any public dashboard at a login role under `demo_reader`, never at the admin or `sojourner_app` connection.
- For a public demo, use seeded or consenting data, and keep `data_origin` visible.
- `python tiger_store.py delete-profile --profile-id <uuid>` removes everything about a profile in one action.

## Design notes

- `signal_samples` and `control_events` are hypertables on `ts`. `profile_id` is denormalised into both so per-profile queries, indexes and deletion do not need a join.
- Hypertable primary keys must include the time column, so `control_events` is keyed `(session_id, event_sequence, ts)`. `event_sequence` is the order of the cue within its session; it is named so it isn't confused with the contract's motor-command `sequence`.
- `signal_summary_1min` uses `tracking_ok` samples only, so lost tracking cannot inflate a channel's range. `session_channel_stats` rolls it up per session and channel for the drift trend.
- Compression and retention policies are included, commented out, in `schema.sql`.
- Keep a local copy of the demo profiles, and sync here when connected, since the control loop must never wait on the network.
- Contract coverage and open items are listed in `backend/README.md` and at the top of `schema.sql`.
