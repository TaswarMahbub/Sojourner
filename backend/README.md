# Backend

## Which variable points where

Two programs talk to the database, and they read different variables. The names differ on purpose: each variable belongs to one program.

| Program | Variable | Connects as | Set in |
| --- | --- | --- | --- |
| `db/tiger_store.py`, and `psql` with `schema.sql` / `queries.sql` | `TIMESCALE_SERVICE_URL` | `sojourner_app` (teammates) or `tsdbadmin` (admin setup) | `backend/db/.env` |
| Cloud API (`cloud/app/main.py`) | `DATABASE_URL_APP` | `sojourner_app` | Local: built by `cloud/docker-compose.yml` from `backend/cloud/.env`. Vultr: `/etc/sojourner/cloud.env` |
| Cloud API (`cloud/app/main.py`) | `DATABASE_URL_DASHBOARD` | a login role under `demo_reader` (`demo_dashboard` locally) | same as above |
| `cloud/scripts/create_token.py` | `DATABASE_URL_ADMIN` | `postgres` (local) or `tsdbadmin` (Tiger Cloud) | your shell, for one command |

The API does not read `TIMESCALE_SERVICE_URL`, and the loader does not read `DATABASE_URL_*`.

There are two databases, and they don't share data:

| Database | URL host | Who uses it |
| --- | --- | --- |
| Shared Tiger Cloud | `<host>.tsdb.cloud.timescale.com`, database `tsdb` | Teammates' `backend/db/.env`, and the API on Vultr |
| Local Docker container | `127.0.0.1:5432`, database `postgres` | The API from `cloud/docker-compose.yml`, for testing only |

- **Data loaded into one isn't visible in the other.** To test the local API with fixtures, point the loader at the local container for that terminal only. Run this from `backend/cloud`, after `docker compose up`:

  ```sh
  set -a; source .env; set +a
  export TIMESCALE_SERVICE_URL="postgresql://sojourner_app:$APP_DB_PASSWORD@127.0.0.1:5432/postgres"
  ```

- **Check before you load.** Run `echo "${TIMESCALE_SERVICE_URL#*@}"`. It prints the host and database without the password. If the host ends in `tsdb.cloud.timescale.com`, you are writing to the shared database. To go back to it after local testing, open a new terminal and run step 4 of the quick start again.
- **The API needs `cloud/sql/cloud_schema.sql`.** The local container applies it automatically. The shared Tiger Cloud database only has it if the admin ran `psql "$TIMESCALE_SERVICE_URL" -f cloud/sql/cloud_schema.sql` after `schema.sql`.

## db
The team shares one Tiger Cloud database (managed Postgres with TimescaleDB). It stores derived channel numbers, cues, calibration metrics, simulator metrics and per-user profiles.

Stored: derived numeric values only.
Never stored: raw video, images, face templates.

You do not need a Tiger Cloud account or any database setup. One person created the database. You only need the connection URL and about five minutes.

### Quick start

You need Python 3 and git. Clone the repo, then run every command below from `backend/db`:

```sh
cd backend/db
```

1. Get the connection URL from Bailey, privately. It connects as `sojourner_app`, a role that can add and read data and delete a profile, but cannot drop or change tables. It is still a password, so use a direct message or a password manager. Never paste it in a shared channel, an issue, a PR, or a commit.

2. Create your own `.env` file from the committed example. The example file starts with a dot, so `ls` hides it; use `ls -a` to see it.

   ```sh
   cp .env.example .env
   ```

   Open `.env` and replace the placeholder URL with the one you were given. `.env` is ignored by git, so it stays on your machine. Keep the name `.env`.

