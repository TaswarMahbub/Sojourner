"""Command and status authentication for the schema 4 control channel.

docs/contracts.md section 3, "Command authentication": HMAC-SHA256 (RFC 2104)
over a canonical text encoding of the fields. This module is written from that
text, not copied from the test oracle; tests/test_control.py proves the two
produce byte-identical MACs.

The pre-shared key and the controller id bound to it are provisioned out of
band ("Command authentication"). They are read from the environment only.
There is no default key and no key literal in this package. A process without
them refuses to start.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
from dataclasses import dataclass

KEY_ENV = "SOJOURNER_CONTROL_KEY"
CONTROLLER_ENV = "SOJOURNER_CONTROLLER_ID"
MIN_KEY_BYTES = 32

CONTROLLER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAC_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# Section 3, "Command authentication": prefix, then fields in this order.
COMMAND_PREFIX = "sojourner-command-mac-v1\n"
COMMAND_FIELD_ORDER = (
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
    ("linear_mps", "velocity"),
    ("angular_rad_s", "velocity"),
    ("stop", "bool"),
)

# Section 4, "Motor status": status MAC field order. A null session or
# sequence is the token `null` (section 3, "Command authentication").
STATUS_PREFIX = "sojourner-status-mac-v1\n"
STATUS_FIELD_ORDER = (
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


class CanonicalError(ValueError):
    """A field cannot be put into the canonical MAC encoding."""


class MissingCredentials(RuntimeError):
    """The pre-shared key or the controller id is absent or malformed."""


@dataclass(frozen=True)
class Credentials:
    """Pre-shared key and the one controller id bound to it.

    Section 3, "Command authentication": a message whose controller_id is not
    the provisioned controller is not authorized, even with the same key.
    """

    key: bytes
    controller_id: str

    def __repr__(self) -> str:
        return f"Credentials(controller_id={self.controller_id!r}, key=<redacted>)"


def load_credentials(environ=None) -> Credentials:
    """Read the key and controller id from the environment, or refuse.

    `SOJOURNER_CONTROL_KEY` is the key as hex, at least 32 bytes once decoded.
    `SOJOURNER_CONTROLLER_ID` matches the section 3 controller_id pattern.
    Raises MissingCredentials. Never falls back to a default.
    """
    env = os.environ if environ is None else environ
    raw_key = env.get(KEY_ENV)
    if not raw_key:
        raise MissingCredentials(f"{KEY_ENV} is not set; refusing to run without a provisioned key")
    try:
        key = bytes.fromhex(raw_key.strip())
    except ValueError as exc:
        raise MissingCredentials(f"{KEY_ENV} is not hex") from exc
    if len(key) < MIN_KEY_BYTES:
        raise MissingCredentials(f"{KEY_ENV} must decode to at least {MIN_KEY_BYTES} bytes")
    controller_id = env.get(CONTROLLER_ENV)
    if not isinstance(controller_id, str) or CONTROLLER_ID_PATTERN.fullmatch(controller_id) is None:
        raise MissingCredentials(f"{CONTROLLER_ENV} is not set or is not a valid controller_id")
    return Credentials(key=key, controller_id=controller_id)


def canonical_velocity(value) -> float | None:
    """Return the velocity that will be applied, or None when it is not canonical.

    Section 3, "Command authentication" and "Linear and angular motion": the
    applied value is the value printed with exactly six digits after the
    point. A value that is not already equal to its six-digit form is
    rejected, and so is negative zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        try:
            number = float(value)
        except OverflowError:
            # An integer too large for a double has no applied value.
            return None
    else:
        number = value
    if not math.isfinite(number):
        return None
    if number == 0.0 and math.copysign(1.0, number) < 0.0:
        return None
    applied = float(f"{number:.6f}")
    if applied == 0.0 and math.copysign(1.0, applied) < 0.0:
        return None
    if applied != number:
        return None
    return applied


def _field_text(name: str, value, kind: str) -> str:
    if kind == "int_or_null" and value is None:
        return "null"
    if kind in ("int", "int_or_null"):
        if type(value) is not int:
            raise CanonicalError(f"{name} is not an integer")
        return str(value)
    if kind == "bool":
        if value is True:
            return "true"
        if value is False:
            return "false"
        raise CanonicalError(f"{name} is not a boolean")
    if kind == "str":
        if not isinstance(value, str):
            raise CanonicalError(f"{name} is not a string")
        return value
    if kind == "velocity":
        applied = canonical_velocity(value)
        if applied is None:
            raise CanonicalError(f"{name} is not a canonical velocity")
        return f"{applied:.6f}"
    raise CanonicalError(f"unknown field kind {kind!r}")


def _mac_input(prefix: str, order, message) -> bytes:
    if not isinstance(message, dict):
        raise CanonicalError("message is not an object")
    parts = [prefix]
    for name, kind in order:
        if name not in message:
            raise CanonicalError(f"{name} is missing")
        parts.append(f"{name}={_field_text(name, message[name], kind)}\n")
    return "".join(parts).encode("utf-8")


def command_mac_input(message: dict) -> bytes:
    """Canonical command bytes. The `mac` field is not an input (section 3)."""
    return _mac_input(COMMAND_PREFIX, COMMAND_FIELD_ORDER, message)


def status_mac_input(message: dict) -> bytes:
    """Canonical status bytes. The `mac` field is not an input (section 4)."""
    return _mac_input(STATUS_PREFIX, STATUS_FIELD_ORDER, message)


def _hex_mac(key: bytes, body: bytes) -> str:
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise CanonicalError("key is not bytes")
    return hmac.new(bytes(key), body, hashlib.sha256).hexdigest()


def sign_command(key: bytes, message: dict) -> str:
    """Lowercase hex HMAC-SHA256 of the canonical command. Raises CanonicalError."""
    return _hex_mac(key, command_mac_input(message))


def sign_status(key: bytes, message: dict) -> str:
    """Lowercase hex HMAC-SHA256 of the canonical status. Raises CanonicalError."""
    return _hex_mac(key, status_mac_input(message))


def verify_status(key: bytes, message) -> bool:
    """True only when the presented status MAC matches. Constant-time compare.

    A status that cannot be canonically encoded does not verify. Only
    CanonicalError is turned into False; any other error propagates.
    """
    if not isinstance(message, dict):
        return False
    presented = message.get("mac")
    if not isinstance(presented, str) or MAC_PATTERN.fullmatch(presented) is None:
        return False
    try:
        expected = sign_status(key, message)
    except CanonicalError:
        return False
    return hmac.compare_digest(presented, expected)
