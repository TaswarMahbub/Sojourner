"""Everything that touches the database: all SQL, session ingestion, and token hashing.
Kept in one file so database access can be reviewed in one place. No FastAPI import, so
scripts/create_token.py can use it."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
from pathlib import Path

import psycopg

try:
    import tiger_store  # copied next to the app in the container image
except ModuleNotFoundError:  # running from the repository
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
    import tiger_store

SCOPES = ("profiles:write", "sessions:write", "aggregates:read")
SUMMARY_VIEWS = ("signal_summary_1min", "control_summary_1min")
MAX_REPORTED_REJECTIONS = 100
_PROFILE_COLS = "profile_id, consent_state, demo_visible, created_at"


# Tokens (only an HMAC of each token is stored) ------------------------------------

def new_token() -> str:
    return "sjr_" + secrets.token_urlsafe(32)


def hash_token(token: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), token.encode(), hashlib.sha256).hexdigest()


def client_scopes(conn, token_hash: str) -> list[str] | None:
    row = conn.execute("SELECT scopes FROM api_clients WHERE token_hash = %s AND revoked_at IS NULL",
                       (token_hash,)).fetchone()
    return None if row is None else list(row["scopes"])


# Profiles -----------------------------------------------------------------------

def create_profile(conn, consent_state: str, demo_visible: bool) -> dict:
    return conn.execute(f"INSERT INTO profiles (consent_state, demo_visible) VALUES (%s, %s) "
                        f"RETURNING {_PROFILE_COLS}", (consent_state, demo_visible)).fetchone()


def get_profile(conn, profile_id) -> dict | None:
    return conn.execute(f"SELECT {_PROFILE_COLS} FROM profiles WHERE profile_id = %s",
                        (profile_id,)).fetchone()


def grant_consent(conn, profile_id) -> dict | None:
    return conn.execute(f"UPDATE profiles SET consent_state = 'granted' WHERE profile_id = %s "
                        f"RETURNING {_PROFILE_COLS}", (profile_id,)).fetchone()


def delete_profile(conn, profile_id) -> bool:
    return conn.execute("DELETE FROM profiles WHERE profile_id = %s", (profile_id,)).rowcount > 0


def deletion_status(conn, profile_id) -> dict:
    exists = get_profile(conn, profile_id) is not None
    leftover = {view: conn.execute(f"SELECT count(*) AS n FROM {view} WHERE profile_id = %s",
                                   (profile_id,)).fetchone()["n"]
                for view in SUMMARY_VIEWS}
    return {"profile_exists": exists, "leftover_summary_rows": leftover}


def lock_profile_consent(conn, profile_id) -> str | None:
    """Consent state, locked until commit so a withdrawal can't interleave with an upload."""
    row = conn.execute("SELECT consent_state FROM profiles WHERE profile_id = %s FOR SHARE",
                       (profile_id,)).fetchone()
    return None if row is None else row["consent_state"]


# Sessions -----------------------------------------------------------------------

def session_by_client_id(conn, client_session_id):
    row = conn.execute("SELECT session_id FROM sessions WHERE client_session_id = %s",
                       (client_session_id,)).fetchone()
    return None if row is None else row["session_id"]


def insert_session(conn, *, profile_id, calibration_id, tier, started_at, data_origin,
                   client_session_id):
    """New session id, or None if another upload with this client_session_id won the race."""
    row = conn.execute(
        "INSERT INTO sessions (profile_id, calibration_id, tier, started_at, data_origin, "
        "client_session_id) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (client_session_id) DO NOTHING RETURNING session_id",
        (profile_id, calibration_id, tier, started_at, data_origin, client_session_id)).fetchone()
    return None if row is None else row["session_id"]


def close_session(conn, session_id, ended_at, simulator_metrics) -> None:
    from psycopg.types.json import Json

    conn.execute("UPDATE sessions SET ended_at = %s, simulator_metrics = %s WHERE session_id = %s",
                 (ended_at, Json(simulator_metrics), session_id))


def get_session(conn, session_id) -> dict | None:
    return conn.execute(
        "SELECT s.session_id, s.profile_id, s.client_session_id, s.tier, s.data_origin, "
        "s.started_at, s.ended_at, s.simulator_metrics, "
        "(SELECT count(*) FROM signal_samples x WHERE x.session_id = s.session_id) AS samples, "
        "(SELECT count(*) FROM control_events e WHERE e.session_id = s.session_id) AS events "
        "FROM sessions s WHERE s.session_id = %s", (session_id,)).fetchone()


