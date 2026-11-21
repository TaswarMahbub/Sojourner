"""Records later training and adaptation can fill in.

These types do not fit a model, do not read a dataset, and do not load
weights. Participant ids are opaque tokens. They are not names, emails, or
other identifying text, and this repository must not store those.

Normalization parameters are supplied by the caller. This module does not
estimate them. A spec whose source is ``eval`` is refused, so evaluation
frames are not a place to fit those parameters. Applying a spec does not
change its stored mean or scale.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

from ml.features import FEATURE_DIM
from ml.schema import MODEL_VERSION, SAFE_INT_MAX

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SPLITS = frozenset({"train", "eval", "calibration"})
_FIT_SPLITS = frozenset({"train", "calibration"})
_RESERVED_LABELS = frozenset({"neutral", "unknown"})


def _token(value, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is not an opaque token")
    return value


def _timestamp(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= SAFE_INT_MAX:
        raise ValueError(f"{name} is not a timestamp")
    return value


def _vector(values: Sequence[float]) -> tuple[float, ...]:
    if len(values) != FEATURE_DIM:
        raise ValueError(f"feature vector length is {len(values)}, not {FEATURE_DIM}")
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("feature value is not a finite number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("feature value is not a finite number")
        vector.append(number)
    return tuple(vector)


@dataclass(frozen=True)
class LabeledExample:
    """One constructed or future recorded window, already reduced to a vector.

    ``label`` is an annotation for a later trainer. It is not a classification
    this phase produced.
    """

    participant_id: str
    session_id: str
    split: str
    label: str
    features: tuple[float, ...]
    window_start_timestamp_ms: int
    window_end_timestamp_ms: int
    frame_count: int

    def __post_init__(self) -> None:
        _token(self.participant_id, _ID, "participant_id")
        _token(self.session_id, _ID, "session_id")
        if self.split not in _SPLITS:
            raise ValueError("split must be train, eval, or calibration")
        _token(self.label, _LABEL, "label")
        if len(self.features) != FEATURE_DIM:
            raise ValueError(f"feature vector length is {len(self.features)}, not {FEATURE_DIM}")
        for value in self.features:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("feature value is not a finite number")
        start = _timestamp(self.window_start_timestamp_ms, "window_start_timestamp_ms")
        end = _timestamp(self.window_end_timestamp_ms, "window_end_timestamp_ms")
        if start > end:
            raise ValueError("window starts after it ends")
        if isinstance(self.frame_count, bool) or not isinstance(self.frame_count, int) or self.frame_count < 1:
            raise ValueError("frame_count must be an integer >= 1")


@dataclass(frozen=True)
class NormalizationSpec:
    """Per-user location and scale. The caller supplies both. Nothing is fit here."""

    participant_id: str
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    source_split: str

    def __post_init__(self) -> None:
        _token(self.participant_id, _ID, "participant_id")
        if self.source_split not in _FIT_SPLITS:
            raise ValueError("normalization parameters cannot be taken from eval")
        object.__setattr__(self, "mean", _vector(self.mean))
        object.__setattr__(self, "scale", _vector(self.scale))
        if any(value <= 0 for value in self.scale):
            raise ValueError("normalization scale must be positive")


@dataclass(frozen=True)
class PersonalizedModelRef:
    """A later choice of model for one person. This phase stores no weights."""

    participant_id: str
    model_version: str
    disabled_labels: tuple[str, ...]
    rejection_threshold: float | None
    weights_present: bool = False

    def __post_init__(self) -> None:
        _token(self.participant_id, _ID, "participant_id")
        if not isinstance(self.model_version, str) or MODEL_VERSION.fullmatch(self.model_version) is None:
            raise ValueError("model_version is invalid")
        if self.weights_present is not False:
            raise ValueError("this phase does not accept model weights")
        if not isinstance(self.disabled_labels, tuple):
            raise ValueError("disabled_labels must be a tuple")
        seen: set[str] = set()
        for label in self.disabled_labels:
            _token(label, _LABEL, "disabled label")
            if label in _RESERVED_LABELS:
                raise ValueError("neutral and unknown cannot be disabled")
            if label in seen:
                raise ValueError("disabled label is repeated")
            seen.add(label)
        threshold = self.rejection_threshold
        if threshold is not None:
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                raise ValueError("rejection threshold is not a finite number")
            if not math.isfinite(float(threshold)) or not 0.0 < float(threshold) <= 1.0:
                raise ValueError("rejection threshold must be in (0, 1]")


def apply_normalization(
    vector: Sequence[float], spec: NormalizationSpec
) -> tuple[float, ...]:
    """Return ``(x - mean) / scale`` without changing ``spec``."""
    if not isinstance(spec, NormalizationSpec):
        raise TypeError("spec is not a normalization spec")
    values = _vector(vector)
    return tuple((value - mean) / scale for value, mean, scale in zip(values, spec.mean, spec.scale))


def group_by_participant(examples: Sequence[LabeledExample]) -> dict[str, tuple[LabeledExample, ...]]:
    """Group examples by participant, preserving input order inside each group."""
    return _group(examples, lambda example: example.participant_id)


def group_by_session(
    examples: Sequence[LabeledExample],
) -> dict[tuple[str, str], tuple[LabeledExample, ...]]:
    """Group by ``(participant_id, session_id)``, preserving input order."""
    return _group(examples, lambda example: (example.participant_id, example.session_id))


def partition_splits(
    examples: Sequence[LabeledExample],
) -> dict[str, tuple[LabeledExample, ...]]:
    """Split examples into train, eval, and calibration without reordering them."""
    grouped = {name: [] for name in ("train", "eval", "calibration")}
    for example in examples:
        if not isinstance(example, LabeledExample):
            raise TypeError("example is not a labeled example")
        grouped[example.split].append(example)
    return {name: tuple(items) for name, items in grouped.items()}


def _group(examples, key):
    grouped: dict = {}
    for example in examples:
        if not isinstance(example, LabeledExample):
            raise TypeError("example is not a labeled example")
        grouped.setdefault(key(example), []).append(example)
    return {name: tuple(items) for name, items in grouped.items()}
