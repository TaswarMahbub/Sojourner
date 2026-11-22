# Sojourner cloud API

Status: scaffolded. Runs locally with Docker Compose; Vultr files included. Not yet run against a real database (see Verification).

## Scope

An HTTP API in front of the existing TimescaleDB schema (`backend/db/schema.sql`) for:

1. Profiles that have consented to cloud upload
2. Session ingestion
3. Historical aggregates
4. Non-critical dashboard reads

Out of scope: anything on the control path. The contract (`docs/contracts.md`) keeps storage off the real-time stop path. The laptop records locally and syncs whole sessions afterwards. If the API is down, the rover is unaffected.

## Shape

```text
 laptop (local-first)                         cloud
 ─────────────────────                        ──────────────────────────────────────────
 perception --record ─► local session files
                        local profile store
        │  sync job: consented profiles only, completed sessions only, retries
        ▼
   HTTPS ──► reverse proxy (TLS, body-size cap)
                │
                ▼
             API service (one process, three route groups, three DB pools)
                ├─ /v1/profiles   ─┐
                ├─ /v1/sessions   ─┴─► pool A: sojourner_app ─┐
                ├─ /v1/profiles/{id}/trends, /v1/sessions/{id}/summary ─► pool A
                └─ /v1/dashboard/* ─► cache ─► pool B: demo_reader login ─┤
                                                                         ▼
                                                     TimescaleDB (Tiger Cloud, or a local container)
 dashboard (frontend) ──► /v1/dashboard/*
```

- **One service, not microservices.** The load is a handful of sessions a day. Route groups separate concerns; DB roles separate privileges.
- **Python (FastAPI + psycopg 3).** It imports the existing contract adapter from `backend/db/tiger_store.py` (`contract_frame_rows`, `read_contract_jsonl`, `copy_rows`), so the cloud validates frames exactly as the CLI does.
- **The database stays TimescaleDB.** Hypertables, continuous aggregates and policies already exist. Only the API moves to Vultr.

## Endpoints

### Profiles (consent-gated)

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/profiles` | Body: `consent_state`, `demo_visible`. Returns `profile_id`. No `display_name` in the cloud; names stay on the laptop |
| `GET` | `/v1/profiles/{id}` | Consent, `demo_visible`, created_at |
| `PUT` | `/v1/profiles/{id}/consent` | `granted` or `withdrawn`. `withdrawn` deletes the profile (as `DELETE` below) |
| `DELETE` | `/v1/profiles/{id}` | Cascade delete. Returns `202` with a status URL; see Deletion |

Rule (decided): `consent_state = 'granted'` is the cloud-upload consent; there is no separate flag. The sync job uploads only profiles whose local consent is `granted`. The API rejects session uploads (`403`) unless the profile's `consent_state` is `granted`. A `pending` profile can exist, but no data is accepted for it.

### Session ingestion

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/sessions` | Multipart: `metadata` (JSON), `frames` (contract JSONL), `events` (interim JSONL). One request per completed session |
| `GET` | `/v1/sessions/{id}` | Metadata, sample and event counts, rejection summary |

- Metadata: `profile_id`, `client_session_id` (UUID made on the laptop), `tier`, `data_origin`, optional calibration and simulator metrics.
- **Idempotent.** A retry with the same `client_session_id` returns the existing session (`200`) instead of writing twice. Needs a schema change (below).
- **All or nothing.** One transaction: calibration, session, `COPY` of samples and events, close. Same as `load_session` today.
- **Validation.** Frames go through the contract adapter. Non-conforming frames are skipped and returned as `{line, reason}` (first 100, plus a total). The request fails (`422`) if no frame conforms.
- **Size.** About 150k rows for a 90-second session. A synchronous `COPY` is fine. Cap the body (e.g. 50 MB) at the proxy. Add a job queue only if uploads start timing out.

### Historical aggregates (any team token)

| Method | Path | Source |
| --- | --- | --- |
| `GET` | `/v1/profiles/{id}/trends?channel=…` | `session_channel_stats`, ordered by `started_at` |
| `GET` | `/v1/sessions/{id}/summary` | `signal_summary_1min`, `control_summary_1min` |

Any valid team token with `aggregates:read` can read any profile's aggregates (decided). There is no per-device ownership check. These read continuous aggregates only, never raw `signal_samples` in bulk.

### Dashboard reads (non-critical, public)

| Method | Path | Source |
| --- | --- | --- |
| `GET` | `/v1/dashboard/sessions` | `demo_session_summary` |
| `GET` | `/v1/dashboard/trends?channel=…` | `demo_channel_trend` |

- Separate pool on a login role under `demo_reader`, so this path cannot see base tables, names or ids, whatever the code does.
- Per-client-IP rate limit (token bucket: 60 requests/minute, burst 20; `429` with `Retry-After`; set with `DASHBOARD_RATE_PER_MINUTE`, `DASHBOARD_BURST`). It matters because each `channel` value is its own cache entry, so the cache alone can't stop a client forcing queries. The dashboard pool's 3 connections cap concurrent database load from this path.
- In-process TTL cache (60 s). On a database error, serve stale cache with `"stale": true`, or `503`. Never fail over to pool A.

## Auth

- **Device tokens** for the laptop sync job: one per laptop, sent as a bearer token, stored hashed in an `api_clients` table, revocable. Scopes: `profiles:write`, `sessions:write`, `aggregates:read`.
- **Dashboard:** no auth, because it only reaches demo views.
- **Admin:** no admin endpoints. Admin tasks stay on the CLI with the admin URL. The API never holds admin credentials.