# Ingestion (frame validation and COPY reuse backend/db/tiger_store.py) -------------

class IngestError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


class _LostRace(Exception):
    """A concurrent upload with the same client_session_id committed first."""


def _rejection_report(rejections: list) -> dict:
    return {"total": len(rejections),
            "first": [{"line": line, "reason": reason}
                      for line, reason in rejections[:MAX_REPORTED_REJECTIONS]]}


def ingest_session(conn, meta, frames_path: Path, events_path: Path) -> dict:
    """Write one session in one transaction. Returns {"created", "session_id", ...}."""
    stamps = tiger_store.contract_file_timestamps(frames_path)
    if not stamps:
        rejections: list = []
        for _ in tiger_store.contract_file_rows(frames_path, None, None, rejections):
            pass
        raise IngestError(422, {"message": "no frame conforms to the camera feature contract",
                                "rejected_frames": _rejection_report(rejections)})

    try:
        with conn.transaction():
            consent = lock_profile_consent(conn, meta.profile_id)
            if consent is None:
                raise IngestError(404, "profile not found")
            if consent != "granted":
                raise IngestError(403, "profile has not granted consent for cloud upload")

            existing = session_by_client_id(conn, meta.client_session_id)
            if existing is not None:
                return {"created": False, "session_id": existing}

            cal = meta.calibration.model_dump() if meta.calibration else {}
            calibration_id = tiger_store.add_calibration(
                conn, meta.profile_id, meta.tier, cal.get("thresholds"), cal.get("metric_summary"))
            session_id = insert_session(
                conn, profile_id=meta.profile_id, calibration_id=calibration_id, tier=meta.tier,
                started_at=min(stamps), data_origin=meta.data_origin,
                client_session_id=meta.client_session_id)
            if session_id is None:
                raise _LostRace

            rejections = []
            n_samples = tiger_store.copy_rows(
                conn, tiger_store.SIGNAL_SAMPLES_COPY,
                tiger_store.contract_file_rows(frames_path, session_id, meta.profile_id, rejections))
            try:
                n_events = tiger_store.copy_rows(
                    conn, tiger_store.CONTROL_EVENTS_COPY,
                    tiger_store.event_rows(events_path, session_id, meta.profile_id))
            except (KeyError, TypeError, ValueError, psycopg.DataError) as e:
                raise IngestError(422, f"events file is not in the interim event format: {e}") from e
            close_session(conn, session_id, max(stamps), meta.simulator_metrics)
    except _LostRace:
        return {"created": False, "session_id": session_by_client_id(conn, meta.client_session_id)}

    return {"created": True, "session_id": session_id, "samples": n_samples, "events": n_events,
            "rejected_frames": _rejection_report(rejections)}


# Aggregates (continuous aggregates and views only) ---------------------------------

def profile_trends(conn, profile_id, channel: str | None) -> list[dict]:
    return conn.execute(
        "SELECT session_id, started_at, tier, data_origin, channel_name, observed_range, "
        "mean_value, n_samples, tracking_ok_frac FROM session_channel_stats "
        "WHERE profile_id = %s AND (%s::text IS NULL OR channel_name = %s) "
        "ORDER BY channel_name, started_at", (profile_id, channel, channel)).fetchall()


def session_summary(conn, session_id) -> dict:
    channels = conn.execute(
        "SELECT channel_name, observed_range, mean_value, n_samples, tracking_ok_frac "
        "FROM session_channel_stats WHERE session_id = %s ORDER BY channel_name",
        (session_id,)).fetchall()
    control = conn.execute(
        "SELECT bucket, n_events, n_stops, avg_confidence FROM control_summary_1min "
        "WHERE session_id = %s ORDER BY bucket", (session_id,)).fetchall()
    return {"channels": channels, "control_per_minute": control}


# Dashboard (demo_reader role: demo views only) -------------------------------------

def dashboard_sessions(conn) -> list[dict]:
    return conn.execute("SELECT * FROM demo_session_summary ORDER BY started_at DESC LIMIT 500"
                        ).fetchall()


def dashboard_trends(conn, channel: str | None) -> list[dict]:
    return conn.execute(
        "SELECT * FROM demo_channel_trend WHERE (%s::text IS NULL OR channel_name = %s) "
        "ORDER BY channel_name, started_at LIMIT 5000", (channel, channel)).fetchall()
