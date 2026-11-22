"""One pass from a camera feature message to a vector or an unknown label.

The pipeline never writes a motor command. An actionable window yields a
feature vector and no gesture label, because no model has been trained.
A window that is not actionable yields ``unknown`` and no feature vector.
"""

from __future__ import annotations

from dataclasses import dataclass

from ml.classify import classification_for_window
from ml.features import extract_features
from ml.schema import is_timestamp
from ml.windows import AssembledWindow, WindowAssembler


@dataclass(frozen=True)
class WindowProduct:
    window: AssembledWindow
    features: tuple[float, ...] | None
    classification: dict | None


@dataclass(frozen=True)
class PushResult:
    accepted: bool
    errors: tuple[str, ...]
    product: WindowProduct | None


class FeaturePipeline:
    """Window assembler plus feature extraction and unknown classification.

    ``produced_at_ms`` is the time the classification would be produced. When
    the accepted frame would complete a non-actionable window, that time must
    be at least the newest input timestamp. If it is earlier, the frame is
    not stored and no message is emitted. The caller can push the same frame
    again with a legal clock. Actionable windows do not emit a label, so this
    clock does not create a classification for them.
    """

    def __init__(self, length: int = 1, stride: int = 1):
        self._assembler = WindowAssembler(length, stride)

    @property
    def pending_count(self) -> int:
        return self._assembler.pending_count

    @property
    def last_timestamp_ms(self) -> int | None:
        return self._assembler.last_timestamp_ms

    def push(self, message, *, produced_at_ms: int) -> PushResult:
        if not is_timestamp(produced_at_ms):
            raise ValueError("produced_at_ms is not a timestamp")
        errors, window, _frozen = self._assembler.preview(message)
        if errors:
            return PushResult(False, errors, None)
        if (
            window is not None
            and not window.actionable
            and produced_at_ms < window.end_timestamp_ms
        ):
            return PushResult(
                False,
                ("classification time precedes the newest input timestamp",),
                None,
            )
        committed = self._assembler.push(message)
        if not committed.accepted or committed.window is None and window is not None:
            return PushResult(False, committed.errors or ("frame was not stored",), None)
        if committed.window is None:
            return PushResult(True, (), None)
        features = extract_features(committed.window)
        classification = classification_for_window(
            committed.window, produced_at_ms=produced_at_ms
        )
        if committed.window.actionable:
            if features is None or classification is not None:
                raise RuntimeError("actionable window did not stay a feature vector")
        elif features is not None or classification is None:
            raise RuntimeError("non-actionable window produced a feature vector")
        return PushResult(
            True,
            (),
            WindowProduct(committed.window, features, classification),
        )
