# Sojourner cloud API (architecture sketch)

Status: design only, no code yet. Runs locally first, then on Vultr.

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
   HTTPS ──► reverse proxy (TLS, rate limit, body-size cap)
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

Rule: the sync job uploads only profiles whose local consent is `granted`. The API rejects session uploads (`403`) unless the profile's `consent_state` is `granted`. A `pending` profile can exist, but no data is accepted for it.

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

### Historical aggregates (authenticated, per profile)

| Method | Path | Source |
| --- | --- | --- |
| `GET` | `/v1/profiles/{id}/trends?channel=…` | `session_channel_stats`, ordered by `started_at` |
| `GET` | `/v1/sessions/{id}/summary` | `signal_summary_1min`, `control_summary_1min` |

These read continuous aggregates only, never raw `signal_samples` in bulk.

### Dashboard reads (non-critical, public)

| Method | Path | Source |
| --- | --- | --- |
| `GET` | `/v1/dashboard/sessions` | `demo_session_summary` |
| `GET` | `/v1/dashboard/trends?channel=…` | `demo_channel_trend` |

- Separate pool on a login role under `demo_reader`, so this path cannot see base tables, names or ids, whatever the code does.
- In-process TTL cache (60 s) and a stricter rate limit. On a database error, serve stale cache with `"stale": true`, or `503`. Never fail over to pool A.

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

All configuration comes from environment variables: `DATABASE_URL_APP`, `DATABASE_URL_DASHBOARD`, `TOKEN_PEPPER`, `ALLOWED_ORIGINS`. Locally they live in a git-ignored `.env`; on Vultr in a root-only env file on the instance. Never commit them.

## Running locally

`docker compose up` with two services:

- `db`: the `timescale/timescaledb` image, with `backend/db/schema.sql` applied on first start. No Tiger Cloud account needed, no shared data touched.
- `api`: the API container, on `127.0.0.1:8000`.

Pointing `DATABASE_URL_*` at Tiger Cloud instead tests against the shared database.

## Hosting on Vultr

Phase 1, enough for the project:

- One Vultr Cloud Compute instance running the same Compose file, without `db`, plus Caddy for automatic TLS.
- Vultr Firewall: inbound 443 (and 80 for the ACME challenge) only. SSH restricted to team IPs.
- Database stays on Tiger Cloud. If Tiger Cloud supports IP allow-lists on your plan, allow only the instance's IP.
- Images are built in CI and pushed to Vultr Container Registry (or GHCR). Deploy = pull and restart.

Phase 2, only if needed: a Vultr Load Balancer in front of two instances (the API is stateless apart from the cache), and Vultr Object Storage to keep uploaded session files for re-ingestion.

To verify before relying on it: whether Vultr Managed Databases for PostgreSQL offers the TimescaleDB extension. If not, moving the database to Vultr means running TimescaleDB on a compute instance yourselves, which is more work than staying on Tiger Cloud.

## Schema changes this needs

- `sessions.client_session_id uuid UNIQUE`, for idempotent uploads.
- `api_clients (client_id, token_hash, scopes, created_at, revoked_at)`, with no grant to `demo_reader`.
- A dashboard login role: `CREATE ROLE demo_dashboard LOGIN ... IN ROLE demo_reader` (already noted in `schema.sql`).

## Open decisions

- Is the existing `consent_state = 'granted'` enough for cloud upload, or does cloud upload need its own consent flag?
- Should per-profile aggregate reads be limited to the device that uploaded the profile, or open to any team token?
- Should gesture classification messages be stored once PR #4 produces real ones? That needs the `control_events` changes listed in `schema.sql`.
