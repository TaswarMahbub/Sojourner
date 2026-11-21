"""Deterministic windows over Schema 4 camera feature messages.

Policy:

- A frame is accepted only when ``validate_feature`` reports no errors and its
  ``timestamp_ms`` is strictly greater than the last accepted timestamp.
- An equal timestamp is rejected. Schema 4 allows gaps, and it does not define
  two captures at one timestamp. Strict increase keeps the oldest and newest
  frame unambiguous.
- A rejected frame is not inserted. The rejection is returned to the caller.
  Frames already accepted stay where they are. Nothing is dropped quietly.
- Missing timestamps are not filled in, and scores are not interpolated.
  Elapsed time is not a frame count. The window count is the number of
  accepted messages in that window.
- ``length`` is how many accepted frames form one window. ``stride`` is how
  many accepted frames to advance afterward. Both are integers greater than
  or equal to 1. A stride shorter than the length overlaps. A stride at least
  as long as the length does not.
- A window is actionable only when every frame in it is actionable. One
  ``partial`` or ``lost`` frame makes the window non-actionable.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType

from ml.schema import is_actionable_feature, validate_feature


@dataclass(frozen=True)
class AssembledWindow:
    """One closed window. ``frames`` is a frozen copy of the accepted messages."""

    frames: tuple[MappingProxyType, ...]
    actionable: bool
    start_timestamp_ms: int
    end_timestamp_ms: int
    frame_count: int


@dataclass(frozen=True)
class AssemblerPush:
    accepted: bool
    errors: tuple[str, ...]
    window: AssembledWindow | None


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1")
    return value


class WindowAssembler:
    """Assemble accepted frames into fixed-count windows.

    One accepted frame completes at most one window, because the buffer is
    always shorter than ``length`` before that frame arrives.
    """

    def __init__(self, length: int = 1, stride: int = 1):
        self.length = _positive_int(length, "length")
        self.stride = _positive_int(stride, "stride")
        self._frames: list[tuple[MappingProxyType, bool]] = []
        self._last_timestamp: int | None = None

    @property
    def pending_count(self) -> int:
        return len(self._frames)

    @property
    def last_timestamp_ms(self) -> int | None:
        return self._last_timestamp

    def preview(self, message):
        """Return ``(errors, window, stored)`` without changing the buffer.

        ``window`` is set when accepting this frame would complete one.
        """
        return self._plan(message)

    def push(self, message) -> AssemblerPush:
        errors, window, frozen = self.preview(message)
        if errors:
            return AssemblerPush(False, errors, None)
        assert frozen is not None
        self._frames.append(frozen)
        self._last_timestamp = frozen[0]["timestamp_ms"]
        if window is not None:
            del self._frames[: self.stride]
        return AssemblerPush(True, (), window)

    def _plan(self, message):
        errors = tuple(validate_feature(message))
        if errors:
            return errors, None, None
        timestamp = message["timestamp_ms"]
        if self._last_timestamp is not None and timestamp <= self._last_timestamp:
            return ("timestamp is not strictly newer than the last accepted frame",), None, None
        frozen = _freeze(copy.deepcopy(message))
        actionable = is_actionable_feature(message)
        prospective = (*self._frames, (frozen, actionable))
        window = None
        if len(prospective) >= self.length:
            chunk = prospective[: self.length]
            messages = tuple(item[0] for item in chunk)
            window = AssembledWindow(
                frames=messages,
                actionable=all(item[1] for item in chunk),
                start_timestamp_ms=messages[0]["timestamp_ms"],
                end_timestamp_ms=messages[-1]["timestamp_ms"],
                frame_count=len(messages),
            )
        return (), window, (frozen, actionable)
