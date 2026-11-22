"""Feature processing for the Sojourner gesture classifier.

Phase 1 turns Schema 4 camera feature messages into fixed vectors and into
``unknown`` classifications. It does not train a model and it does not publish
motor commands.
"""

from ml.classify import classification_for_window, unknown_classification
from ml.features import FEATURE_DIM, FEATURE_NAMES, extract_features, named_features
from ml.pipeline import FeaturePipeline, PushResult, WindowProduct
from ml.records import (
    LabeledExample,
    NormalizationSpec,
    PersonalizedModelRef,
    apply_normalization,
    group_by_participant,
    group_by_session,
    partition_splits,
)
from ml.schema import (
    BLENDSHAPES,
    MODEL_BLENDSHAPES,
    SCHEMA_VERSION,
    is_actionable_feature,
    validate_feature,
    validate_gesture,
)
from ml.windows import AssembledWindow, WindowAssembler

__all__ = (
    "BLENDSHAPES",
    "FEATURE_DIM",
    "FEATURE_NAMES",
    "MODEL_BLENDSHAPES",
    "SCHEMA_VERSION",
    "AssembledWindow",
    "FeaturePipeline",
    "LabeledExample",
    "NormalizationSpec",
    "PersonalizedModelRef",
    "PushResult",
    "WindowAssembler",
    "WindowProduct",
    "apply_normalization",
    "classification_for_window",
    "extract_features",
    "group_by_participant",
    "group_by_session",
    "is_actionable_feature",
    "named_features",
    "partition_splits",
    "unknown_classification",
    "validate_feature",
    "validate_gesture",
)
