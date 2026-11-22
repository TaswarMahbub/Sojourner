"""Regressions for the ML-2A round 1 audit of 4c63935.

A. Fitting is scoped by purpose against a validated split plan.
B. Deeply nested JSON is a numbered rejection, not an escaped RecursionError.
C. Malformed UTF-8 is a numbered rejection, not an escaped UnicodeDecodeError.

Every vector, frame, and token here is constructed in the test. None is a
recording or a participant.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace

import pytest
from perception.fake_stream import encode
from tests.test_ml_dataset import (
    T0,
    _frame,
    _ingest_path,
    _interval,
    _plan,
    _prepared,
)

from ml.dataset import (
    FittingAuthorization,
    FittingScope,
    ParticipantRole,
    RecordingAssignment,
    SessionSplit,
    SplitPlanError,
    ValidatedSplitPlan,
    DatasetError,
    assert_examples_may_fit,
    fitting_scope,
    normalization_candidates,
    validate_split_plan,
    validate_splits,
)
from ml.features import FEATURE_DIM, FEATURE_NAMES

# --------------------------------------------------------------------------
# A. Fitting scopes
# --------------------------------------------------------------------------


def _vec(value):
    return (float(value),) * FEATURE_DIM


# P is a population participant. Q and R are held out, each with calibration
# and eval in different sessions. Q's numbers are far from P's on purpose.
P1 = _prepared("pP", "sP1", "rP1", "train", _vec(0))
P2 = _prepared("pP", "sP2", "rP2", "train", _vec(0.5))
Q_CAL = _prepared("pQ", "sQc", "rQc", "calibration", _vec(100))
Q_EVAL = _prepared("pQ", "sQe", "rQe", "eval", _vec(200))
R_CAL = _prepared("pR", "sRc", "rRc", "calibration", _vec(50))
R_EVAL = _prepared("pR", "sRe", "rRe", "eval", _vec(60))
ALL = (P1, P2, Q_CAL, Q_EVAL, R_CAL, R_EVAL)


def _validated(items=ALL):
    items = list(items)
    return validate_split_plan(*_plan(items), items)


def _codes(examples, scope):
    with pytest.raises(SplitPlanError) as exc:
        assert_examples_may_fit(examples, scope)
    assert exc.value.codes == tuple(sorted(exc.value.codes))
    return set(exc.value.codes)


def _dataset_code(build):
    with pytest.raises(DatasetError) as exc:
        build()
    return exc.value.code


def test_codex_scenario_population_never_pools_held_out_calibration():
    # The audit case: P train near 0, Q calibration near 100, Q eval apart.
    items = [P1, Q_CAL, Q_EVAL]
    validate_splits(*_plan(items), items)
    plan = _validated(items)
    population = fitting_scope(plan, "population")
    assert normalization_candidates(items, population) == (P1,)
    assert normalization_candidates(items) == (P1,)
    assert _codes([P1, Q_CAL], population) >= {"calibration_excluded_from_population_fit"}
    # With no scope there is nothing to check against, so nothing is approved.
    with pytest.raises(SplitPlanError) as exc:
        assert_examples_may_fit([P1, Q_CAL])
    assert exc.value.codes == ("fitting_scope_required",)
    with pytest.raises(SplitPlanError) as exc:
        assert_examples_may_fit([P1])
    assert exc.value.codes == ("fitting_scope_required",)


# 1. Valid population-only training.
def test_population_fit_accepts_population_train_only():
    scope = fitting_scope(_validated(), "population")
    decision = assert_examples_may_fit([P1, P2], scope)
    assert isinstance(decision, FittingAuthorization)
    assert decision.purpose == "population"
    assert decision.target_participant_id is None
    assert decision.examples == (P1, P2)
    assert normalization_candidates(ALL, scope) == (P1, P2)


# 2. Population fitting that includes one calibration example.
def test_population_fit_refuses_one_calibration_example():
    scope = fitting_scope(_validated(), "population")
    codes = _codes([P1, P2, Q_CAL], scope)
    assert "calibration_excluded_from_population_fit" in codes
    assert "held_out_participant_excluded_from_population_fit" in codes


# 3. Population fitting that includes a held-out participant, even under a
# forged train label that the plan never assigned.
def test_population_fit_refuses_a_held_out_participant():
    scope = fitting_scope(_validated(), "population")
    for row in (Q_CAL, R_CAL, Q_EVAL):
        assert "held_out_participant_excluded_from_population_fit" in _codes([P1, row], scope)
    forged = _prepared("pQ", "sQc", "rQc", "train", _vec(100))
    codes = _codes([P1, forged], scope)
    assert {
        "held_out_participant_excluded_from_population_fit",
        "example_not_in_plan",
        "example_recording_mismatch",
    } <= codes
    assert normalization_candidates([P1, forged], scope) == (P1,)


# 4. Population fitting that includes an evaluation example.
def test_population_fit_refuses_an_eval_example():
    scope = fitting_scope(_validated(), "population")
    assert "eval_excluded_from_fitting" in _codes([P1, R_EVAL], scope)


# 5. Valid personalization from one target's calibration examples.
def test_personalization_accepts_the_targets_calibration_only():
    plan = _validated()
    for target, row in (("pQ", Q_CAL), ("pR", R_CAL)):
        scope = fitting_scope(plan, "personalization", target)
        decision = assert_examples_may_fit([row], scope)
        assert decision.purpose == "personalization"
        assert decision.target_participant_id == target
        assert decision.examples == (row,)
        assert normalization_candidates(ALL, scope) == (row,)


# 6. Personalization without a target participant.
def test_personalization_requires_a_target():
    plan = _validated()
    assert _dataset_code(lambda: fitting_scope(plan, "personalization")) == (
        "personalization_target_required"
    )
    assert _dataset_code(lambda: fitting_scope(plan, "personalization", "pZ")) == (
        "personalization_target_not_in_plan"
    )
    for bad in ("", "p Q", 7, ["pQ"]):
        assert _dataset_code(lambda: fitting_scope(plan, "personalization", bad)) == (
            "invalid_participant_id"
        )


# 7. Personalization with a population-training target.
def test_personalization_refuses_a_population_target():
    plan = _validated()
    assert _dataset_code(lambda: fitting_scope(plan, "personalization", "pP")) == (
        "personalization_target_not_held_out"
    )


# 8. Personalization pooling two held-out participants.
def test_personalization_refuses_pooling_two_held_out_participants():
    plan = _validated()
    for target in ("pQ", "pR"):
        scope = fitting_scope(plan, "personalization", target)
        codes = _codes([Q_CAL, R_CAL], scope)
        assert codes == {"other_participant_excluded_from_personalization"}


# 9. Personalization containing population-training examples.
def test_personalization_refuses_population_train_examples():
    scope = fitting_scope(_validated(), "personalization", "pQ")
    codes = _codes([Q_CAL, P1], scope)
    assert {
        "other_participant_excluded_from_personalization",
        "train_excluded_from_personalization",
        "population_participant_excluded_from_personalization",
    } <= codes


# 10. Personalization containing evaluation examples, including the target's own.
def test_personalization_refuses_eval_examples():
    scope = fitting_scope(_validated(), "personalization", "pQ")
    assert _codes([Q_CAL, Q_EVAL], scope) == {"eval_excluded_from_fitting"}
    assert "eval_excluded_from_fitting" in _codes([Q_CAL, R_EVAL], scope)


def test_personalization_decision_does_not_approve_population_selection():
    plan = _validated()
    personal = assert_examples_may_fit([Q_CAL], fitting_scope(plan, "personalization", "pQ"))
    population = fitting_scope(plan, "population")
    assert personal.purpose != "population"
    assert "calibration_excluded_from_population_fit" in _codes(personal.examples, population)


def test_assertion_rechecks_rows_rather_than_trusting_the_helper():
    plan = _validated()
    personal = fitting_scope(plan, "personalization", "pQ")
    population = fitting_scope(plan, "population")
    # Rows filtered for one purpose are refused for the other.
    assert _codes(normalization_candidates(ALL, personal), population)
    assert _codes(normalization_candidates(ALL, population), personal)
    # The unscoped pre-filter approves nothing on its own.
    with pytest.raises(SplitPlanError):
        assert_examples_may_fit(normalization_candidates(ALL))


# 11. Forged or inconsistent plan metadata.
def test_forged_plans_and_scopes_fail_closed():
    plan = _validated()
    report = plan.report
    assert plan.is_validated
    built = ValidatedSplitPlan(report, plan.participant_roles, plan.recording_assignments, ALL)
    copied = replace(plan, participant_roles=(("pP", "held_out"), ("pQ", "population_train")))
    for forged in (built, copied, replace(plan)):
        assert forged.is_validated is False
        assert _dataset_code(lambda: fitting_scope(forged, "population")) == "unvalidated_split_plan"
    # A scope copied onto a forged plan is refused too.
    population = fitting_scope(plan, "population")
    assert _dataset_code(lambda: replace(population, plan=copied)) == "unvalidated_split_plan"
    assert _dataset_code(
        lambda: replace(population, purpose="personalization", target_participant_id="pP")
    ) == "personalization_target_not_held_out"
    # Smuggling a forged plan into an existing scope object is caught at use.
    smuggled = fitting_scope(plan, "population")
    object.__setattr__(smuggled, "plan", copied)
    with pytest.raises(SplitPlanError) as exc:
        assert_examples_may_fit([Q_CAL], smuggled)
    assert exc.value.codes == ("invalid_fitting_scope",)
    assert _dataset_code(lambda: fitting_scope(report, "population")) == "unvalidated_split_plan"
    assert _dataset_code(lambda: FittingScope(object(), "population")) == "unvalidated_split_plan"
    for scope in (report, plan, "population", {"purpose": "population"}):
        with pytest.raises(SplitPlanError) as exc:
            assert_examples_may_fit([P1], scope)
        assert exc.value.codes == ("invalid_fitting_scope",)


def test_rows_outside_the_validated_plan_are_refused():
    plan = _validated([P1, Q_CAL, Q_EVAL])
    population = fitting_scope(plan, "population")
    personal = fitting_scope(plan, "personalization", "pQ")
    # Same identity, different numbers: not a row the plan checked.
    altered = replace(P1, example=replace(P1.example, features=_vec(9)))
    assert _codes([altered], population) == {"example_not_in_plan"}
    # A row from a recording the plan does not know.
    unknown = _prepared("pP", "sP1", "rX", "train")
    assert _codes([unknown], population) == {"example_not_in_plan", "example_recording_mismatch"}
    # A participant the plan does not know at all.
    stranger = _prepared("pZ", "sZ", "rZ", "train")
    assert "example_not_in_plan" in _codes([stranger], population)
    assert "held_out_participant_excluded_from_population_fit" in _codes([stranger], population)
    # P2 and R_CAL are valid in the larger plan but not in this one.
    assert "example_not_in_plan" in _codes([P2], population)
    assert _dataset_code(lambda: fitting_scope(plan, "personalization", "pR")) == (
        "personalization_target_not_in_plan"
    )
    assert _codes([Q_CAL, R_CAL], personal) >= {"example_not_in_plan"}


def test_an_invalid_plan_yields_no_fitting_context():
    items = [P1, Q_CAL]
    roles, sessions, recordings = _plan(items)
    roles = [ParticipantRole("pP", "population_train"), ParticipantRole("pQ", "population_train")]
    with pytest.raises(SplitPlanError):
        validate_split_plan(roles, sessions, recordings, items)
    leaky = [ParticipantRole("pP", "population_train"), ParticipantRole("pQ", "held_out")]
    sessions = [SessionSplit("pP", "sP1", "train"), SessionSplit("pQ", "sQc", "train")]
    recordings = [
        RecordingAssignment("rP1", "pP", "sP1", "train"),
        RecordingAssignment("rQc", "pQ", "sQc", "train"),
    ]
    forged = _prepared("pQ", "sQc", "rQc", "train")
    with pytest.raises(SplitPlanError) as exc:
        validate_split_plan(leaky, sessions, recordings, [P1, forged])
    assert "held_out_participant_in_train" in exc.value.codes


# 12. Reordered examples and repeated calls.
def test_decisions_do_not_depend_on_order_or_repetition():
    plan = _validated()
    population = fitting_scope(plan, "population")
    personal = fitting_scope(plan, "personalization", "pQ")
    mixed = [P1, Q_CAL, R_EVAL]
    expected = {scope.purpose: _codes(mixed, scope) for scope in (population, personal)}
    for order in itertools.permutations(mixed):
        for scope in (population, personal):
            for _ in range(2):
                assert _codes(list(order), scope) == expected[scope.purpose]
    for order in itertools.permutations([P1, P2]):
        assert assert_examples_may_fit(list(order), population).examples == order
        assert set(normalization_candidates(list(order) + [Q_CAL], population)) == {P1, P2}
    first = assert_examples_may_fit([Q_CAL], personal)
    second = assert_examples_may_fit([Q_CAL], personal)
    assert first == second
    rebuilt = fitting_scope(_validated(reversed(ALL)), "personalization", "pQ")
    assert assert_examples_may_fit([Q_CAL], rebuilt) == first


# 13. Empty or malformed fitting requests.
def test_empty_and_malformed_requests_fail_closed():
    plan = _validated()
    population = fitting_scope(plan, "population")
    with pytest.raises(SplitPlanError) as exc:
        assert_examples_may_fit([], population)
    assert exc.value.codes == ("empty_fitting_request",)
    with pytest.raises(SplitPlanError) as exc:
        assert_examples_may_fit([], None)
    assert exc.value.codes == ("fitting_scope_required",)
    for container in ("rP1", b"rP1", {"a": P1}, {P1}, (row for row in [P1]), P1, None, 3):
        with pytest.raises(SplitPlanError) as exc:
            assert_examples_may_fit(container, population)
        assert exc.value.codes == ("invalid_fitting_request",)
        with pytest.raises(SplitPlanError):
            normalization_candidates(container, population)
    for rows in ([P1, None], [P1, P1.example], [object()]):
        with pytest.raises(DatasetError) as exc:
            assert_examples_may_fit(rows, population)
        assert exc.value.code == "invalid_example"
    for purpose in (None, "", "Population", "eval", "calibration", "train", ["population"], 1):
        assert _dataset_code(lambda: fitting_scope(plan, purpose)) == "invalid_fitting_purpose"
    assert _dataset_code(lambda: fitting_scope(plan, "population", "pP")) == (
        "population_fit_takes_no_target"
    )


def test_caller_collections_cannot_change_a_decision_afterwards():
    roles, sessions, recordings = _plan(list(ALL))
    examples = list(ALL)
    plan = validate_split_plan(roles, sessions, recordings, examples)
    population = fitting_scope(plan, "population")
    personal = fitting_scope(plan, "personalization", "pQ")
    rows = [P1, P2]
    decision = assert_examples_may_fit(rows, population)
    # Rewrite every caller-owned list: make Q a population participant.
    roles[:] = [ParticipantRole("pP", "population_train"), ParticipantRole("pQ", "population_train")]
    sessions.append(SessionSplit("pQ", "sQx", "train"))
    recordings.clear()
    examples.clear()
    rows.append(Q_CAL)
    assert decision.examples == (P1, P2)
    assert isinstance(decision.examples, tuple)
    assert plan.role_of("pQ") == "held_out"
    assert plan.examples == ALL
    assert "calibration_excluded_from_population_fit" in _codes([Q_CAL], population)
    assert assert_examples_may_fit([Q_CAL], personal).examples == (Q_CAL,)
    with pytest.raises(Exception):
        decision.examples = (Q_CAL,)
    with pytest.raises(Exception):
        population.purpose = "personalization"


# --------------------------------------------------------------------------
# B and C. Decoding
# --------------------------------------------------------------------------

DEEP_LIST = "[" * 10000 + "0" + "]" * 10000
DEEP_OBJECT = '{"a":' * 10000 + "0" + "}" * 10000
MARKER = "PLANTED-ML2A-7731"
JAW_MEAN = FEATURE_NAMES.index("jawOpen.mean")


def _line(timestamp, jaw=0.0) -> bytes:
    return encode(_frame(timestamp, jaw)).encode("utf-8")


def _ingest_bytes(path, raw: bytes, start=T0, end=T0 + 100):
    path.write_bytes(raw)
    return _ingest_path(path, [_interval("jawOpen", start, end)])


def _survivors(result):
    return [item.example.window_start_timestamp_ms for item in result.examples]


def _rejections(result):
    return [(item.line_number, item.reason) for item in result.rejections]


@pytest.mark.parametrize(
    "deep",
    [DEEP_LIST, DEEP_OBJECT, "[" * 100000 + "]" * 100000],
    ids=["list-10000", "object-10000", "list-100000"],
)
def test_deep_json_between_valid_records_is_one_numbered_rejection(tmp_path, deep):
    raw = _line(T0, 0.1) + b"\n" + deep.encode() + b"\n" + _line(T0 + 2, 0.3) + b"\n"
    result = _ingest_bytes(tmp_path / "deep.jsonl", raw)
    assert _rejections(result) == [(2, "json_depth_exceeded")]
    assert result.counts.records_read == 3
    assert result.counts.accepted_frames == 2
    assert result.counts.rejected_records == 1
    assert _survivors(result) == [T0, T0 + 2]
    jaw = [item.example.features[JAW_MEAN] for item in result.examples]
    assert jaw == pytest.approx([0.1, 0.3])
    assert "[[[" not in repr(result) and '{"a"' not in repr(result)


@pytest.mark.parametrize("deep", [DEEP_LIST, DEEP_OBJECT], ids=["list", "object"])
def test_deep_json_as_first_or_last_record(tmp_path, deep):
    first = _ingest_bytes(tmp_path / "first.jsonl", deep.encode() + b"\n" + _line(T0) + b"\n")
    assert _rejections(first) == [(1, "json_depth_exceeded")]
    assert _survivors(first) == [T0]
    last = _ingest_bytes(tmp_path / "last.jsonl", _line(T0) + b"\n" + deep.encode())
    assert _rejections(last) == [(2, "json_depth_exceeded")]
    assert _survivors(last) == [T0]
    only = _ingest_bytes(tmp_path / "only.jsonl", deep.encode() + b"\n")
    assert _rejections(only) == [(1, "json_depth_exceeded")]
    assert only.examples == () and only.counts.windows_emitted == 0


def test_strict_json_rules_still_hold_next_to_deep_input(tmp_path):
    rows = [
        _line(T0),
        DEEP_LIST.encode(),
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b"[1]",
        b"{bad",
        _line(T0 + 1),
    ]
    result = _ingest_bytes(tmp_path / "strict.jsonl", b"\n".join(rows) + b"\n")
    assert _rejections(result) == [
        (2, "json_depth_exceeded"),
        (3, "duplicate_key"),
        (4, "non_finite_number"),
        (5, "non_finite_number"),
        (6, "not_an_object"),
        (7, "invalid_json"),
    ]
    assert _survivors(result) == [T0, T0 + 1]


BAD_UTF8 = [
    b"\xff",
    b"\xfe\xff",
    b"\xc0\xaf",  # overlong
    b"\xed\xa0\x80",  # encoded surrogate
    b"\xe2\x82",  # truncated sequence
    b"\xf4\x90\x80\x80",  # above U+10FFFF
]


@pytest.mark.parametrize("bad", BAD_UTF8)
def test_invalid_utf8_between_valid_records(tmp_path, bad):
    raw = _line(T0, 0.1) + b"\n" + bad + b"\n" + _line(T0 + 2, 0.3) + b"\n"
    result = _ingest_bytes(tmp_path / "utf8.jsonl", raw)
    assert _rejections(result) == [(2, "invalid_encoding")]
    assert result.counts.records_read == 3
    assert result.counts.accepted_frames == 2
    assert result.counts.rejected_records == 1
    assert _survivors(result) == [T0, T0 + 2]


def test_invalid_utf8_as_first_and_last_line(tmp_path):
    first = _ingest_bytes(tmp_path / "first.jsonl", b"\xff\n" + _line(T0) + b"\n")
    assert _rejections(first) == [(1, "invalid_encoding")]
    assert _survivors(first) == [T0]
    last = _ingest_bytes(tmp_path / "last.jsonl", _line(T0) + b"\n\xff\n")
    assert _rejections(last) == [(2, "invalid_encoding")]
    assert _survivors(last) == [T0]
    unterminated = _ingest_bytes(tmp_path / "unterminated.jsonl", _line(T0) + b"\n\xff")
    assert _rejections(unterminated) == [(2, "invalid_encoding")]
    assert _survivors(unterminated) == [T0]


def test_invalid_bytes_inside_json_shaped_rows_do_not_leak(tmp_path):
    good = _line(T0 + 1)
    shaped = [
        _line(T0).replace(b'"4"', b'"4\xff"'),
        ('{"' + MARKER + '":"').encode() + b"\xc3\x28" + b'"}',
        _line(T0 + 3)[:-1] + b',"x":"\xed\xa0\x80"}',
        (MARKER + " ").encode() + b"\xf8\x88\x80\x80\x80",
    ]
    raw = b"\n".join(shaped[:2] + [good] + shaped[2:]) + b"\n"
    result = _ingest_bytes(tmp_path / "shaped.jsonl", raw)
    assert _rejections(result) == [
        (1, "invalid_encoding"),
        (2, "invalid_encoding"),
        (4, "invalid_encoding"),
        (5, "invalid_encoding"),
    ]
    assert result.counts.records_read == 5
    assert result.counts.accepted_frames == 1
    assert result.counts.rejected_records == 4
    assert _survivors(result) == [T0 + 1]
    text = repr(result) + "".join(str(item) for item in result.rejections)
    assert MARKER not in text
    assert "\\xff" not in text and "\\xc3" not in text and "�" not in text
    assert "UnicodeDecodeError" not in text and "codec" not in text


def test_blank_lines_and_line_numbers_are_unchanged(tmp_path):
    raw = b"\n  \n" + _line(T0) + b"\n\t\n\xff\n\n" + _line(T0 + 1) + b"\n\n"
    result = _ingest_bytes(tmp_path / "blank.jsonl", raw)
    assert _rejections(result) == [(5, "invalid_encoding")]
    assert result.counts.records_read == 3
    assert _survivors(result) == [T0, T0 + 1]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_line_breaks_number_lines_like_the_text_reader(tmp_path, newline):
    rows = [_line(T0), b"\xff", b"{bad", b"", _line(T0 + 1)]
    raw = newline.join(rows) + newline
    result = _ingest_bytes(tmp_path / "breaks.jsonl", raw)
    assert _rejections(result) == [(2, "invalid_encoding"), (3, "invalid_json")]
    assert _survivors(result) == [T0, T0 + 1]
    # The same valid content read the old way, through a text-mode file.
    text_lines = raw.replace(b"\xff", b"x").decode("utf-8")
    path = tmp_path / "text.txt"
    path.write_bytes(text_lines.encode())
    with path.open(encoding="utf-8") as handle:
        assert sum(1 for _ in handle) == len(rows)


def test_byte_order_mark_is_still_invalid_json(tmp_path):
    result = _ingest_bytes(tmp_path / "bom.jsonl", b"\xef\xbb\xbf" + _line(T0) + b"\n" + _line(T0 + 1))
    assert _rejections(result) == [(1, "invalid_json")]
    assert _survivors(result) == [T0 + 1]


def test_valid_multibyte_utf8_is_still_decoded(tmp_path):
    # A non-ASCII string decodes and then fails Schema 4 as before, not as encoding.
    row = json.dumps({"schema_version": "4", "note": "café ✓"}, ensure_ascii=False)
    result = _ingest_bytes(tmp_path / "multi.jsonl", row.encode("utf-8") + b"\n" + _line(T0))
    assert _rejections(result) == [(1, "schema_rejected")]
    assert _survivors(result) == [T0]
