"""Gesture messages this phase is allowed to publish.

The only label emitted here is ``unknown``, and only for a window that is not
actionable. There is no trained model, so an actionable window gets no label.
Nothing in this module builds a motor command.
"""

from __future__ import annotations

from ml.schema import SCHEMA_VERSION, is_timestamp, validate_gesture
from ml.windows import AssembledWindow

MODEL_VERSION_NONE = "none"
UNKNOWN_CONFIDENCE = 0.0


def unknown_classification(window: AssembledWindow, *, produced_at_ms: int) -> dict:
    """Build a Schema 4 ``unknown`` message for a non-actionable window.

    ``produced_at_ms`` is the classification time. It must be an integer
    timestamp greater than or equal to the newest input timestamp. Confidence
    is 0, which the contract defines as "no score", not as a gesture.
    """
    if not isinstance(window, AssembledWindow):
        raise TypeError("window is not an assembled window")
    if window.actionable:
        raise ValueError("an actionable window is not classified as unknown")
    if window.frame_count < 1:
        raise ValueError("window has no frames")
    if not is_timestamp(produced_at_ms):
        raise ValueError("produced_at_ms is not a timestamp")
    if produced_at_ms < window.end_timestamp_ms:
        raise ValueError("classification time precedes the newest input timestamp")
    message = {
        "schema_version": SCHEMA_VERSION,
        "timestamp_ms": produced_at_ms,
        "source_timestamp_ms": window.end_timestamp_ms,
        "window_start_timestamp_ms": window.start_timestamp_ms,
        "window_end_timestamp_ms": window.end_timestamp_ms,
        "window_frame_count": window.frame_count,
        "label": "unknown",
        "confidence": UNKNOWN_CONFIDENCE,
        "model_version": MODEL_VERSION_NONE,
    }
    errors = validate_gesture(message)
    if errors:
        raise ValueError(errors)
    return message


def classification_for_window(window: AssembledWindow, *, produced_at_ms: int) -> dict | None:
    """Return ``unknown`` for a non-actionable window, or None when it is actionable.

    Phase 1 does not emit ``neutral`` or any other intentional label.
    """
    if not isinstance(window, AssembledWindow):
        raise TypeError("window is not an assembled window")
    if window.actionable:
        return None
    return unknown_classification(window, produced_at_ms=produced_at_ms)
