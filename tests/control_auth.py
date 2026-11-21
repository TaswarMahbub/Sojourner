"""HMAC-SHA256 for the schema 3 control channel.

The MAC is RFC 2104 via the Python standard library. It is an application
check for this demonstration, not a firmware implementation and not DTLS.
The test key below is a fixture. It is not a rover secret and must not be
provisioned onto hardware.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re

TEST_KEY = b"sojourner-test-key-not-for-hardware"
CONTROLLER = "laptop"
CONTROLLER_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAC_HEX = re.compile(r"^[0-9a-f]{64}$")

COMMAND_PREFIX = "sojourner-command-mac-v1\n"
STATUS_PREFIX = "sojourner-status-mac-v1\n"

COMMAND_FIELDS = (
    ("schema_version", "str"),
    ("controller_id", "str"),
    ("boot_nonce", "int"),
    ("session_id", "int"),
    ("sequence", "int"),
    ("freshness_id", "int"),
    ("rearm_generation", "int"),
    ("status_challenge", "int"),
    ("issued_at_ms", "int"),
    ("expires_at_ms", "int"),
    ("task_state", "str"),
    ("linear_mps", "vel"),
    ("angular_rad_s", "vel"),
    ("stop", "bool"),
)

STATUS_FIELDS = (
    ("schema_version", "str"),
    ("controller_id", "str"),
    ("timestamp_ms", "int"),
    ("boot_nonce", "int"),
    ("session_id", "int_or_null"),
    ("last_sequence", "int_or_null"),
    ("status_sequence", "int"),
    ("freshness_id", "int"),
    ("rearm_generation", "int"),
    ("status_challenge", "int"),
    ("clock_state", "str"),
    ("sensor_state", "str"),
    ("cutoff_open", "bool"),
    ("physical_estop", "bool"),
    ("software_estop_latch", "bool"),
    ("fault_latch", "bool"),
    ("armed", "bool"),
    ("commanding_stop", "bool"),
)


def _encode(value, kind: str) -> str:
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("not an int")
        return str(value)
    if kind == "int_or_null":
        if value is None:
            return "null"
        return _encode(value, "int")
    if kind == "bool":
        if value is True:
            return "true"
        if value is False:
            return "false"
        raise ValueError("not a bool")
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError("not a string")
        return value
    if kind == "vel":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("not a velocity")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite velocity")
        return f"{number:.6f}"
    raise ValueError("unknown kind")


def _body(prefix: str, fields, message: dict) -> bytes:
    lines = [prefix]
    for name, kind in fields:
        lines.append(f"{name}={_encode(message[name], kind)}\n")
    return "".join(lines).encode("utf-8")


def _mac(key: bytes, body: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def sign_command(key: bytes, message: dict) -> str:
    return _mac(key, _body(COMMAND_PREFIX, COMMAND_FIELDS, message))


def sign_status(key: bytes, message: dict) -> str:
    return _mac(key, _body(STATUS_PREFIX, STATUS_FIELDS, message))


def _matches(key: bytes, message, prefix: str, fields) -> bool:
    if not isinstance(message, dict) or not isinstance(key, (bytes, bytearray)):
        return False
    presented = message.get("mac")
    if not isinstance(presented, str) or MAC_HEX.fullmatch(presented) is None:
        return False
    try:
        expected = _mac(key, _body(prefix, fields, message))
    except Exception:
        return False
    return hmac.compare_digest(presented, expected)


def verify_command(key: bytes, message) -> bool:
    return _matches(key, message, COMMAND_PREFIX, COMMAND_FIELDS)


def verify_status(key: bytes, message) -> bool:
    return _matches(key, message, STATUS_PREFIX, STATUS_FIELDS)


def seal(receiver, message, *, key=None, freshness_id=None, rearm_generation=None, controller_id=None, status_challenge=None):
    """Copy a command and attach the current challenge, generation, and MAC."""
    sealed = dict(message)
    sealed["controller_id"] = CONTROLLER if controller_id is None else controller_id
    sealed["freshness_id"] = receiver.freshness_id if freshness_id is None else freshness_id
    sealed["rearm_generation"] = (
        receiver.rearm_generation if rearm_generation is None else rearm_generation
    )
    if status_challenge is None:
        status_challenge = sealed.get("status_challenge")
    if not isinstance(status_challenge, int) or isinstance(status_challenge, bool):
        status_challenge = next(_CHALLENGES)
    sealed["status_challenge"] = status_challenge
    sealed["mac"] = sign_command(receiver.key if key is None else key, sealed)
    return sealed


def _challenge_counter():
    value = 1
    while True:
        yield value
        value += 1


_CHALLENGES = _challenge_counter()


def _nonfinite(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_nonfinite(item) for item in value.values())
    if isinstance(value, list):
        return any(_nonfinite(item) for item in value)
    return False


def decode_strict(raw: str):
    """Return (object, None) or (None, "malformed"). Never raises.

    Duplicate keys and NaN/Infinity are rejected. A decoded object is not
    trusted until its MAC is checked.
    """

    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate key")
            obj[key] = value
        return obj

    def reject_constant(name):
        raise ValueError(name)

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return None, "malformed"
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except Exception:
        return None, "malformed"
    if _nonfinite(value):
        return None, "malformed"
    return value, None


def peek_object(raw):
    """Best-effort identity peek. Never raises. Not an authorization check."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return None
        value = json.loads(raw)
    except Exception:
        return None
    if isinstance(value, dict):
        return value
    return None