## Deletion without admin credentials

A continuous aggregate refresh needs the owner and can't run inside a transaction or a function, so the API can't do what `tiger_store.py delete-profile` does. Instead:

1. The API deletes as `sojourner_app`. The base-table cascade is immediate.
2. The refresh policy (`start_offset => NULL`, every minute) removes the profile's summary rows within about a minute.
3. `GET /v1/deletions/{id}` counts the leftover summary rows and reports `pending` or `complete`.

## Secrets and configuration

All configuration comes from environment variables: `DATABASE_URL_APP`, `DATABASE_URL_DASHBOARD`, `TOKEN_PEPPER`, `ALLOWED_ORIGINS`. Locally they live in a git-ignored `.env`; on Vultr in a root-only env file on the instance. Never commit them; `backend/db/tiger-cloud-db-*-credentials.env` is the precedent to avoid.

The API does not read `TIMESCALE_SERVICE_URL`, the loader's variable. See [Which variable points where](../README.md#which-variable-points-where) for which program reads which variable, and which database each points to.

## Layout

```text
app/main.py     settings, DB pools, auth, dashboard cache and rate limit, app factory
app/routes.py   HTTP routes and request bodies
app/store.py    everything that touches the database: SQL, ingestion, token hashing
sql/            cloud_schema.sql: additions on top of backend/db/schema.sql
docker/         local-only init script (role logins)
scripts/        create_token.py (admin)
docker-compose.yml        local: TimescaleDB + API
vultr/          hosting: cloud-init.yaml (server setup + deploy script), docker-compose.yml (API + Caddy), Caddyfile
```

CI: `.github/workflows/deploy-cloud.yml` tests, builds and deploys on pushes to `main`.

## Running locally

Needs Docker Desktop. From `backend/cloud`:

```sh
cp .env.example .env                      # fill each value with: openssl rand -hex 24
docker compose up --build -d              # db applies schema.sql + cloud_schema.sql on first start
set -a; source .env; set +a
pip install -r ../../requirements.txt
DATABASE_URL_ADMIN="postgresql://postgres:$POSTGRES_PASSWORD@127.0.0.1:5432/postgres" \
  python scripts/create_token.py create --name my-laptop \
  --scopes profiles:write sessions:write aggregates:read
```

Then, with the printed token:

```sh
T=<token>
curl -s -H "Authorization: Bearer $T" -H 'content-type: application/json' \
  -d '{"consent_state": "granted"}' localhost:8000/v1/profiles
curl -s -H "Authorization: Bearer $T" localhost:8000/v1/sessions \
  -F metadata='{"profile_id": "<id>", "client_session_id": "'$(uuidgen)'", "tier": 1, "data_origin": "synthetic_seed"}' \
  -F frames=@frames.jsonl -F events=@events.jsonl
curl -s localhost:8000/v1/dashboard/sessions
```

`frames.jsonl` is contract-format camera frames, e.g. from `python -m perception.publish --source fake --record frames.jsonl` (fake data: use `synthetic_seed`). Interactive docs: http://localhost:8000/docs.

Reset the local database: `docker compose down -v`.

Tests (no Docker needed) are in the repository's `tests/test_cloud_api.py` and run in CI: from the repository root, `pip install -r requirements.txt && pytest tests`.

To test against Tiger Cloud instead: apply `sql/cloud_schema.sql` there as admin, create a `demo_dashboard` login under `demo_reader`, and run the API with `DATABASE_URL_*` pointing at it.

## Hosting on Vultr

One Vultr instance runs the API behind Caddy (automatic HTTPS); the database stays on Tiger Cloud. GitHub Actions tests, builds and deploys every push to `main` that touches the API, with automatic rollback if the new release fails its health check. After a one-time setup by one person, deploying needs nothing on anyone's laptop.

Setup, day-to-day use and security notes: [vultr/README.md](vultr/README.md).

## Schema changes (in `sql/cloud_schema.sql`)

- `sessions.client_session_id uuid UNIQUE`, for idempotent uploads.
- `api_clients (client_id, token_hash, scopes, created_at, revoked_at)`, with no grant to `demo_reader`.
- `GRANT UPDATE (consent_state, demo_visible) ON profiles TO sojourner_app`, for granting consent and for the `FOR SHARE` lock taken during uploads.
- A dashboard login role under `demo_reader`. Created by `docker/120_local_roles.sh` locally; by hand on Tiger Cloud.

## Verification

- Done: 20 route tests (auth, consent, idempotent retry, frame rejection, size cap, dashboard cache, stale fallback, rate limiting); server boots and returns 503 with no database; compose files parse.
- Done: the local stack (`docker compose up`) starts, and `tiger_store.py report` connects to its database.
- Done (hosting): cloud-init schema, SSH lockdown, deploy script checks, deploy, rollback and image cleanup, tested in an Ubuntu 24.04 container with a stand-in for Docker; workflows pass `actionlint`.
- Not done: the API routes against a real TimescaleDB; a real Vultr instance; pulling from GHCR with the workflow token.

## Decisions

- Cloud upload consent is the existing `consent_state = 'granted'`. No separate flag.
- Per-profile aggregate reads are open to any team token. No per-device ownership.

## Open decisions

- Should gesture classification messages be stored once PR #4 produces real ones? That needs the `control_events` changes listed in `schema.sql`.