3. Install the Python dependency. `requirements.txt` is at the repository root, two folders up.

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r ../../requirements.txt
   ```

4. Load your URL into the terminal. Do this in every new terminal window:

   ```sh
   set -a; source .env; set +a
   ```

5. Check it works.

   ```sh
   python tiger_store.py report
   ```

   If you see two tables headed "Latest 2 sessions" and "Per-channel summary", you're connected. Empty tables are fine; they just mean there is no data yet. An error means something is wrong; check that step 4 ran in this window and that the URL in `.env` is complete.

Windows (PowerShell): run `cd backend\db`, `copy .env.example .env`, `py -m venv .venv`, `.venv\Scripts\Activate.ps1`, `pip install -r ..\..\requirements.txt`, then `$env:TIMESCALE_SERVICE_URL="<the URL>"` in each new window.

New laptop? Repeat the steps, or copy your `.env` file across privately.

### What you can do

| Command | What it does |
| --- | --- |
| `python tiger_store.py report` | Show the latest sessions and per-channel summaries |
| `python tiger_store.py make-fixture` | Generate two synthetic test sessions in `fixtures/` |
| `python tiger_store.py load-session ...` | Write one session (frames file + events file) to the database |
| `python tiger_store.py delete-profile --profile-id <uuid>` | Delete a profile and everything about it, including its summary rows (admin connection; prints the leftover count, which should be 0) |

The summaries refresh themselves every minute. `python tiger_store.py refresh` forces it now, but needs the admin connection; `sojourner_app` cannot run it.

You can also run `psql "$TIMESCALE_SERVICE_URL" -f queries.sql` to explore the data. It needs psql installed (on a Mac: `brew install libpq`, then add `/opt/homebrew/opt/libpq/bin` to your PATH).

### Rules for the shared database
- Change the schema through `schema.sql`, in a PR, applied by the admin. Don't make one-off edits in a console, or the repo and the real database drift apart.
- Label your data honestly. Load real recordings with `--origin measured`. Anything generated by make-fixture or made up for testing must use `--origin synthetic_seed`. Never present synthetic data as a measured result.
- Consent is explicit. A new profile is created with consent `pending`. Pass `--consent granted` only when that person has actually agreed.
- Clean up after testing. Use a profile name that says it's a test, such as `--new-profile "TEST yourname"`, and delete it when you're done with `delete-profile`.
- Never commit credentials. No URLs, passwords or `.env` files in git. If you accidentally push one, tell Bailey straight away so the password can be reset.
- No raw video or images, ever. Only derived numbers go in.
- The control loop never waits on the database. Motor control and the stop logic must not depend on a network call. Write to the database after a session or in the background.

### Loading a session

A session is two files in JSONL format (one JSON object per line). One is the camera frames and the other is the cue decisions. The exact fields are described at the top of `tiger_store.py`.

#### Frames from perception (`--frame-format contract`)

Real recordings use the camera feature message from `docs/contracts.md` (schema `"4"`), one message per line. The 52 blendshape names are stored exactly as MediaPipe spells them (`jawOpen`, `eyeBlinkLeft`, ...). `tracking_ok` comes from `tracking_state == "tracking"`. `"confidence": null` is stored as NULL. A frame that does not match the contract, including any other `schema_version`, is skipped and listed after the load.

```json
{"schema_version": "4", "timestamp_ms": 1795276800000, "tracking_state": "lost", "features": {}, "head_pose": null, "confidence": null}
```

#### Synthetic frames (`--frame-format synthetic`, the default)

Only for the output of `make-fixture`:

```json
{"t": 1763700000.033, "channels": {"brow_raise_left": 0.41}, "head": {"yaw": 2.1, "pitch": -0.4, "roll": 0.0}, "confidence": 0.93}
```

#### Events (one per decision)

The gesture classification format from `docs/contracts.md` is not supported yet; it waits on a trained model and a reviewed label list. For now events use this format, as produced by `make-fixture`. Its cues (`go`, `left`, `right`, `stop`, `hold`) are not contract labels.

```json
{"t": 1763700004.5, "cue": "go", "confidence": 0.88, "stop_reason": null}
```

Load them:

```sh
python tiger_store.py make-fixture
python tiger_store.py load-session --new-profile "TEST yourname" --tier 1 --origin synthetic_seed \
    --frames fixtures/session_a_frames.jsonl --events fixtures/session_a_events.jsonl
```

It prints the profile id and session id. To add a second session to the same profile, replace `--new-profile "..."` with `--profile-id <the profile id it printed>`.

Optional flags: `--calibration cal.json`, `--sim-metrics '{"cues_per_metre": 3.1}'`, `--consent granted` (only with real consent), `--demo-visible` (allow aggregated data from this profile in the public demo views).

### Setting up the service from scratch

_Only needed if someone creates a new Tiger Cloud service. See `db/README.md`._

Public demo: point any public dashboard at a login role under `demo_reader`, never at the admin or app URL. `demo_reader` can only read `demo_session_summary` and `demo_channel_trend`, which show hashed aliases and aggregates for profiles with consent granted and `demo_visible` set.

#### Design notes
- `signal_samples` and `control_events` are hypertables on `ts`. `profile_id` is denormalised into both so per-profile queries, indexes and deletion don't need a join.
- `control_events` is a cue log, keyed `(session_id, event_sequence, ts)`, because a hypertable key must include the time column. `event_sequence` is the order of the cue within its session (1, 2, ...). It is named so it isn't confused with the contract's motor-command `sequence`.
- `signal_samples.confidence` is NULL when the producer had no per-frame score.
- `signal_summary_1min` only uses `tracking_ok` samples, so lost tracking can't inflate a channel's range. `session_channel_stats` rolls it up per session and channel for the drift trend.
- Compression and retention policies are in `schema.sql`, commented out.
- Keep a local copy of the demo profiles, and sync here when connected, since the control loop must never wait on the network.

#### Contract coverage and open items

Checked against `docs/contracts.md` schema `"4"`, as merged to `main` in #1.

- **Camera feature stream:** stored in `signal_samples` as described above. Frames are validated against the contract on load, including the head-pose matrix.
- **Gesture classification: open.** Not stored in contract form yet. Events use the interim format, and `model_version` and the window fields are not stored. This waits on a trained model and a reviewed label list.
- **Motor command and motor status:** not stored, by decision. Storage is not part of the real-time stop path. The contract's `boot_nonce` + `session_id` is a UDP link session, not `sessions.session_id`. If datagrams are ever logged, they get their own table.
- **Persisted status-gate state** (challenge and `status_sequence` high-water marks): not stored here. The contract requires a durable local file on the laptop, written before a challenge is sent. A network database can't be on that path.
- **Open, not yet decided:**
  - `tracking_state` is reduced to `tracking_ok`, so `partial` and `lost` aren't distinguished, and a `lost` frame writes no row. Tracking fractions count only frames that carried values.
  - `head_pose.matrix` (translation and scale) is validated but not stored.
  - The `schema_version` of loaded data is not recorded.
