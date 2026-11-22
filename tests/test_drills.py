"""The failure drills as tests, plus stop-latency arithmetic, replay labelling and speed visibility.

tests/drills.py prints the same drills as demo transcripts. Here each one
must pass every check it asserts.
"""

from __future__ import annotations

import json

import pytest

from control.latency import BUDGET_MS, CUED, PARTS, SIGNAL_LOSS, TOTAL, LatencyLog, StopSample, format_table, summarize, verdict
from control.task import HOLD
from tests import drills
from tests.run_doorway_e2e import posed_frame, synthetic_profile
from tests.run_replay import replay
from tests.session_harness import TICK_MS, HarnessRig, Pipeline, Recorder, SimClock, run_loop, start, END


# ----- drills -------------------------------------------------------------------


@pytest.mark.parametrize("ident", sorted(drills.DRILLS))
def test_drill_passes_every_check(ident):
    lines = []
    result = drills.DRILLS[ident](out=lines.append)
    failed = [desc for desc, ok in result.checks if not ok]
    assert result.passed, failed
    assert result.verdict.startswith(f"VERDICT drill {ident} ") and "PASS" in result.verdict
    assert lines and all(line.startswith(f"DRILL {ident} |") for line in lines)


def test_drill_f_new_challenge_is_above_the_persisted_mark_and_corrupt_state_locks():
    result = drills.drill_f(out=lambda _line: None)
    checks = dict(result.checks)
    assert checks["high-water marks survived the kill exactly"]
    assert checks["the new challenge is above the persisted high-water mark (no id reuse across the restart)"]
    assert all(ok for desc, ok in result.checks if desc.startswith("corrupt state"))
    assert sum(desc.startswith("corrupt state") for desc in checks) == 8


def test_drills_cli_exit_status(capsys):
    assert drills.main(["drills.py", "e"]) == 0
    assert "VERDICT drill e" in capsys.readouterr().out
    assert drills.main(["drills.py", "z"]) == 2


# ----- latency arithmetic -------------------------------------------------------


def sample(kind, *parts):
    t, stamps = 1000.0, [1000.0]
    for p in parts:
        t += p
        stamps.append(t)
    return StopSample(kind, tuple(stamps))


def test_summary_median_worst_count_per_part_and_total():
    samples = [sample(CUED, 60, 1, 1, 1), sample(CUED, 100, 3, 1, 1), sample(CUED, 80, 2, 1, 1)]
    table = summarize(samples)
    first = PARTS[CUED][0]
    assert table[first] == {"count": 3, "median": 80, "worst": 100}
    assert table[TOTAL] == {"count": 3, "median": 84, "worst": 105}


def test_verdict_over_budget_names_the_part_that_spends_it():
    samples = [sample(SIGNAL_LOSS, 300, 0.2, 0.1, 0.1)]
    text = verdict(samples)
    assert text.startswith(f"OVER the {BUDGET_MS:.0f} ms budget")
    assert "last actionable frame -> loss detected" in text


def test_verdict_within_budget():
    assert verdict([sample(CUED, 70, 1, 1, 1)]).startswith("WITHIN")


def test_unmeasured_stops_are_reported_not_dropped():
    log = LatencyLog()
    log.skip(CUED, "no torque before the stop")
    lines = format_table(log, CUED)
    assert "0 measured, 1 not measurable" in lines[0]
    assert any("no torque before the stop" in line for line in lines)


def test_latency_module_is_standard_library_only():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "control" / "latency.py").read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert mods <= {"__future__", "dataclasses"}, mods


# ----- a scripted session, recorded, then replayed ------------------------------


def scripted_session(tmp_path, recorder=None, out=None):
    clock = SimClock()
    rig = HarnessRig(tmp_path / "state", clock)
    start(rig)
    lines = []
    p = Pipeline(rig, synthetic_profile(), label="LIVE", out=out or lines.append, every_tick=True, recorder=recorder)
    rest = {b["channel"]: b["rest_mean"] for b in synthetic_profile()["bindings"]}

    def tick(jaw=None, pitch=None, face=True):
        rig.advance(TICK_MS)
        now = clock.unix
        if face:
            frame = posed_frame(now, rest["jawOpen"] if jaw is None else jaw, rest["yaw_deg"],
                                rest["pitch_deg"] if pitch is None else pitch, 0.0)
        else:
            frame = {"schema_version": "4", "timestamp_ms": now, "tracking_state": "lost", "features": {},
                     "head_pose": None, "confidence": None}
        p.tick(frame, arrival=clock.stamp() + 20)  # 20 ms of "inference" between capture and arrival
        return p

    for _ in range(3):
        tick()
    tick(pitch=rest["pitch_deg"] - 15)  # head down in Hold: Slower, the silent drift
    tick(pitch=rest["pitch_deg"] - 15)
    tick()
    for jaw in (0.6, 0.6, None, None, None, None, 0.6, 0.6, None):  # Go, travel, Stop
        tick(jaw=jaw)
    tick(jaw=0.6), tick(jaw=0.6), tick(), tick()  # Go again
    tick(face=False), tick(face=False), tick()  # signal loss while moving
    return p, lines


