"""HMAC-SHA256 for the schema 4 control channel.

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
MAX_DATAGRAM_BYTES = 8192
MAX_JSON_DEPTH = 32

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
        applied = canonical_velocity(value)
        if applied is None:
            raise ValueError("noncanonical velocity")
        return f"{applied:.6f}"
    raise ValueError("unknown kind")


def canonical_velocity(value):
    """Return the velocity that would be applied, or None if it is not canonical.

    The MAC text is that same value printed with six digits after the decimal.
    A value that would round to a different applied velocity is rejected, as is
    negative zero. Two different velocities therefore cannot share a MAC.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    if not math.isfinite(number):
        return None
    if number == 0.0 and math.copysign(1.0, number) < 0.0:
        return None
    try:
        text = f"{number:.6f}"
        applied = float(text)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(applied) or applied == 0.0 and math.copysign(1.0, applied) < 0.0:
        return None
    if number != applied:
        return None
    return applied


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


def _iter_nonfinite(value) -> bool:
    stack = [value]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > 10000:
            return True
        if isinstance(node, float):
            if not math.isfinite(node):
                return True
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _container_too_deep(value) -> bool:
    stack = [(value, 1)]
    seen = 0
    while stack:
        node, depth = stack.pop()
        seen += 1
        if depth > MAX_JSON_DEPTH or seen > 10000:
            return True
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return False


def _scan_top_identity(raw: str):
    """Read depth-1 boot_nonce and session_id without recursing. Never raises."""
    found = {}
    try:
        depth = 0
        in_string = False
        escape = False
        pending = None
        i = 0
        limit = min(len(raw), MAX_DATAGRAM_BYTES)
        while i < limit:
            ch = raw[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                i += 1
                continue
            if ch == '"':
                j = i + 1
                chars = []
                escaped = False
                while j < limit:
                    mark = raw[j]
                    if escaped:
                        chars.append(mark)
                        escaped = False
                    elif mark == "\\":
                        escaped = True
                    elif mark == '"':
                        break
                    else:
                        chars.append(mark)
                    j += 1
                text = "".join(chars)
                i = j + 1 if j < limit else limit
                k = i
                while k < limit and raw[k] in " \t\r\n":
                    k += 1
                if depth == 1 and k < limit and raw[k] == ":":
                    pending = text
                    i = k + 1
                continue
            if ch in "{[":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    break
                i += 1
                continue
            if ch in "}]":
                depth = max(0, depth - 1)
                pending = None
                i += 1
                continue
            if pending is not None and depth == 1 and ch in "-0123456789":
                j = i + 1 if ch == "-" else i
                digits = j
                while j < limit and raw[j].isdigit():
                    j += 1
                width = j - digits
                if 1 <= width <= 16 and (j >= limit or raw[j] not in ".eE"):
                    if pending in ("boot_nonce", "session_id"):
                        try:
                            found[pending] = int(raw[i:j])
                        except (ValueError, OverflowError):
                            pass
                pending = None
                i = j
                continue
            if ch not in " \t\r\n:,":
                pending = None
            i += 1
    except RecursionError:
        return {}
    except Exception:
        return {}
    return found


def decode_strict(raw: str):
    """Return (object, None) or (None, "malformed"). Never raises.

    Duplicate keys, NaN/Infinity, datagrams over 8192 bytes, and nesting
    deeper than 32 are rejected. A decoded object is not trusted until its
    MAC is checked.
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
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > MAX_DATAGRAM_BYTES:
                return None, "malformed"
            raw = raw.decode("utf-8")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DATAGRAM_BYTES:
            return None, "malformed"
        if _scan_exceeds_depth(raw):
            return None, "malformed"
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except RecursionError:
        return None, "malformed"
    except Exception:
        return None, "malformed"
    try:
        if _iter_nonfinite(value) or _container_too_deep(value):
            return None, "malformed"
    except RecursionError:
        return None, "malformed"
    except Exception:
        return None, "malformed"
    return value, None


def _scan_exceeds_depth(raw: str) -> bool:
    depth = 0
    in_string = False
    escape = False
    try:
        for ch in raw[:MAX_DATAGRAM_BYTES]:
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch in "{[":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    return True
            elif ch in "}]":
                depth = max(0, depth - 1)
    except RecursionError:
        return True
    except Exception:
        return True
    return False


def peek_object(raw):
    """Best-effort identity peek. Never raises. Not an authorization check."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            return None
        found = _scan_top_identity(raw)
    except RecursionError:
        return None
    except Exception:
        return None
    if "boot_nonce" in found and "session_id" in found:
        return {"boot_nonce": found["boot_nonce"], "session_id": found["session_id"]}
    return None
