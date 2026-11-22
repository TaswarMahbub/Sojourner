"""Cue vocabulary, label lookup per model_version, and cue arbitration.

Sources:

* Project report, cue vocabulary: Stop, Go, Left, Right, Through, Slower,
  Faster, Confirm, Cancel. Arbitration: Stop beats Cancel beats everything
  else, and a Stop cue is never queued behind anything.
* docs/contracts.md section 2 "Label": `neutral` and `unknown` are reserved
  in every model version. Any other label must appear in a reviewed label
  list for that exact `model_version` before a consumer treats it as known.
  A label without such an entry is invalid and is handled as `unknown`.

The label lists below are PROPOSED, not reviewed. docs/contracts.md does not
contain a label list yet. Two existing checks therefore still reject every
non-reserved label, and this module changes neither:

* tests/contract_schema.py validate_gesture() reports "label has no
  reviewed list entry", so control/task_controller.py treats such a gesture
  as invalid and stops;
* control/publisher.py has an empty REVIEWED_LABELS, so the publisher sends
  a stop on any tick whose gesture carries such a label.

So in this build cues reach the task machine through the calibrated
channels (control/tiers.py), not through classifier labels. The lists here
become effective only when the same list is added to docs/contracts.md as a
reviewed change agreed with the ML owner, and the validator and the
publisher's list are updated in that same change (see control/README.md).

A cue is a decision by the user. It is not a velocity. control/task.py turns
cues into a motion task, and control/publisher.py decides whether any motion
is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass

# Project report, cue vocabulary. The string is also the classifier label
# proposed for each cue.
STOP = "stop"
GO = "go"
LEFT = "left"
RIGHT = "right"
THROUGH = "through"
SLOWER = "slower"
FASTER = "faster"
CONFIRM = "confirm"
CANCEL = "cancel"

CUES: tuple[str, ...] = (STOP, GO, LEFT, RIGHT, THROUGH, SLOWER, FASTER, CONFIRM, CANCEL)

# docs/contracts.md section 2 "Label": reserved in every model version.
NEUTRAL = "neutral"
UNKNOWN = "unknown"
RESERVED_LABELS = frozenset({NEUTRAL, UNKNOWN})

# A model_version that names this proposed cue list. It is a placeholder
# identifier, not a trained model: no weights exist in the repository.
PROPOSED_CUE_MODEL_VERSION = "cue-vocabulary-proposed-1"

# Label lists keyed by exact model_version. Section 2: "none" is a legal
# identifier for a producer without a trained model, and it has no labels
# beyond the reserved two. A model_version that is not a key here has no
# list, so every non-reserved label it sends is unknown.
#
# PROPOSED, NOT REVIEWED. Must be added to docs/contracts.md and agreed with
# the ML owner before any classifier is trained against it.
LABEL_LISTS: dict[str, frozenset[str]] = {
    "none": frozenset(),
    PROPOSED_CUE_MODEL_VERSION: frozenset(CUES),
}

# Project report, arbitration: lower rank wins.
_RANK = {STOP: 0, CANCEL: 1}
_OTHER_RANK = 2


@dataclass(frozen=True)
class Cue:
    """One user decision.

    `magnitude` is set only by the Tier 1 proportional reader (in (0, 1],
    the fraction of the usable range the excursion reached). Every other
    source leaves it None and the task machine uses its fixed step.
    `source` is a short tag for logs: "label", "tier1", "tier2", "tier3",
    "signal" or "confidence".
    """

    kind: str
    source: str
    magnitude: float | None = None

    def __post_init__(self):
        if self.kind not in CUES:
            raise ValueError(f"not a cue: {self.kind!r}")
        if self.magnitude is not None:
            if isinstance(self.magnitude, bool) or not isinstance(self.magnitude, float):
                raise ValueError("cue magnitude must be a float or None")
            if not 0.0 < self.magnitude <= 1.0:
                raise ValueError("cue magnitude must lie in (0, 1]")


def classify_label(model_version, label) -> str:
    """Map a gesture label to a cue name, NEUTRAL or UNKNOWN.

    Section 2 "Label": a non-reserved label is known only when it appears in
    the list for that exact model_version. A label from a different
    model_version's list, a label that merely looks like a cue, a wrong
    type, or an unlisted model_version all return UNKNOWN.
    """
    if not isinstance(label, str) or not isinstance(model_version, str):
        return UNKNOWN
    if label in RESERVED_LABELS:
        return label
    known = LABEL_LISTS.get(model_version)
    if known is None or label not in known:
        return UNKNOWN
    return label


def arbitrate(cues: list[Cue]) -> list[Cue]:
    """The cues one tick applies, in order.

    Project report: Stop beats Cancel beats everything else. If any Stop is
    present, only that Stop is applied and every other cue in the tick is
    dropped, so nothing can ride along with it or run ahead of it. Otherwise
    if any Cancel is present, only that Cancel is applied. Otherwise every
    cue is applied in arrival order.
    """
    if not cues:
        return []
    best = min(_RANK.get(cue.kind, _OTHER_RANK) for cue in cues)
    if best == _OTHER_RANK:
        return list(cues)
    return [next(cue for cue in cues if _RANK.get(cue.kind) == best)]