def test_speed_setting_is_in_every_tick_line_and_hold_drift_is_flagged(tmp_path):
    p, lines = scripted_session(tmp_path)
    tick_lines = [line for line in lines if " ms  " in line and "--" not in line]
    assert tick_lines and all(" v=" in line for line in tick_lines)
    hold_changes = [c for c in p.speed_changes if c.in_hold]
    assert hold_changes and hold_changes[0].after == 0.05 and hold_changes[0].task_before == HOLD
    summary = "\n".join(p.summary())
    assert "in Hold, no feedback" in summary and "speed setting now v=" in summary


def test_scripted_session_measures_a_cued_and_a_signal_loss_stop(tmp_path):
    p, _ = scripted_session(tmp_path)
    assert len(p.log.of(CUED)) == 1 and len(p.log.of(SIGNAL_LOSS)) == 1
    cued = p.log.of(CUED)[0]
    # Two sustained frames at 50 ms: the first crossing frame was captured one tick before the cue.
    assert cued.parts[0] >= TICK_MS
    assert p.rig.watchdog_trips == []


def test_replay_is_labelled_on_every_line_and_reproduces_the_session(tmp_path):
    path = tmp_path / "session.jsonl"
    rec = Recorder(path)
    live, _ = scripted_session(tmp_path, recorder=rec)
    live.print_summary()
    rec.close()
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["protocol_kind"] == "control_session" and header["profile"]

    lines = []
    replayed = replay(path, fast=True, every_tick=True, out=lines.append)
    assert lines and all(line.startswith("REPLAY | ") for line in lines)
    assert any("NOT a live session" in line for line in lines)
    assert lines[-1] == "REPLAY | this summary is from a REPLAY of recorded frames, not a live run"
    assert replayed.history == live.history
    assert len(replayed.log.of(CUED)) == len(live.log.of(CUED)) == 1
    assert len(replayed.log.of(SIGNAL_LOSS)) == len(live.log.of(SIGNAL_LOSS)) == 1


def test_replay_restamps_frames_to_replay_time(tmp_path):
    path = tmp_path / "s.jsonl"
    rec = Recorder(path)
    scripted_session(tmp_path, recorder=rec)[0].print_summary()
    rec.close()
    seen = []
    from tests.session_harness import ReplaySource, load_recording

    clock = SimClock(unix=1_900_000_000_000)
    src = ReplaySource(load_recording(path)[1], clock)
    while (item := src.get(TICK_MS)) is not END:
        if item and item[0] == "frame":
            seen.append(item[2] - item[1]["timestamp_ms"])
    assert seen and all(19 <= age <= 21 for age in seen), "each frame is as old at arrival as it was live"


def test_run_loop_survives_a_camera_error_and_stops_on_staleness(tmp_path):
    clock = SimClock()
    rig = HarnessRig(tmp_path, clock)
    start(rig)
    p = Pipeline(rig, synthetic_profile(), label="LIVE", out=lambda _l: None, every_tick=True)
    rest = {b["channel"]: b["rest_mean"] for b in synthetic_profile()["bindings"]}
    script = [None] * 2 + [0.6, 0.6] + [None] * 4

    class Source:
        def __init__(self):
            self.i = 0

        def get(self, timeout_ms):
            clock.wait(timeout_ms)
            self.i += 1
            if self.i <= len(script):
                jaw = script[self.i - 1]
                frame = posed_frame(clock.unix, rest["jawOpen"] if jaw is None else jaw, rest["yaw_deg"], rest["pitch_deg"], 0.0)
                return ("frame", frame, clock.stamp(), None)
            if self.i == len(script) + 1:
                return ("error", "The camera stopped delivering frames.")
            return None if self.i < len(script) + 12 else END

    run_loop(p, Source())
    assert p.camera_errors and p.controller.task.kind == HOLD
    assert p.stops[SIGNAL_LOSS] == 1 and not rig.receiver.motors_have_torque()
    assert rig.watchdog_trips == []
